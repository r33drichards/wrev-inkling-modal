"""Orchestrate the production review with explicit host-only boundaries."""
import asyncio
import json
import os
from pathlib import Path
import subprocess
import tempfile

from comment import start, finish
from github_app import Pull, installation_token
from gateway import wait_ready, ReviewModel
from wasm_runner import MAX_MODEL_TURNS, run, wasi_arguments

PREPARE_TIMEOUT = 120

async def sync(fn, *args, **kwargs):
    return await asyncio.to_thread(fn, *args, **kwargs)

class Worker:
    def __init__(self, store, config):
        self.store, self.config = store, config

    def pull(self, installation, repository, pr, *, write=False):
        repo = self.config.allowed[(installation, repository)]
        token = installation_token(self.config.app_id, self.config.private_key,
                                   installation, repository, write=write)
        return repo, token, Pull(repo, pr, token)

    @staticmethod
    def state(pr, head, base):
        labels = {label["name"] for label in pr.get("labels", [])}
        if pr.get("state") != "open" or "mwrev" not in labels:
            return "cancelled"
        if pr["head"]["sha"] != head or pr["base"]["sha"] != base:
            return "superseded"
        return "completed"

    def review(self, repo, pr, base, head, token, initial):
        cfg = self.config
        with tempfile.TemporaryDirectory(prefix="mwrev-review-") as temp:
            root = Path(temp)
            inputs = root / "input"
            inputs.mkdir()
            request = inputs / "request.json"
            request.write_text(json.dumps({
                "nwo": repo, "pr_number": pr, "base": base, "head": head,
                "title": initial.get("title"), "body": initial.get("body"),
            }))
            evidence = root / "evidence"
            subprocess.run([str(cfg.native), "prepare-review", str(request), str(evidence)],
                           check=True, timeout=PREPARE_TIMEOUT,
                           env={"PATH":"/usr/bin:/bin", "HOME":temp, "GIT_TERMINAL_PROMPT":"0",
                                "GIT_CONFIG_GLOBAL":"/dev/null", "GIT_CONFIG_NOSYSTEM":"1",
                                "WREV_GITHUB_TOKEN":token},
                           stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL)
            wait_ready(cfg.model_origin, cfg.model_auth)
            expected = {"repo":f"https://github.com/{repo}", "pr_number":pr, "base":base, "head":head}
            return run(wasi_arguments(cfg.wasmtime, cfg.component, evidence, inputs),
                       ReviewModel(cfg.model_origin, cfg.model_auth, MAX_MODEL_TURNS), expected)

    async def execute(self, review_id):
        claim = await self.store.claim(review_id)
        if not claim:
            return "duplicate"
        attempt, (installation, repository, pr, head, base) = claim
        try:
            repo, token, read = await sync(self.pull, installation, repository, pr)
            initial = await sync(read.get_pull)
            state = self.state(initial, head, base)
            if state != "completed":
                await self.store.finish(review_id, attempt, {"state":state})
                return state
            key = {"review_id":review_id, "head":head, "app_id":self.config.app_id}
            if self.config.comments:
                _, _, write = await sync(self.pull, installation, repository, pr, write=True)
                record = await self.store.reserve_start_comment(review_id, attempt)
                if record is None:
                    return "start_not_reserved"
                comment = await sync(start, write, **key, comment_id=record[0])
                if not await self.store.remember_comment(review_id, attempt, comment):
                    return "claim_lost"
            payload = await sync(self.review, repo, pr, base, head, token, initial)
            state = self.state(await sync(read.get_pull), head, base)
            ok = await self.store.finish(review_id, attempt, {
                "state":state, **({"review":payload} if state == "completed" else {}),
            })
            return state if ok else "claim_lost"
        except Exception:
            # No raw GitHub, git, or model exception crosses the service boundary.
            await self.store.release_attempt(review_id, attempt)
            return "retry_pending"

    async def publish(self):
        done = 0
        for id, installation, repo_id, pr, head, comment_id, result in await self.store.publications():
            try:
                _, _, write = await sync(self.pull, installation, repo_id, pr, write=True)
                await sync(finish, write, review_id=id, head=head, app_id=self.config.app_id,
                           comment_id=comment_id, outcome=result["state"],
                           findings_count=len(result.get("review", {}).get("findings", [])))
                await self.store.mark_published(id, comment_id)
                done += 1
            except Exception:
                continue
        return done
