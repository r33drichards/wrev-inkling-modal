use anyhow::{Context, Result, anyhow, ensure};
use minimal_mcp_agent::durable_mysql::MySqlBackend;
use mysql::{Conn, prelude::Queryable};
use ring::{
    aead::{AES_256_GCM, Aad, LessSafeKey, Nonce, UnboundKey},
    rand::{SecureRandom, SystemRandom},
};
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use std::{
    env,
    io::Read,
    time::{SystemTime, UNIX_EPOCH},
};
use subtle::ConstantTimeEq;

const MAX_BODY: u64 = 32 * 1024;
const ROW_CONTEXT: &[u8] = b"wrev/chatgpt-credentials/v1";

#[derive(Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct Credentials {
    access_token: String,
    expires_at: u64,
    refresh_token: Option<String>,
    client_id: Option<String>,
}

#[derive(Deserialize)]
struct RefreshResponse {
    access_token: String,
    refresh_token: String,
    token_type: String,
    expires_in: u64,
    scope: Option<String>,
}

impl Credentials {
    fn validate(&self) -> Result<()> {
        let valid =
            |value: &str| !value.is_empty() && value.bytes().all(|byte| byte.is_ascii_graphic());
        ensure!(valid(&self.access_token), "Invalid access token");
        ensure!(self.expires_at > 0, "Invalid expiry");
        ensure!(
            match (&self.refresh_token, &self.client_id) {
                (None, None) => true,
                (Some(token), Some(client)) => valid(token) && valid(client),
                _ => false,
            },
            "Refresh token and issued client ID must be supplied together"
        );
        Ok(())
    }

    fn refresh(self, http: &reqwest::blocking::Client) -> Result<Self> {
        let response = http
            .post("https://auth.openai.com/api/accounts/oauth/token")
            .form(&[
                ("grant_type", "refresh_token"),
                (
                    "client_id",
                    self.client_id
                        .as_deref()
                        .context("Missing OAuth client ID")?,
                ),
                (
                    "refresh_token",
                    self.refresh_token
                        .as_deref()
                        .context("Missing refresh token")?,
                ),
                ("resource", "https://api.openai.com/v1"),
            ])
            .send()?
            .error_for_status()?
            .json::<RefreshResponse>()?;
        ensure!(
            response.token_type.eq_ignore_ascii_case("bearer"),
            "Invalid token type"
        );
        ensure!(
            response.scope.as_ref().is_none_or(|scope| scope
                .split_whitespace()
                .any(|value| value == "chatgpt.tokens.use.direct")),
            "Missing ChatGPT plan permission"
        );
        ensure!(response.expires_in > 30, "Invalid token lifetime");
        let next = Self {
            access_token: response.access_token,
            refresh_token: Some(response.refresh_token),
            client_id: self.client_id,
            expires_at: now()?
                .checked_add(response.expires_in)
                .context("Invalid expiry")?,
        };
        next.validate()?;
        Ok(next)
    }
}

pub struct HostedAuth {
    db: MySqlBackend,
    key: LessSafeKey,
    admin_digest: [u8; 32],
    http: reqwest::blocking::Client,
}

impl HostedAuth {
    pub fn from_env() -> Result<Option<Self>> {
        let Some(admin) = env::var_os("WREV_CREDENTIAL_ADMIN_TOKEN") else {
            return Ok(None);
        };
        let admin = admin
            .into_string()
            .map_err(|_| anyhow!("Invalid credential admin token"))?;
        ensure!(
            admin.len() >= 32 && admin.bytes().all(|byte| byte.is_ascii_graphic()),
            "Invalid credential admin token"
        );
        let hex_key = env::var("WREV_CREDENTIAL_ENCRYPTION_KEY")
            .context("WREV_CREDENTIAL_ENCRYPTION_KEY is required")?;
        let mut bytes = [0_u8; 32];
        hex::decode_to_slice(hex_key, &mut bytes)
            .map_err(|_| anyhow!("Expected a 32-byte hex encryption key"))?;
        let key =
            UnboundKey::new(&AES_256_GCM, &bytes).map_err(|_| anyhow!("Invalid encryption key"))?;
        let db = MySqlBackend::from_env()?;
        let http = reqwest::blocking::Client::builder()
            .redirect(reqwest::redirect::Policy::none())
            .timeout(crate::config::auth_timeout()?)
            .build()?;
        Ok(Some(Self {
            db,
            key: LessSafeKey::new(key),
            admin_digest: Sha256::digest(admin).into(),
            http,
        }))
    }

