#[cfg(not(all(target_os = "wasi", target_env = "p2")))]
use crate::config;
use crate::config::ReviewConfig;
#[cfg(not(all(target_os = "wasi", target_env = "p2")))]
use anyhow::ensure;
use anyhow::{Context, Result};
#[cfg(not(all(target_os = "wasi", target_env = "p2")))]
use hmac::{Hmac, Mac};
use minimal_mcp_agent::{agent, quickjs_tools::QuickJsTools};
#[cfg(not(target_os = "wasi"))]
use octocrab::Octocrab as GitHub;
#[cfg(not(all(target_os = "wasi", target_env = "p2")))]
use serde::Deserialize;
use serde_json::{Value, json};
#[cfg(not(all(target_os = "wasi", target_env = "p2")))]
use sha2::Sha256;
#[cfg(any(
    feature = "sqlite-backend",
    not(all(target_os = "wasi", target_env = "p2"))
))]
use std::env;
#[cfg(not(target_os = "wasi"))]
use std::time::Duration;
#[cfg(not(all(target_os = "wasi", target_env = "p2")))]
use std::{
    collections::HashSet,
    io::{self, Read, Write},
};
use tokio::runtime::Runtime;

#[cfg(not(all(target_os = "wasi", target_env = "p2")))]
#[derive(Clone, Debug, Eq, Hash, PartialEq)]
struct Target {
    repository: String,
    number: u64,
    head: String,
    base: String,
}

#[cfg(not(all(target_os = "wasi", target_env = "p2")))]
#[derive(Deserialize)]
struct Repository {
    full_name: String,
}

#[cfg(not(all(target_os = "wasi", target_env = "p2")))]
#[derive(Deserialize)]
struct Label {
    name: String,
}

#[cfg(not(all(target_os = "wasi", target_env = "p2")))]
#[derive(Deserialize)]
struct Ref {
    sha: String,
}

#[cfg(not(target_os = "wasi"))]
#[derive(Deserialize)]
struct Pull {
    state: String,
    base: Ref,
    head: Ref,
    labels: Vec<Label>,
    title: Option<String>,
    body: Option<String>,
}

#[cfg(not(all(target_os = "wasi", target_env = "p2")))]
#[derive(Deserialize)]
struct PullEvent {
    action: String,
    number: u64,
    label: Option<Label>,
    repository: Repository,
    pull_request: EventPull,
}

#[cfg(not(all(target_os = "wasi", target_env = "p2")))]
#[derive(Deserialize)]
struct EventPull {
    head: Ref,
    base: Ref,
}

pub(crate) fn allowed_repo(name: &str) -> bool {
    name.split_once('/').is_some_and(|(owner, repo)| {
        [owner, repo].iter().all(|part| {
            !part.is_empty()
                && *part != "."
                && *part != ".."
                && part
                    .bytes()
                    .all(|byte| byte.is_ascii_alphanumeric() || b"-_.".contains(&byte))
        })
    })
}

pub(crate) fn valid_sha(sha: &str) -> bool {
    sha.len() == 40 && sha.bytes().all(|byte| byte.is_ascii_hexdigit())
}

#[cfg(not(target_os = "wasi"))]
fn authenticate(runtime: &Runtime) -> Result<GitHub> {
    let _runtime = runtime.enter();
    let token = config::required("WREV_GITHUB_TOKEN")?;
    Ok(GitHub::builder().personal_token(token).build()?)
}

#[cfg(not(target_os = "wasi"))]
async fn pull(client: &GitHub, repository: &str, number: u64, timeout: Duration) -> Result<Pull> {
    ensure!(
        allowed_repo(repository) && number > 0,
        "Invalid pull request identity"
    );
    let route = format!("/repos/{repository}/pulls/{number}");
    tokio::time::timeout(timeout, client.get(route, None::<&()>))
        .await
        .context("GitHub metadata request timed out")?
        .context("Read pull request")
}

