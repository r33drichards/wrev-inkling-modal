use anyhow::{Context, Result, bail};
#[cfg(not(all(target_os = "wasi", target_env = "p2")))]
pub use reqwest::blocking::Client;
#[cfg(all(target_os = "wasi", target_env = "p2"))]
pub type Client = std::time::Duration;
use serde_json::{Value, json};
#[cfg(any(test, not(all(target_os = "wasi", target_env = "p2", feature = "mwrev-host-stdio"))))]
use std::io::BufRead;
#[cfg(not(all(target_os = "wasi", target_env = "p2", feature = "mwrev-host-stdio")))]
use std::io::BufReader;

#[cfg(not(all(target_os = "wasi", target_env = "p2")))]
pub fn client() -> Result<Client> {
    Ok(Client::builder()
        .redirect(reqwest::redirect::Policy::none())
        .timeout(crate::config::model_timeout()?)
        .build()?)
}

#[cfg(all(target_os = "wasi", target_env = "p2"))]
pub fn client() -> Result<Client> {
    crate::config::model_timeout()
}

#[cfg(not(all(target_os = "wasi", target_env = "p2", feature = "mwrev-host-stdio")))]
pub fn models(token: &str) -> Result<Vec<Value>> {
    #[cfg(not(all(target_os = "wasi", target_env = "p2")))]
    let response = client()?
        .get("https://api.openai.com/v1/models")
        .bearer_auth(token)
        .send()?
        .error_for_status()?;
    #[cfg(not(all(target_os = "wasi", target_env = "p2")))]
    let value: Value = response.json()?;
    #[cfg(all(target_os = "wasi", target_env = "p2", not(feature = "mwrev-host-stdio")))]
    let value: Value = serde_json::from_reader(BufReader::new(crate::wasi_http::request(
        wasi::http::types::Method::Get,
        "api.openai.com",
        "/v1/models",
        token,
        None,
        client()?,
    )?))?;
    Ok(value["models"]
        .as_array()
        .context("Missing models catalog")?
        .iter()
        .filter(|m| m["visibility"] == "list")
        .cloned()
        .collect())
}

#[cfg(all(target_os = "wasi", target_env = "p2", feature = "mwrev-host-stdio"))]
pub fn models(_token: &str) -> Result<Vec<Value>> {
    bail!("Model discovery is not available in the host-stdio build");
}

#[cfg(all(target_os = "wasi", target_env = "p2", feature = "mwrev-host-stdio"))]
pub fn host_stdio() -> bool {
    std::env::var("WREV_MODEL_TRANSPORT").as_deref() == Ok("host-stdio")
}

#[cfg(all(target_os = "wasi", target_env = "p2", feature = "mwrev-host-stdio"))]
pub fn write_host_frame(frame: &Value) -> Result<()> {
    use std::io::Write;
    let mut out = std::io::stdout().lock();
    serde_json::to_writer(&mut out, frame)?;
    out.write_all(b"\n")?;
    out.flush()?;
    Ok(())
}

#[cfg(all(target_os = "wasi", target_env = "p2", feature = "mwrev-host-stdio"))]
fn prompt_host(body: Value) -> Result<Vec<Value>> {
    use std::io::{BufRead, Read};
    write_host_frame(&json!({"type": "model.request", "body": body}))?;
    let mut line = Vec::new();
    let limit = 8 * 1024 * 1024;
    std::io::stdin().lock().take(limit + 1).read_until(b'\n', &mut line)?;
    anyhow::ensure!(line.len() <= limit as usize && line.ends_with(b"\n"),
        "Missing or oversized model reply");
    let value: Value = serde_json::from_slice(&line).context("Invalid host model reply")?;
    anyhow::ensure!(value["type"] == "model.response", "Host model call failed");
    value["output"].as_array().cloned().context("Host model reply missing output")
}

