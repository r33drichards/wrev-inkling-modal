use anyhow::{Context, Result, anyhow, bail, ensure};
use config::{Config, ConfigError, Environment, Map};
use minimal_mcp_agent::quickjs_tools::ScriptLimits;
use std::{
    env,
    num::{NonZeroU32, NonZeroUsize},
    str::FromStr,
    sync::OnceLock,
    time::Duration,
};

fn from_environment(values: Map<String, String>) -> Result<Config, ConfigError> {
    Config::builder()
        .add_source(Environment::default().source(Some(values)))
        .build()
}

fn environment() -> Result<&'static Config> {
    static CONFIG: OnceLock<Result<Config, String>> = OnceLock::new();
    CONFIG
        .get_or_init(|| {
            // Load the application environment without retaining unrelated secrets.
            let mut values = Map::new();
            for (key, value) in env::vars_os() {
                let Ok(key) = key.into_string() else { continue };
                if !(matches!(key.as_str(), "OPENAI_MODEL" | "CHATGPT_ACCESS_TOKEN")
                    || key.starts_with("WREV_")
                        && key.bytes().all(|byte| {
                            byte.is_ascii_uppercase() || byte.is_ascii_digit() || byte == b'_'
                        }))
                {
                    continue;
                }
                let value = value
                    .into_string()
                    .map_err(|_| format!("{key} is not valid Unicode"))?;
                values.insert(key, value);
            }
            from_environment(values)
                .map_err(|_| "Could not load environment configuration".to_owned())
        })
        .as_ref()
        .map_err(|error| anyhow!("{error}"))
}

fn optional(settings: &Config, key: &str) -> Result<Option<String>> {
    match settings.get_string(&key.to_ascii_lowercase()) {
        Ok(value) => Ok(Some(value)),
        Err(ConfigError::NotFound(_)) => Ok(None),
        Err(_) => bail!("Invalid {key}"),
    }
}

pub fn chatgpt_token() -> Result<String> {
    match optional(environment()?, "CHATGPT_ACCESS_TOKEN")? {
        Some(token) => {
            ensure!(!token.trim().is_empty(), "CHATGPT_ACCESS_TOKEN is empty");
            Ok(token)
        }
        None => crate::auth::access_token(),
    }
}

pub fn required(key: &str) -> Result<String> {
    let value = optional(environment()?, key)?.with_context(|| format!("Set {key}"))?;
    ensure!(!value.trim().is_empty(), "{key} is empty");
    Ok(value)
}

fn setting<T: FromStr>(key: &str, default: T) -> Result<T> {
    setting_from(environment()?, key, default)
}

fn setting_from<T: FromStr>(settings: &Config, key: &str, default: T) -> Result<T> {
    match optional(settings, key)? {
        Some(value) => value.parse().map_err(|_| anyhow!("Invalid {key}")),
        None => Ok(default),
    }
}

fn seconds(key: &str, default: u32) -> Result<Duration> {
    let seconds = setting(
        key,
        NonZeroU32::new(default).expect("positive timeout default"),
    )?;
    Ok(Duration::from_secs(u64::from(seconds.get())))
}

pub fn model_timeout() -> Result<Duration> {
    seconds("WREV_MODEL_TIMEOUT_SECS", 180)
}

#[cfg(any(unix, feature = "mysql-backend"))]
pub fn auth_timeout() -> Result<Duration> {
    seconds("WREV_AUTH_TIMEOUT_SECS", 30)
}

#[cfg(unix)]
pub struct OAuthCallbackConfig {
    pub timeout: Duration,
    pub read_timeout: Duration,
}

#[cfg(unix)]
impl OAuthCallbackConfig {
    pub fn from_env() -> Result<Self> {
        Ok(Self {
            timeout: seconds("WREV_OAUTH_CALLBACK_TIMEOUT_SECS", 300)?,
            read_timeout: seconds("WREV_OAUTH_READ_TIMEOUT_SECS", 2)?,
        })
    }
}

fn positive_size(key: &str, default: usize) -> Result<usize> {
    Ok(setting(
        key,
        NonZeroUsize::new(default).expect("positive size default"),
    )?
    .get())
}

pub struct ReviewConfig {
    pub model: String,
    #[cfg(not(target_os = "wasi"))]
    pub github_timeout: Duration,
    pub script: ScriptLimits,
}

impl ReviewConfig {
    pub fn from_env() -> Result<Self> {
        let defaults = ScriptLimits::default();
        Ok(Self {
            model: required("OPENAI_MODEL")?,
            #[cfg(not(target_os = "wasi"))]
            github_timeout: seconds("WREV_GITHUB_TIMEOUT_SECS", 20)?,
            script: ScriptLimits {
                timeout: seconds(
                    "WREV_SCRIPT_TIMEOUT_SECS",
                    defaults.timeout.as_secs() as u32,
                )?,
                code_bytes: positive_size("WREV_SCRIPT_CODE_BYTES", defaults.code_bytes)?,
                output_bytes: positive_size("WREV_SCRIPT_OUTPUT_BYTES", defaults.output_bytes)?,
                memory_bytes: positive_size("WREV_SCRIPT_MEMORY_BYTES", defaults.memory_bytes)?,
                stack_bytes: positive_size("WREV_SCRIPT_STACK_BYTES", defaults.stack_bytes)?,
            },
        })
    }
}

#[cfg(not(all(target_os = "wasi", target_env = "p2")))]
pub struct WebhookConfig {
    pub label: String,
    pub body_bytes: usize,
}

#[cfg(not(all(target_os = "wasi", target_env = "p2")))]
impl WebhookConfig {
    pub fn from_env() -> Result<Self> {
        let label = setting("WREV_LABEL", String::from("wrev"))?;
        ensure!(!label.trim().is_empty(), "WREV_LABEL is empty");
        Ok(Self {
            label,
            body_bytes: positive_size("WREV_WEBHOOK_BODY_BYTES", 1024 * 1024)?,
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn uses_independent_overrides_and_rejects_invalid_values() -> Result<()> {
        let default = NonZeroU32::new(20).unwrap();
        let settings = from_environment(Map::from([
            ("WREV_OVERRIDE".to_owned(), "45".to_owned()),
            ("WREV_ZERO".to_owned(), "0".to_owned()),
            ("WREV_INVALID".to_owned(), "invalid".to_owned()),
            ("CHATGPT_ACCESS_TOKEN".to_owned(), "007".to_owned()),
        ]))?;
        assert_eq!(setting_from(&settings, "WREV_UNSET", default)?, default);
        assert_eq!(setting_from(&settings, "WREV_OVERRIDE", default)?.get(), 45);
        assert!(setting_from(&settings, "WREV_ZERO", default).is_err());
        assert!(setting_from(&settings, "WREV_INVALID", default).is_err());
        assert_eq!(
            optional(&settings, "CHATGPT_ACCESS_TOKEN")?.as_deref(),
            Some("007")
        );
        Ok(())
    }
}