pub(crate) fn run_agent(
    runtime: &Runtime,
    settings: &ReviewConfig,
    model_client: &crate::model::Client,
    prepare: impl FnOnce() -> Result<QuickJsTools>,
    _identity: &str,
    input: &str,
    access_token: impl Fn() -> Result<String>,
) -> Result<String> {
    let mut definitions = QuickJsTools::definitions();
    definitions[0]["description"] = json!(
        "Run JavaScript to read the mounted review snapshot. Use await fs.readText(path, {startLine, maxLines}) or await fs.listDir(path, {offset}). Use console.log to return results. Each invocation starts a fresh context."
    );
    let prompt = |messages: &[Value]| {
        crate::model::prompt(
            model_client,
            &access_token()?,
            &settings.model,
            messages,
            &definitions,
            include_str!("review_prompt.md"),
        )
    };
    let tools = std::cell::OnceCell::new();
    let mut prepare = Some(prepare);
    let mut hook = |event, _: &agent::Checkpoint| {
        if event == agent::Event::TurnPreparing {
            let prepared = prepare.take().context("Turn already prepared")?()?;
            let _ = tools.set(prepared);
        }
        Ok(())
    };
    let tool = |name: &str, args: &str| {
        let tools = tools.get().context("Review evidence is unavailable")?;
        runtime.block_on(tools.call(name, args, false))
    };
    let mut messages = Vec::new();
    #[cfg(feature = "sqlite-backend")]
    if let Some(dir) = env::var_os("WREV_SESSION_DIR") {
        use minimal_mcp_agent::durable::{Turn, run_durable_turn};
        use minimal_mcp_agent::durable_sqlite::SqliteBackend;
        let backend = SqliteBackend::new(std::path::PathBuf::from(dir));
        messages = run_durable_turn(
            &backend,
            Turn {
                session_id: _identity,
                request_id: "review",
                input,
                max_rounds: usize::MAX,
            },
            prompt,
            |name, args| {
                Ok(tool(name, args)
                    .unwrap_or_else(|err| json!({"isError":true,"error":err.to_string()})))
            },
            |_| {},
            &mut hook,
        )?;
    } else {
        agent::run_turn_with_hooks(&mut messages, input, None, prompt, tool, |_| {}, &mut hook)?;
    }
    #[cfg(not(feature = "sqlite-backend"))]
    agent::run_turn_with_hooks(&mut messages, input, None, prompt, tool, |_| {}, &mut hook)?;
    messages
        .iter()
        .rev()
        .filter(|item| {
            item["type"] == "message"
                && item["role"] == "assistant"
                && item["phase"] != "commentary"
        })
        .find_map(|item| {
            let text = item["content"]
                .as_array()?
                .iter()
                .filter_map(|part| part["text"].as_str())
                .collect::<Vec<_>>()
                .join("\n");
            (!text.trim().is_empty()).then_some(text)
        })
        .context("Model returned no final review")
}

pub(crate) fn benchmark_findings(
    repository: &str,
    number: u64,
    base: &str,
    head: &str,
    review: &str,
) -> Result<Value> {
    let review = review.trim();
    let review = review
        .strip_prefix("```json")
        .or_else(|| review.strip_prefix("```"))
        .and_then(|body| body.strip_suffix("```"))
        .unwrap_or(review);
    let result: Value =
        serde_json::from_str(review).context("Wrev returned invalid ReviewBench JSON")?;
    let findings = result["findings"]
        .as_array()
        .context("Wrev returned no findings array")?
        .iter()
        .map(|finding| {
            let mut finding = finding
                .as_object()
                .context("Invalid ReviewBench finding")?
                .clone();
            finding.insert("producer".into(), json!("wrev"));
            Ok(Value::Object(finding))
        })
        .collect::<Result<Vec<_>>>()?;
    Ok(json!({
        "pr": {
            "repo": format!("https://github.com/{repository}"),
            "pr_number": number,
            "base": base,
            "head": head
        },
        "agent": "wrev",
        "findings": findings
    }))
}

