#[cfg(unix)]
mod auth;
#[cfg(not(unix))]
#[path = "auth_headless.rs"]
mod auth;
mod benchmark;
mod config;
#[cfg(feature = "mysql-backend")]
mod hosted_auth;
mod model;
mod review;
#[cfg(all(target_os = "wasi", target_env = "p2", not(feature = "mwrev-host-stdio")))]
mod wasi_http;

use anyhow::{Result, bail};
use std::env;

fn main() -> Result<()> {
    match env::args().nth(1).as_deref() {
        #[cfg(not(all(target_os = "wasi", target_env = "p2")))]
        Some("login") => auth::login(),
        #[cfg(not(all(target_os = "wasi", target_env = "p2")))]
        Some("logout") => auth::logout(),
        Some("models") => {
            for model in model::models(&config::chatgpt_token()?)? {
                println!("{}", model["slug"].as_str().unwrap_or(""));
            }
            Ok(())
        }
        #[cfg(not(all(target_os = "wasi", target_env = "p2")))]
        Some("wcgi") => review::wcgi(),
        #[cfg(not(target_os = "wasi"))]
        Some("review") => review::manual(),
        #[cfg(target_os = "wasi")]
        Some("review") => benchmark::run_mounted(),
        #[cfg(not(target_os = "wasi"))]
        Some("prepare-review") => benchmark::prepare(),
        Some("review-snapshot") => benchmark::run_mounted(),
        #[cfg(not(target_os = "wasi"))]
        Some("reviewbench") => benchmark::run(),
        None | Some("-h" | "--help") => {
            #[cfg(not(target_os = "wasi"))]
            println!(
                "wrev [login | logout | models | wcgi | review OWNER/REPO PR_NUMBER | reviewbench CHECKOUT PR_JSON | prepare-review REQUEST_JSON MOUNT_ROOT | review-snapshot REQUEST_JSON [MOUNT_ROOT]]"
            );
            #[cfg(all(target_os = "wasi", not(target_env = "p2")))]
            println!("wrev [login | logout | models | wcgi | review REQUEST_JSON [MOUNT_ROOT]]");
            #[cfg(all(target_os = "wasi", target_env = "p2"))]
            println!("wrev [models | review REQUEST_JSON [MOUNT_ROOT]]");
            Ok(())
        }
        Some(other) => bail!("Unknown command: {other}"),
    }
}