pub fn prompt(
    client: &Client,
    token: &str,
    model: &str,
    messages: &[Value],
    tools: &[Value],
    instructions: &str,
) -> Result<Vec<Value>> {
    let body = json!({"model": model, "input": messages,
        "tools": tools, "instructions": instructions, "store": false, "stream": true,
        "include": ["reasoning.encrypted_content"]});
    #[cfg(all(target_os = "wasi", target_env = "p2", feature = "mwrev-host-stdio"))]
    {
        let _ = (client, token);
        anyhow::ensure!(host_stdio(), "Host-stdio transport must be explicitly enabled");
        return prompt_host(body);
    }
    #[cfg(not(all(target_os = "wasi", target_env = "p2")))]
    let response = client
        .post("https://api.openai.com/v1/responses")
        .bearer_auth(token)
        .json(&body)
        .send()?
        .error_for_status()?;
    #[cfg(all(target_os = "wasi", target_env = "p2", not(feature = "mwrev-host-stdio")))]
    let response = crate::wasi_http::request(
        wasi::http::types::Method::Post,
        "api.openai.com",
        "/v1/responses",
        token,
        Some(&serde_json::to_vec(&body)?),
        *client,
    )?;
    #[cfg(not(all(target_os = "wasi", target_env = "p2", feature = "mwrev-host-stdio")))]
    {
        read_completed(BufReader::new(response))
    }
}

// SSE data can span multiple lines. Only a completed response is a successful turn.
#[cfg(any(test, not(all(target_os = "wasi", target_env = "p2", feature = "mwrev-host-stdio"))))]
fn read_completed(reader: impl BufRead) -> Result<Vec<Value>> {
    let mut data = String::new();
    for line in reader.lines().chain(std::iter::once(Ok(String::new()))) {
        let line = line?;
        if line.is_empty() {
            if data.is_empty() {
                continue;
            }
            let event: Value =
                serde_json::from_str(&data).context("Invalid Responses stream event")?;
            data.clear();
            match event["type"].as_str() {
                Some("response.completed") => {
                    return Ok(event["response"]["output"]
                        .as_array()
                        .context("Completed response missing output")?
                        .clone());
                }
                Some("response.failed" | "response.incomplete" | "error") => {
                    // Do not print the whole event: it can contain conversation data.
                    let kind = event["type"].as_str().unwrap();
                    let code = event["response"]["error"]["code"]
                        .as_str()
                        .or(event["code"].as_str())
                        .or(event["error"]["code"].as_str())
                        .or(event["error"]["type"].as_str())
                        .or(event["response"]["incomplete_details"]["reason"].as_str())
                        .filter(|code| {
                            code.len() <= 100
                                && code.bytes().all(|b| b.is_ascii_alphanumeric() || b == b'_')
                        })
                        .unwrap_or("unknown");
                    bail!("Responses stream ended with {kind} ({code})");
                }
                _ => {}
            }
        } else if let Some(value) = line.strip_prefix("data:") {
            if !data.is_empty() {
                data.push('\n');
            }
            data.push_str(value.strip_prefix(' ').unwrap_or(value));
        }
    }
    bail!("Stream interrupted before response.completed; not retrying automatically")
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn reads_completed_sse() {
        let s = "event: response.completed\ndata: {\ndata: \"type\":\"response.completed\",\"response\":{\"output\":[]}}\n\n";
        assert!(read_completed(s.as_bytes()).unwrap().is_empty());
    }
    #[test]
    fn reports_nested_error_codes_without_the_response_body() {
        let data = b"data: {\"type\":\"error\",\"error\":{\"code\":\"subscription_sharing_usage_limit_exceeded\",\"message\":\"private\"}}\n\n";
        let error = read_completed(&data[..]).unwrap_err().to_string();
        assert!(error.contains("subscription_sharing_usage_limit_exceeded"));
        assert!(!error.contains("private"));
    }

    #[test]
    fn fails_closed_for_interruption_or_incomplete() {
        for s in [
            "",
            "data: {\"type\":\"response.incomplete\"}\n\n",
            "data: {\"type\":\"response.failed\"}\n\n",
        ] {
            assert!(read_completed(s.as_bytes()).is_err());
        }
    }
}
