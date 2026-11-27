"""Host-owned transport for Wrev's Responses requests; not a public endpoint."""
from collections.abc import Iterator, Mapping
from http.client import HTTPException, HTTPSConnection
import json
import re
import time
from urllib.parse import urlsplit

MODEL = "thinkingmachines/Inkling-2-Mini"
class GatewayError(RuntimeError):
    """Safe to log; deliberately excludes request and upstream response bodies."""


def upstream_host(origin: str) -> str:
    parts = urlsplit(origin)
    host = parts.hostname or ""
    if (parts.scheme != "https" or parts.netloc != host
            or parts.path not in {"", "/"} or parts.query or parts.fragment
            or not re.fullmatch(r"[a-z0-9-]+(?:\.[a-z0-9-]+)?\.modal\.run", host)):
        raise GatewayError("invalid configured Modal origin")
    return host


def prepare(payload: bytes) -> bytes:
    try:
        query = json.loads(payload)
    except (ValueError, RecursionError) as exc:
        raise GatewayError("invalid model request JSON") from exc
    if not isinstance(query, dict) or not isinstance(query.get("input"), list):
        raise GatewayError("invalid model input")
    query["model"] = MODEL
    query["stream"] = True
    query["store"] = False
    # vLLM does not provide OpenAI's encrypted reasoning store.
    query.pop("include", None)
    try:
        encoded = json.dumps(query, ensure_ascii=True, allow_nan=False).encode()
    except (ValueError, RecursionError) as exc:
        raise GatewayError("invalid normalized model request") from exc
    return encoded


def stream(origin: str, credential_headers: Mapping[str, str],
           payload: bytes, *, deadline: float) -> Iterator[bytes]:
    """Call only the configured host and path, with no redirect handling.

    The trusted caller supplies proxy credentials. Guest headers never enter
    this interface. The embedding must enforce the overall review deadline.
    """
    host = upstream_host(origin)
    body = prepare(payload)
    if not all(credential_headers.get(k) for k in ("Modal-Key", "Modal-Secret")):
        raise GatewayError("missing Modal proxy credentials")
    headers = {"Modal-Key": credential_headers["Modal-Key"],
               "Modal-Secret": credential_headers["Modal-Secret"],
               "Content-Type": "application/json", "Accept": "text/event-stream"}
    conn = HTTPSConnection(host, timeout=time_left(deadline))
    try:
        conn.request("POST", "/v1/responses", body=body, headers=headers)
        response = conn.getresponse()
        if response.status != 200:
            raise GatewayError(f"model endpoint returned HTTP {response.status}")
        if response.getheader("Content-Type", "").split(";", 1)[0].strip().lower() != "text/event-stream":
            raise GatewayError("model endpoint did not return an event stream")
        while True:
            left = time_left(deadline)
            if conn.sock is not None:
                conn.sock.settimeout(left)
            data = response.read1(65536)
            if not data:
                break
            yield data
    except (OSError, ValueError, HTTPException) as exc:
        raise GatewayError("model transport failed") from exc
    finally:
        conn.close()


def completed_output(chunks: Iterator[bytes]):
    """Parse complete SSE output; truncated or failed streams never pass."""
    pending = bytearray()
    lines = []
    for chunk in chunks:
        pending.extend(chunk)
        lines_in_chunk = pending.split(b"\n")
        pending = lines_in_chunk.pop()
        for raw in lines_in_chunk:
            line = raw.rstrip(b"\r")
            if line.startswith(b"data:"):
                lines.append(line[5:].removeprefix(b" "))
            elif not line and lines:
                try:
                    event = json.loads(b"\n".join(lines))
                except (ValueError, RecursionError) as exc:
                    raise GatewayError("invalid model stream event") from exc
                lines.clear()
                if not isinstance(event, dict):
                    raise GatewayError("invalid model stream event")
                kind = event.get("type")
                if kind == "response.completed":
                    response = event.get("response")
                    output = response.get("output") if isinstance(response, dict) else None
                    if not isinstance(output, list):
                        raise GatewayError("completed model stream missing output")
                    return output
                if kind in ("error", "response.failed", "response.incomplete"):
                    raise GatewayError("model did not complete its response")
    raise GatewayError("model stream ended without a complete response")


def time_left(deadline):
    left = deadline - time.monotonic()
    if left <= 0:
        raise GatewayError("review deadline expired")
    return left


def model_call(origin, credentials, body, *, deadline):
    chunks = stream(origin, credentials, json.dumps(body).encode(), deadline=deadline)
    try:
        return completed_output(chunks)
    finally:
        chunks.close()

MODEL_STARTUP_SECONDS = 900

def wait_ready(origin, credentials, *, deadline):
    """Wait on the known Modal endpoint before starting a model turn."""
    host = upstream_host(origin)
    route = "/health"
    deadline = min(deadline, time.monotonic() + MODEL_STARTUP_SECONDS)
    headers = {key: credentials[key] for key in ("Modal-Key", "Modal-Secret")}
    while time.monotonic() < deadline:
        left = deadline - time.monotonic()
        conn = HTTPSConnection(host, timeout=min(155, max(1, left)))
        try:
            conn.request("GET", route, headers=headers)
            response = conn.getresponse()
            if response.status == 200:
                return
            if response.status == 303:
                dest = urlsplit(response.getheader("Location", ""))
                # Modal's long-request result URI; no arbitrary redirect or
                # credential forwarding to another origin/path.
                if ((dest.scheme, dest.netloc) not in {("", ""), ("https", host)}
                        or dest.path != "/health" or not dest.query or dest.fragment
                        or len(dest.query) > 2048):
                    raise GatewayError("unexpected readiness continuation")
                route = "/health?" + dest.query
            elif response.status not in (408, 429, 502, 503, 504):
                raise GatewayError(f"model readiness returned HTTP {response.status}")
        except (OSError, HTTPException):
            pass
        finally:
            conn.close()
        time.sleep(min(2, max(0, deadline-time.monotonic())))
    raise GatewayError("model did not become ready")

class ReviewModel:
    """No independent turn, time, token, or transfer cap."""
    def __init__(self, origin, credentials):
        self.origin = origin
        self.credentials = credentials

    def __call__(self, body, remaining_seconds):
        return model_call(self.origin, self.credentials, body,
                          deadline=time.monotonic() + remaining_seconds)