    fn lock(&self) -> Result<Conn> {
        let mut conn = self.db.connect()?;
        let acquired: Option<u8> = conn.query_first(
            "SELECT GET_LOCK(SHA2(CONCAT(DATABASE(), ':wrev:chatgpt-credentials'), 256), 0)",
        )?;
        ensure!(acquired == Some(1), "Credential store busy");
        Ok(conn)
    }

    fn save(&self, conn: &mut Conn, credentials: &Credentials) -> Result<()> {
        let sealed = seal(&self.key, credentials)?;
        conn.exec_drop(
            "INSERT INTO wrev_chatgpt_credentials (id, sealed) VALUES (1, ?) ON DUPLICATE KEY UPDATE sealed = ?",
            (&sealed, &sealed),
        )?;
        Ok(())
    }

    pub fn access_token(&self) -> Result<String> {
        let mut conn = self.lock()?;
        let sealed: Vec<u8> = conn
            .query_first("SELECT sealed FROM wrev_chatgpt_credentials WHERE id = 1")?
            .context("Hosted credentials have not been provisioned")?;
        let current = open(&self.key, &sealed)?;
        let time = now()?;
        if current.expires_at > time.saturating_add(30)
            || (current.refresh_token.is_none() && current.expires_at > time)
        {
            return Ok(current.access_token);
        }
        ensure!(
            current.refresh_token.is_some(),
            "Hosted access token expired"
        );
        // Retire the old refresh token before the exchange. A crash or ambiguous
        // network failure must not cause another instance to replay it.
        let mut retired = current.clone();
        retired.refresh_token = None;
        retired.client_id = None;
        self.save(&mut conn, &retired)?;
        let next = current.refresh(&self.http)?;
        self.save(&mut conn, &next)?;
        Ok(next.access_token)
    }

    pub fn put_body(&self, authorization: Option<&str>, body: impl Read) -> u16 {
        let Some(token) = authorization.and_then(|header| header.strip_prefix("Bearer ")) else {
            return 401;
        };
        let digest: [u8; 32] = Sha256::digest(token).into();
        if !bool::from(digest.ct_eq(&self.admin_digest)) {
            return 401;
        }
        let mut bytes = Vec::new();
        if body.take(MAX_BODY + 1).read_to_end(&mut bytes).is_err() || bytes.len() as u64 > MAX_BODY
        {
            return 413;
        }
        let Ok(credentials) = serde_json::from_slice::<Credentials>(&bytes) else {
            return 400;
        };
        if credentials.validate().is_err()
            || now().is_ok_and(|time| {
                credentials.expires_at <= time && credentials.refresh_token.is_none()
            })
        {
            return 400;
        }
        match self
            .lock()
            .and_then(|mut conn| self.save(&mut conn, &credentials))
        {
            Ok(()) => 204,
            Err(_) => {
                eprintln!("Could not store hosted ChatGPT credentials");
                503
            }
        }
    }
}

fn now() -> Result<u64> {
    Ok(SystemTime::now().duration_since(UNIX_EPOCH)?.as_secs())
}

fn seal(key: &LessSafeKey, credentials: &Credentials) -> Result<Vec<u8>> {
    let mut nonce = [0; 12];
    SystemRandom::new()
        .fill(&mut nonce)
        .map_err(|_| anyhow!("Could not generate encryption nonce"))?;
    let mut ciphertext = serde_json::to_vec(credentials)?;
    key.seal_in_place_append_tag(
        Nonce::assume_unique_for_key(nonce),
        Aad::from(ROW_CONTEXT),
        &mut ciphertext,
    )
    .map_err(|_| anyhow!("Could not encrypt credentials"))?;
    Ok([nonce.as_slice(), ciphertext.as_slice()].concat())
}

