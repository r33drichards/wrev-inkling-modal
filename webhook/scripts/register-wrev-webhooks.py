#!/usr/bin/env python3
"""Register the wrev pull-request webhook on every writable repository you own."""
import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("url", help="HTTPS endpoint ending in /github/webhook")
    args = parser.parse_args()
    url = urllib.parse.urlsplit(args.url)
    if url.scheme != "https" or not url.hostname or url.path != "/github/webhook":
        parser.error("use an HTTPS URL ending in /github/webhook")

    secret = os.environ.get("WREV_WEBHOOK_SECRET")
    if not secret or len(secret) < 32:
        parser.error("set WREV_WEBHOOK_SECRET to the signing secret from Railway")
    token = subprocess.check_output(["gh", "auth", "token"], text=True).strip()

    def request(path, method="GET", payload=None):
        req = urllib.request.Request(
            "https://api.github.com" + path, method=method,
            data=json.dumps(payload).encode() if payload is not None else None,
            headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
                     "X-GitHub-Api-Version": "2022-11-28", "Content-Type": "application/json"},
        )
        for attempt in range(4):
            try:
                with urllib.request.urlopen(req, timeout=30) as response:
                    return json.load(response)
            except urllib.error.HTTPError as error:
                delay = error.headers.get("Retry-After")
                if error.code not in (403, 429) or not delay or attempt == 3:
                    raise
                time.sleep(min(int(delay), 300))

    def pages(path):
        separator = "&" if "?" in path else "?"
        for page in range(1, 10_000):
            result = request(f"{path}{separator}per_page=100&page={page}")
            yield from result
            if len(result) < 100:
                return
        raise RuntimeError("GitHub pagination limit exceeded")

    owner = request("/user")["login"].lower()
    repositories = [
        repo for repo in pages("/user/repos?affiliation=owner")
        if repo["owner"]["login"].lower() == owner and not repo["archived"] and not repo["disabled"]
    ]
    if not repositories:
        print("No writable repositories found for the authenticated account", file=sys.stderr)
        return 1
    # Fail early when the CLI credential lacks access to repository webhooks.
    request(f'/repos/{repositories[0]["full_name"]}/hooks?per_page=1')
    failures = 0
    for repo in repositories:
        name = repo["full_name"]
        route = f"/repos/{name}/hooks"
        try:
            matches = [hook for hook in pages(route) if hook["config"].get("url") == args.url]
            config = {"url": args.url, "content_type": "json", "secret": secret, "insecure_ssl": "0"}
            if matches:
                for hook in matches:
                    # Update the same hook so retries do not create duplicate deliveries.
                    request(f'{route}/{hook["id"]}', "PATCH",
                            {"config": config, "active": True, "events": ["pull_request"]})
                    time.sleep(1)
            else:
                request(route, "POST", {"name": "web", "config": config,
                                        "active": True, "events": ["pull_request"]})
                time.sleep(1)
            print(f"{name}: configured", flush=True)
        except urllib.error.HTTPError as error:
            failures += 1
            print(f"{name}: GitHub HTTP {error.code}", file=sys.stderr, flush=True)
    print(f"Configured {len(repositories) - failures}/{len(repositories)} owned repositories")
    return bool(failures)


if __name__ == "__main__":
    sys.exit(main())