#[cfg(not(target_os = "wasi"))]
pub fn manual() -> Result<()> {
    let mut args = env::args().skip(2);
    let repository = args
        .next()
        .context("Usage: wrev review OWNER/REPO PR_NUMBER")?;
    let number = args.next().context("Missing PR number")?.parse()?;
    ensure!(args.next().is_none(), "Unexpected arguments");
    let runtime = Runtime::new()?;
    let client = authenticate(&runtime)?;
    let settings = ReviewConfig::from_env()?;
    let current = runtime.block_on(pull(&client, &repository, number, settings.github_timeout))?;
    let expected_head = env::var("WREV_EXPECTED_HEAD").ok();
    if let Some(head) = &expected_head {
        ensure!(
            valid_sha(head) && current.head.sha == *head,
            "Pull request head changed before review"
        );
    }
    let required_label = env::var("WREV_REQUIRED_LABEL").ok();
    {
        let eligible = |pr: &Pull| {
            pr.state == "open"
                && required_label
                    .as_ref()
                    .is_none_or(|label| pr.labels.iter().any(|entry| &entry.name == label))
        };
        ensure!(
            eligible(&current),
            "Pull request is closed or no longer labeled"
        );
        let snapshot = crate::benchmark::Snapshot {
            repo: Some(repository.replace('/', "_")),
            nwo: repository,
            pr_number: number,
            base: current.base.sha,
            head: current.head.sha,
            title: current.title,
            body: current.body,
        };
        let mut result = crate::benchmark::review(
            &snapshot,
            || {
                let checkout =
                    crate::benchmark::fetch(&snapshot, &config::required("WREV_GITHUB_TOKEN")?)?;
                crate::benchmark::temporary_mount(checkout.path(), &snapshot)
            },
            config::chatgpt_token,
        )?;
        let latest = runtime.block_on(pull(
            &client,
            &snapshot.nwo,
            number,
            settings.github_timeout,
        ))?;
        ensure!(
            eligible(&latest)
                && latest.base.sha == snapshot.base
                && latest.head.sha == snapshot.head,
            "Pull request changed during review"
        );
        result["repository"] = json!(snapshot.nwo);
        result["number"] = json!(number);
        result["head"] = json!(snapshot.head);
        result["review"] = json!(result["findings"].to_string());
        println!("{result}");
        Ok(())
    }
}

#[cfg(not(all(target_os = "wasi", target_env = "p2")))]
fn signed(secret: &[u8], signature: &str, body: &[u8]) -> bool {
    if signature.len() != 71 {
        return false;
    }
    let Some(bytes) = signature
        .strip_prefix("sha256=")
        .and_then(|value| hex::decode(value).ok())
    else {
        return false;
    };
    let mut mac = Hmac::<Sha256>::new_from_slice(secret).expect("HMAC accepts any key length");
    mac.update(body);
    mac.verify_slice(&bytes).is_ok()
}

#[cfg(not(all(target_os = "wasi", target_env = "p2")))]
fn labeled_target(
    body: &[u8],
    repositories: &HashSet<String>,
    expected_label: &str,
) -> Result<Option<Target>> {
    let event: PullEvent = serde_json::from_slice(body)?;
    if event.action != "labeled"
        || event.label.is_none_or(|label| label.name != expected_label)
        || !repositories.contains(&event.repository.full_name.to_ascii_lowercase())
    {
        return Ok(None);
    }
    ensure!(
        event.number > 0
            && valid_sha(&event.pull_request.head.sha)
            && valid_sha(&event.pull_request.base.sha),
        "Invalid pull request"
    );
    Ok(Some(Target {
        repository: event.repository.full_name.to_ascii_lowercase(),
        number: event.number,
        head: event.pull_request.head.sha,
        base: event.pull_request.base.sha,
    }))
}

#[cfg(not(all(target_os = "wasi", target_env = "p2")))]
fn repositories() -> Result<HashSet<String>> {
    let repositories: HashSet<_> = config::required("WREV_REPOSITORIES")?
        .split(',')
        .map(|name| name.trim().to_ascii_lowercase())
        .collect();
    ensure!(
        !repositories.is_empty() && repositories.iter().all(|name| allowed_repo(name)),
        "Expected a comma-separated repository allowlist"
    );
    Ok(repositories)
}

#[cfg(not(all(target_os = "wasi", target_env = "p2")))]
fn webhook_target(
    body: impl Read,
    signature: Option<&str>,
    event: Option<&str>,
    secret: &[u8],
    repositories: &HashSet<String>,
    webhook: &config::WebhookConfig,
) -> std::result::Result<Target, u16> {
    let signature = signature.ok_or(401u16)?;
    let mut bytes = Vec::new();
    if body
        .take((webhook.body_bytes as u64).saturating_add(1))
        .read_to_end(&mut bytes)
        .is_err()
        || bytes.len() > webhook.body_bytes
    {
        return Err(413);
    }
    if !signed(secret, signature, &bytes) {
        return Err(401);
    }
    if event != Some("pull_request") {
        return Err(204);
    }
    labeled_target(&bytes, repositories, &webhook.label)
        .map_err(|_| 400u16)?
        .ok_or(204)
}