fn open(key: &LessSafeKey, sealed: &[u8]) -> Result<Credentials> {
    let (nonce, ciphertext) = sealed
        .split_at_checked(12)
        .context("Invalid encrypted credentials")?;
    let mut ciphertext = ciphertext.to_vec();
    let plaintext = key
        .open_in_place(
            Nonce::try_assume_unique_for_key(nonce)
                .map_err(|_| anyhow!("Invalid encryption nonce"))?,
            Aad::from(ROW_CONTEXT),
            &mut ciphertext,
        )
        .map_err(|_| anyhow!("Could not authenticate encrypted credentials"))?;
    let credentials: Credentials = serde_json::from_slice(plaintext)?;
    credentials.validate()?;
    Ok(credentials)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn key(bytes: [u8; 32]) -> LessSafeKey {
        LessSafeKey::new(UnboundKey::new(&AES_256_GCM, &bytes).unwrap())
    }

    #[test]
    fn credentials_are_sealed_with_fresh_nonces_and_authenticated() -> Result<()> {
        let key = key([7; 32]);
        let input = Credentials {
            access_token: "test-access-token".into(),
            expires_at: 123,
            refresh_token: Some("test-refresh-token".into()),
            client_id: Some("test-client-id".into()),
        };
        let mut encrypted = seal(&key, &input)?;
        assert_ne!(&encrypted[..12], &seal(&key, &input)?[..12]);
        let output = open(&key, &encrypted)?;
        assert_eq!(output.access_token, input.access_token);
        assert_eq!(output.refresh_token, input.refresh_token);
        assert_eq!(output.client_id, input.client_id);
        assert_eq!(output.expires_at, input.expires_at);
        assert!(open(&self::key([8; 32]), &encrypted).is_err());
        encrypted[12] ^= 1;
        assert!(open(&key, &encrypted).is_err());
        Ok(())
    }

    #[test]
    fn rejects_incomplete_refresh_credentials_and_header_control_characters() -> Result<()> {
        let mut input: Credentials =
            serde_json::from_str(r#"{"access_token":"token","expires_at":123}"#)?;
        input.validate()?;
        input.refresh_token = Some("refresh".into());
        assert!(input.validate().is_err());
        input.client_id = Some("client".into());
        input.validate()?;
        input.access_token = "token\r\n".into();
        assert!(input.validate().is_err());
        Ok(())
    }

    #[test]
    #[ignore = "requires a disposable MYSQL_TEST_URL database"]
    fn endpoint_replaces_encrypted_credentials_in_mysql() -> Result<()> {
        let opts = mysql::Opts::from_url(&env::var("MYSQL_TEST_URL")?)?;
        let http = reqwest::blocking::Client::builder()
            .timeout(std::time::Duration::from_secs(10))
            .build()?;
        let store = HostedAuth {
            db: MySqlBackend::new(opts),
            key: key([9; 32]),
            admin_digest: Sha256::digest("fixture-admin").into(),
            http: http.clone(),
        };
        store
            .db
            .connect()?
            .query_drop(include_str!("../migrations/002_chatgpt_credentials.sql"))?;
        let server = tiny_http::Server::http("127.0.0.1:0").map_err(|e| anyhow!("{e}"))?;
        let url = format!("http://{}/admin/chatgpt-credentials", server.server_addr());
        let worker = std::thread::spawn(move || -> Result<HostedAuth> {
            for _ in 0..3 {
                let mut request = server
                    .recv_timeout(std::time::Duration::from_secs(10))?
                    .context("Missing fixture request")?;
                let mut authorization_headers = request
                    .headers()
                    .iter()
                    .filter(|header| header.field.equiv("Authorization"));
                let authorization = authorization_headers
                    .next()
                    .map(|header| header.value.as_str().to_owned());
                let authorization =
                    authorization.filter(|_| authorization_headers.next().is_none());
                let status = store.put_body(authorization.as_deref(), request.as_reader());
                request.respond(tiny_http::Response::empty(tiny_http::StatusCode(status)))?;
            }
            Ok(store)
        });
        let post = |admin, token| -> Result<u16> {
            Ok(http
                .put(&url)
                .bearer_auth(admin)
                .json(&serde_json::json!({"access_token": token, "expires_at": now()? + 120}))
                .send()?
                .status()
                .as_u16())
        };
        assert_eq!(post("incorrect", "rejected-token")?, 401);
        assert_eq!(post("fixture-admin", "first-token")?, 204);
        assert_eq!(post("fixture-admin", "replacement-token")?, 204);
        let store = worker.join().expect("credential fixture thread")?;
        let sealed: Vec<u8> = store
            .db
            .connect()?
            .query_first("SELECT sealed FROM wrev_chatgpt_credentials WHERE id = 1")?
            .context("Fixture credential missing")?;
        assert!(
            !sealed
                .windows(b"replacement-token".len())
                .any(|bytes| bytes == b"replacement-token")
        );
        assert_eq!(store.access_token()?, "replacement-token");
        let lock = store.lock()?;
        assert!(store.lock().is_err());
        drop(lock);
        assert!(store.lock().is_ok());
        Ok(())
    }
}
