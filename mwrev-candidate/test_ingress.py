import hmac
import json
import unittest
from ingress import app
from github_app import AccountScope

SECRET = b"fixture-only"
SHA = "1" * 40

def payload(label="mwrev"):
    return dict(action="labeled", label={"name": label}, installation={"id": 7},
                repository={"id": 11, "full_name": "owner/repo", "owner": {"login": "owner"}}, pull_request={"state": "open", "draft": True,
                "number": 3, "head": {"sha": SHA}, "base": {"sha": "2"*40}})

class IngressTest(unittest.IsolatedAsyncioTestCase):
    async def post(self, body, signature=None, fail=False):
        effects = self.effects = []
        async def persist(intent):
            effects.append(("persist", intent))
            return 42
        async def dispatch(id):
            effects.append(("dispatch", id))
            if fail:
                raise RuntimeError("unknown spawn result")
        signature = signature or b"sha256=" + hmac.digest(SECRET, body, "sha256").hex().encode()
        events = []
        async def send(msg):
            events.append(msg)
        async def receive():
            return {"type": "http.request", "body": body}
        await app(SECRET, AccountScope(7, "owner"), persist, dispatch)(
            {"type": "http", "method": "POST", "path": "/github/webhook", "headers": [
                (b"x-hub-signature-256", signature), (b"x-github-event", b"pull_request"),
                (b"x-github-delivery", b"deadbeef")]}, receive, send)
        return events[0]["status"]

    async def test_mwrev_draft_commits_first(self):
        self.assertEqual(await self.post(json.dumps(payload()).encode()), 202)
        self.assertEqual([e[0] for e in self.effects], ["persist", "dispatch"])
        self.assertEqual(self.effects[0][1].head, SHA)
        self.assertEqual(self.effects[1][1], 42)

    async def test_mrev_on_another_owned_repo(self):
        data = payload("mrev")
        data["repository"] = {"id": 95, "full_name": "OWNER/other", "owner": {"login": "Owner"}}
        self.assertEqual(await self.post(json.dumps(data).encode()), 202)
        self.assertEqual(self.effects[0][1].repository, 95)

    async def test_signed_other_owner_never_dispatches(self):
        data = payload()
        data["repository"] = {"id": 11, "full_name": "other/repo", "owner": {"login": "other"}}
        self.assertEqual(await self.post(json.dumps(data).encode()), 204)
        self.assertEqual(self.effects, [])

    async def test_wrong_label(self):
        self.assertEqual(await self.post(json.dumps(payload("wrev")).encode()), 204)
        self.assertEqual(self.effects, [])

    async def test_wrong_installation(self):
        data = payload()
        data["installation"]["id"] = 8
        self.assertEqual(await self.post(json.dumps(data).encode()), 204)
        self.assertEqual(self.effects, [])

    async def test_bad_signature(self):
        self.assertEqual(await self.post(b"{}", b"sha256=invalid"), 401)
        self.assertEqual(self.effects, [])

    async def test_cannot_confirm_dispatch(self):
        self.assertEqual(await self.post(json.dumps(payload()).encode(), fail=True), 503)
        self.assertEqual([e[0] for e in self.effects], ["persist", "dispatch"])

    async def test_signed_invalid_json(self):
        self.assertEqual(await self.post(b"{broken"), 400)
        self.assertEqual(self.effects, [])

    async def test_oversize(self):
        self.assertEqual(await self.post(b"x"*524289), 413)
        self.assertEqual(self.effects, [])

    async def test_synchronize_with_mwrev(self):
        data = payload()
        data["action"] = "synchronize"
        data["pull_request"]["labels"] = [{"name":"mwrev"}]
        self.assertEqual(await self.post(json.dumps(data).encode()), 202)

class RepairTest(unittest.IsolatedAsyncioTestCase):
    async def test_one_failure_does_not_stop_remaining_dispatches(self):
        from repair import RepairResult, repair

        seen = []
        class Store:
            async def pending(self):
                return [1, 2]
            async def dispatch(self, review_id, spawn):
                await spawn(review_id)
        async def spawn(review_id):
            seen.append(review_id)
            if review_id == 1:
                raise RuntimeError("ambiguous")
        self.assertEqual(await repair(Store(), spawn), RepairResult(1, 1))
        self.assertEqual(seen, [1, 2])