#[cfg(not(all(target_os = "wasi", target_env = "p2")))]
fn wcgi_request(method: &str, path: &str) -> Result<u16> {
    match (method, path) {
        ("GET", "/health") => {
            config::required("WREV_WEBHOOK_SECRET")?;
            repositories()?;
            config::WebhookConfig::from_env()?;
            ReviewConfig::from_env()?;
            Ok(200)
        }
        #[cfg(feature = "mysql-backend")]
        ("PUT", "/admin/chatgpt-credentials") => {
            let authorization = env::var("HTTP_AUTHORIZATION").ok();
            Ok(
                crate::hosted_auth::HostedAuth::from_env()?.map_or(404, |store| {
                    store.put_body(authorization.as_deref(), io::stdin().lock())
                }),
            )
        }
        ("POST", "/github/webhook") => {
            let Ok(signature) = env::var("HTTP_X_HUB_SIGNATURE_256") else {
                return Ok(401);
            };
            let secret = config::required("WREV_WEBHOOK_SECRET")?;
            let repositories = repositories()?;
            let webhook = config::WebhookConfig::from_env()?;
            let event = env::var("HTTP_X_GITHUB_EVENT").ok();
            let target = match webhook_target(
                io::stdin().lock(),
                Some(&signature),
                event.as_deref(),
                secret.as_bytes(),
                &repositories,
                &webhook,
            ) {
                Ok(target) => target,
                Err(status) => return Ok(status),
            };
            let root = std::path::PathBuf::from(config::required("WREV_SNAPSHOT_DIR")?);
            let snapshot = crate::benchmark::metadata(&root)?;
            ensure!(
                snapshot.nwo.eq_ignore_ascii_case(&target.repository)
                    && snapshot.pr_number == target.number
                    && snapshot.head == target.head
                    && snapshot.base == target.base,
                "Host snapshot does not match the signed webhook"
            );
            #[cfg(feature = "mysql-backend")]
            let credentials = crate::hosted_auth::HostedAuth::from_env()?;
            let review = crate::benchmark::review(
                &snapshot,
                || Ok(crate::benchmark::Mount::supplied(root)),
                || {
                    #[cfg(feature = "mysql-backend")]
                    if let Some(store) = &credentials {
                        return store.access_token();
                    }
                    config::chatgpt_token()
                },
            )?;
            eprintln!("{review}");
            Ok(204)
        }
        _ => Ok(404),
    }
}

#[cfg(not(all(target_os = "wasi", target_env = "p2")))]
pub fn wcgi() -> Result<()> {
    let method = env::var("REQUEST_METHOD").unwrap_or_default();
    let uri = env::var("REQUEST_URI").unwrap_or_default();
    let path = env::var("PATH_INFO").ok().filter(|path| !path.is_empty());
    let path = path
        .as_deref()
        .unwrap_or_else(|| uri.split('?').next().unwrap_or_default());
    let status = wcgi_request(&method, path).unwrap_or_else(|_| {
        eprintln!("WCGI request failed for a configured route");
        500
    });
    let reason = match status {
        200 => "OK",
        204 => "No Content",
        400 => "Bad Request",
        401 => "Unauthorized",
        404 => "Not Found",
        413 => "Content Too Large",
        503 => "Service Unavailable",
        _ => "Internal Server Error",
    };
    write!(
        io::stdout().lock(),
        "Status: {status} {reason}\r\nContent-Type: text/plain\r\n\r\n"
    )?;
    Ok(())
}

#[cfg(all(test, not(all(target_os = "wasi", target_env = "p2"))))]
mod tests {
    use super::*;

    const HEAD: &str = "1111111111111111111111111111111111111111";

    #[test]
    fn verifies_label_repository_and_signature() -> Result<()> {
        let repo = HashSet::from(["acme/project".to_owned()]);
        let mut event = json!({
            "action": "labeled", "number": 7, "label": {"name":"wrev"},
            "repository":{"full_name":"Acme/Project"},
            "pull_request":{"head":{"sha": HEAD},"base":{"sha": HEAD}}
        });
        let body = serde_json::to_vec(&event)?;
        let secret = b"test-webhook-secret";
        let mut mac = Hmac::<Sha256>::new_from_slice(secret)?;
        mac.update(&body);
        let signature = format!("sha256={}", hex::encode(mac.finalize().into_bytes()));
        assert!(signed(secret, &signature, &body));
        assert!(!signed(secret, &signature, b"tampered"));
        assert_eq!(labeled_target(&body, &repo, "wrev")?.unwrap().head, HEAD);
        assert!(labeled_target(&body, &HashSet::new(), "wrev")?.is_none());
        event["label"]["name"] = json!("wreview");
        assert!(labeled_target(&serde_json::to_vec(&event)?, &repo, "wrev")?.is_none());
        Ok(())
    }
}
