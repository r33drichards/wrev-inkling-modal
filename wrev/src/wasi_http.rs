use anyhow::{Context, Result, anyhow, ensure};
use std::{io, time::Duration};
use wasi::{
    clocks::monotonic_clock::{self, Instant},
    http::{
        outgoing_handler,
        types::{
            Fields, IncomingBody, Method, OutgoingBody, OutgoingRequest, RequestOptions, Scheme,
        },
    },
    io::{
        poll::{self, Pollable},
        streams::{InputStream, StreamError},
    },
};

pub struct Response {
    stream: InputStream,
    _body: IncomingBody,
    deadline: Instant,
}

impl io::Read for Response {
    fn read(&mut self, buffer: &mut [u8]) -> io::Result<usize> {
        if buffer.is_empty() {
            return Ok(0);
        }
        let readable = self.stream.subscribe();
        loop {
            wait(&readable, self.deadline)?;
            match self.stream.read(buffer.len() as u64) {
                Ok(data) if !data.is_empty() => {
                    buffer[..data.len()].copy_from_slice(&data);
                    return Ok(data.len());
                }
                Ok(_) => {}
                Err(StreamError::Closed) => return Ok(0),
                Err(_) => return Err(io::Error::other("WASI HTTP body read failed")),
            }
        }
    }
}

fn wait(resource: &Pollable, deadline: Instant) -> io::Result<()> {
    let timer = monotonic_clock::subscribe_instant(deadline);
    poll::poll(&[resource, &timer]);
    if monotonic_clock::now() >= deadline {
        return Err(io::Error::new(
            io::ErrorKind::TimedOut,
            "WASI HTTP request timed out",
        ));
    }
    Ok(())
}

pub fn request(
    method: Method,
    authority: &str,
    path: &str,
    token: &str,
    body: Option<&[u8]>,
    timeout: Duration,
) -> Result<Response> {
    ensure!(!timeout.is_zero(), "WASI HTTP request timed out");
    let nanos = u64::try_from(timeout.as_nanos()).unwrap_or(u64::MAX);
    let deadline = monotonic_clock::now().saturating_add(nanos);
    let mut headers = vec![
        (
            "authorization".into(),
            format!("Bearer {token}").into_bytes(),
        ),
        ("user-agent".into(), b"wrev".to_vec()),
    ];
    if body.is_some() {
        headers.push(("content-type".into(), b"application/json".to_vec()));
    }
    let fields = Fields::from_list(&headers).map_err(|_| anyhow!("Invalid HTTP headers"))?;
    let request = OutgoingRequest::new(fields);
    request
        .set_method(&method)
        .map_err(|_| anyhow!("Invalid HTTP method"))?;
    request
        .set_scheme(Some(&Scheme::Https))
        .map_err(|_| anyhow!("Invalid HTTP scheme"))?;
    request
        .set_authority(Some(authority))
        .map_err(|_| anyhow!("Invalid HTTP authority"))?;
    request
        .set_path_with_query(Some(path))
        .map_err(|_| anyhow!("Invalid HTTP path"))?;
    let outgoing = body
        .map(|_| {
            request
                .body()
                .map_err(|_| anyhow!("Missing HTTP request body"))
        })
        .transpose()?;
    let options = RequestOptions::new();
    options
        .set_connect_timeout(Some(nanos))
        .map_err(|_| anyhow!("Invalid HTTP timeout"))?;
    let future = outgoing_handler::handle(request, Some(options)).context("Start HTTP request")?;
    if let Some((outgoing, mut remaining)) = outgoing.zip(body) {
        {
            let stream = outgoing
                .write()
                .map_err(|_| anyhow!("Missing HTTP output stream"))?;
            let writable = stream.subscribe();
            while !remaining.is_empty() {
                wait(&writable, deadline)?;
                let count = stream
                    .check_write()
                    .map_err(|_| anyhow!("HTTP body is closed"))?;
                let count = remaining
                    .len()
                    .min(usize::try_from(count).unwrap_or(usize::MAX));
                if count > 0 {
                    stream
                        .write(&remaining[..count])
                        .map_err(|_| anyhow!("HTTP body write failed"))?;
                    remaining = &remaining[count..];
                }
            }
        }
        OutgoingBody::finish(outgoing, None).context("Finish HTTP request body")?;
    }
    {
        let ready = future.subscribe();
        wait(&ready, deadline)?;
    }
    let response = future
        .get()
        .context("HTTP response is pending")?
        .map_err(|_| anyhow!("HTTP response already consumed"))?
        .context("Send HTTP request")?;
    let status = response.status();
    ensure!(
        (200..300).contains(&status),
        "HTTP {status} from {authority}"
    );
    let body = response
        .consume()
        .map_err(|_| anyhow!("Missing HTTP response body"))?;
    let stream = body
        .stream()
        .map_err(|_| anyhow!("Missing HTTP input stream"))?;
    Ok(Response {
        stream,
        _body: body,
        deadline,
    })
}
