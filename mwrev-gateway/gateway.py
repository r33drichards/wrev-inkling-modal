"""Host-owned transport for Wrev's Responses requests; not a public endpoint."""
from collections.abc import Iterator, Mapping
from http.client import HTTPException, HTTPSConnection
import json
import re
import time
from urllib.parse import urlsplit

MODEL = "thinkingmachines/Inkling-2-Mini"
MAX_REQUEST_BYTES = 16 * 1024 * 1024
MAX_RESPONSE_BYTES = 8 * 1024 * 1024
MAX_OUTPUT_TOKENS = 8192
MODEL_TIMEOUT_SECONDS = 180

class GatewayError(RuntimeError):
    """Safe to log; deliberately excludes request and upstream response bodies."""


def upstream_host(origin: str) -> str:
    parts = urlsplit(origin)
    host = parts.hostname or ""
    if (parts.scheme != "https" or parts.netloc != host
            or parts.path not in {"", "/"} or parts.query or parts.fragment
            or not re.fullmatch(r"[a-z0-9-]+\.modal\.run", host)):
        raise GatewayError("invalid configured Modal origin")
    return host


def prepare(payload: bytes) -> bytes:
    if not 0 < len(payload) <= MAX_REQUEST_BYTES:
        raise GatewayError("invalid model request size")
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
    limit = query.get("max_output_tokens", MAX_OUTPUT_TOKENS)
    if type(limit) is not int or limit <= 0:
        raise GatewayError("invalid max_output_tokens")
    query["max_output_tokens"] = min(limit, MAX_OUTPUT_TOKENS)
    try:
        encoded = json.dumps(query, ensure_ascii=True, allow_nan=False).encode()
    except (ValueError, RecursionError) as exc:
        raise GatewayError("invalid normalized model request") from exc
    if len(encoded) > MAX_REQUEST_BYTES:
        raise GatewayError("normalized model request too large")
    return encoded


def stream(origin: str, credential_headers: Mapping[str, str],
           payload: bytes) -> Iterator[bytes]:
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
    deadline = time.monotonic() + MODEL_TIMEOUT_SECONDS
    conn = HTTPSConnection(host, timeout=MODEL_TIMEOUT_SECONDS)
    try:
        conn.request("POST", "/v1/responses", body=body, headers=headers)
        response = conn.getresponse()
        if response.status != 200:
            raise GatewayError(f"model endpoint returned HTTP {response.status}")
        if response.getheader("Content-Type", "").split(";", 1)[0].strip().lower() != "text/event-stream":
            raise GatewayError("model endpoint did not return an event stream")
        total = 0
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                raise GatewayError("model stream exceeded time limit")
            if conn.sock is not None:
                conn.sock.settimeout(left)
            data = response.read1(min(65536, MAX_RESPONSE_BYTES - total + 1))
            if not data:
                break
            total += len(data)
            if total > MAX_RESPONSE_BYTES:
                raise GatewayError("model stream exceeded byte limit")
            yield data
    except (OSError, ValueError, HTTPException) as exc:
        raise GatewayError("model transport failed") from exc
    finally:
        conn.close()
