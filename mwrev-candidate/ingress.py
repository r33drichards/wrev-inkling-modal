"""Signed GitHub App ingress. No GitHub, model, or credential operations."""
import asyncio
import hmac
import json
import re
from dataclasses import dataclass

from github_app import AccountScope, LABELS, trigger_labels
SHA = re.compile(r"[a-f0-9]{40}")
DELIVERY = re.compile(r"[a-f0-9-]{1,64}", re.I)
MAX_BODY = 512 * 1024

@dataclass(frozen=True)
class Review:
    delivery: str
    installation: int
    repository: int
    number: int
    head: str
    base: str

def extract(value, delivery, allowed):
    if not isinstance(value, dict):
        raise ValueError("object required")
    if value.get("action") not in ("labeled", "synchronize"):
        return None
    install = value["installation"]["id"]
    repo = value["repository"]["id"]
    if not allowed.accepts(install, repo, value["repository"]):
        return None
    pr = value["pull_request"]
    if pr["state"] != "open":
        return None
    if value["action"] == "labeled":
        if value["label"]["name"] not in LABELS:
            return None
    elif not trigger_labels(pr):
        return None
    number, head, base = pr["number"], pr["head"]["sha"], pr["base"]["sha"]
    if type(number) is not int or number <= 0 or not SHA.fullmatch(head) or not SHA.fullmatch(base):
        raise ValueError("invalid review identity")
    return Review(delivery, install, repo, number, head, base)

def app(secret, allowed, persist, dispatch):
    """persist() returns a durable id or None; dispatch() confirms Modal acceptance."""
    if not secret or not isinstance(allowed, AccountScope):
        raise ValueError("webhook secret and explicit account scope required")

    async def ingress(scope, receive, send):
        if scope["type"] != "http":
            return

        async def reply(status):
            await send({"type": "http.response.start", "status": status,
                        "headers": [(b"content-length", b"0")]})
            await send({"type": "http.response.body", "body": b""})

        if scope["path"] != "/github/webhook":
            return await reply(404)
        if scope["method"] != "POST":
            return await reply(405)
        headers = {}
        for key, value in scope["headers"]:
            if key in (b"x-hub-signature-256", b"x-github-delivery", b"x-github-event"):
                if key in headers:
                    return await reply(400)
                headers[key] = value
        body = bytearray()
        while True:
            msg = await receive()
            if msg["type"] == "http.disconnect":
                return
            chunk = msg.get("body", b"")
            if len(body) + len(chunk) > MAX_BODY:
                return await reply(413)
            body.extend(chunk)
            if not msg.get("more_body"):
                break
        expected = b"sha256=" + hmac.digest(secret, body, "sha256").hex().encode("ascii")
        if not hmac.compare_digest(expected, headers.get(b"x-hub-signature-256", b"")):
            return await reply(401)
        if headers.get(b"x-github-event") != b"pull_request":
            return await reply(204)
        try:
            delivery = headers.get(b"x-github-delivery", b"").decode("ascii")
            if not DELIVERY.fullmatch(delivery):
                raise ValueError("invalid delivery")
            intent = extract(json.loads(body), delivery, allowed)
        except (KeyError, TypeError, AttributeError, ValueError, RecursionError):
            return await reply(400)
        if intent is None:
            return await reply(204)

        try:
            async with asyncio.timeout(8):
                review_id = await persist(intent)
                if review_id is not None:
                    await dispatch(review_id)
        except Exception:
            # Do not log signed payloads, settings, or dependency exception text.
            return await reply(503)
        return await reply(202)

    return ingress
