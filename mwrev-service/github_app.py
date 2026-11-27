"""Host-only scoped GitHub App authentication and API transport."""
import json
from http.client import HTTPSConnection, HTTPException
import re
import time

API_HOST = "api.github.com"
MAX_BYTES = 4 * 1024 * 1024
TIMEOUT = 12
MAX_COMMENT_PAGES = 20
SLUG = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")

class GitHubError(RuntimeError):
    pass

def positive(value):
    if type(value) is not int or not 0 < value < 2**63:
        raise ValueError("invalid GitHub identity")
    return value

def api(method, path, token, payload=None):
    if not path.startswith("/") or path.startswith("//") or not token:
        raise ValueError("invalid GitHub request")
    data = None if payload is None else json.dumps(payload).encode()
    conn = HTTPSConnection(API_HOST, timeout=TIMEOUT)
    try:
        conn.request(method, path, body=data, headers={
            "Authorization": "Bearer " + token,
            "Accept": "application/vnd.github+json",
            "Content-Type": "application/json",
            "User-Agent": "mwrev",
            "X-GitHub-Api-Version": "2022-11-28",
        })
        reply = conn.getresponse()
        if not 200 <= reply.status < 300:
            raise GitHubError(f"GitHub returned HTTP {reply.status}")
        raw = reply.read(MAX_BYTES + 1)
        if not raw or len(raw) > MAX_BYTES:
            raise GitHubError("invalid GitHub response size")
        return json.loads(raw)
    except (OSError, HTTPException, ValueError) as exc:
        raise GitHubError("GitHub request failed") from exc
    finally:
        conn.close()

def installation_token(app_id, key, installation, repo_id, *, write=False, request=api):
    import jwt
    t = int(time.time())
    signed = jwt.encode({"iat": t - 30, "exp": t + 480, "iss": str(positive(app_id))},
                        key, algorithm="RS256")
    permission = "write" if write else "read"
    permissions = {"pull_requests": permission}
    if not write:
        permissions["contents"] = "read"
    v = request("POST", f"/app/installations/{positive(installation)}/access_tokens",
                signed, {"repository_ids": [positive(repo_id)], "permissions": permissions})
    token = v.get("token") if isinstance(v, dict) else None
    if not isinstance(token, str) or not token:
        raise GitHubError("GitHub did not issue a scoped installation token")
    return token

class Pull:
    def __init__(self, repo, pr, token, request=api):
        if not SLUG.fullmatch(repo) or any(x in (".", "..") for x in repo.split("/")):
            raise ValueError("invalid configured repository")
        self.root = f"/repos/{repo}"
        self.pr = positive(pr)
        self.token = token
        self.request = request
        self.comments = f"{self.root}/issues/{pr}/comments"
        self.comment_route = f"{self.root}/issues/comments"

    def get_pull(self):
        return self.request("GET", f"{self.root}/pulls/{self.pr}", self.token)

    def list_comments(self):
        for page in range(1, MAX_COMMENT_PAGES + 1):
            items = self.request("GET", f"{self.comments}?per_page=100&page={page}", self.token)
            if not isinstance(items, list):
                raise GitHubError("invalid GitHub comment list")
            yield from items
            if len(items) < 100:
                return
        raise GitHubError("GitHub comment reconciliation exceeded scan limit")

    def get_comment(self, id):
        return self.request("GET", f"{self.comment_route}/{positive(id)}", self.token)

    def create_comment(self, body):
        result = self.request("POST", self.comments, self.token, {"body": body})
        return positive(result["id"])

    def edit_comment(self, id, body):
        return self.request("PATCH", f"{self.comment_route}/{positive(id)}",
                            self.token, {"body": body})
