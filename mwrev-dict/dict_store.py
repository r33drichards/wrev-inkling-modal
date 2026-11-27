"""Experimental single-writer coordinator; not wired to production ingress."""
import json
import time
import uuid

from store import UnconfirmedDispatch

SCHEMA = 1
KEY = "review-state-v1"
RETRY = 60
LEASE = 65 * 60
RETENTION = 7 * 24 * 3600
BATCH = 50
MODEL_ATTEMPTS = 2
DISPATCH_ATTEMPTS = 8


def review_id():
    return str(uuid.uuid4())


class Coordinator:
    """One operation at a time, one durable Dict write per transition.

    The caller must serialize requests within one non-concurrent process and
    ensure no second process/revision is using the same Dict. External effects
    (spawning, model calls, GitHub) happen outside the coordinator.
    """
    def __init__(self, data, clock=time.time):
        self.data = data
        self.clock = clock

    async def invoke(self, action, *args):
        op = self.OPERATIONS.get(action)
        if op is None:
            raise ValueError("unknown state operation")
        raw = await self.data.get.aio(KEY)
        state = json.loads(raw) if raw is not None else {
            "schema": SCHEMA, "jobs": {}, "deliveries": {}
        }
        if state["schema"] != SCHEMA:
            raise ValueError("incompatible review state")
        result, dirty = op(self, state, self.clock(), *args)
        if dirty:
            # A failure, including an ambiguous reply, is not an ack. A retry
            # loads the committed state if the write actually made it through.
            await self.data.put.aio(KEY, json.dumps(state, separators=(",", ":"),
                                                  allow_nan=False))
        return result

    def persist(self, state, now, delivery, installation, repository, pr, head, base):
        key = f"{installation}:{repository}:{delivery.lower()}"
        identity = [installation, repository, pr, head, base]
        if key in state["deliveries"]:
            id = state["deliveries"][key]
            if state["jobs"][id]["identity"] != identity:
                raise ValueError("delivery identity changed")
            return id, False
        id = next((id for id, job in state["jobs"].items()
                   if job["identity"] == identity and self.unpublished(job)), None)
        if id is None:
            id = review_id()
            state["jobs"][id] = {
                "identity":identity, "result":None, "published_at":None, "terminal_at":None,
                "dispatch_after":now, "dispatch_attempts":0, "dispatch_token":None, "call":None,
                "attempt":None, "attempts":0, "expires_at":0, "comment":None, "publish_after":now,
            }
        state["deliveries"][key] = id
        return id, True

    @staticmethod
    def unpublished(job):
        return (job["result"] is None or
                (job["published_at"] is None and (job["comment"] is not None
                                                 or job["result"]["state"] == "failed")))

    @staticmethod
    def ready(j, now):
        return (j["result"] is None and j["call"] is None and j["dispatch_after"] <= now
                and (j["attempt"] is None or j["expires_at"] <= now)
                and j["dispatch_attempts"] < DISPATCH_ATTEMPTS and j["attempts"] < MODEL_ATTEMPTS)

    def pending(self, state, now):
        return [id for id, j in sorted(state["jobs"].items(), key=lambda x: x[1]["dispatch_after"])
                if self.ready(j, now)][:BATCH], False

    def reserve_dispatch(self, state, now, id):
        j = state["jobs"][id]
        if j["call"] is not None or j["result"] is not None or (
                j["attempt"] is not None and j["expires_at"] > now):
            return {"accepted": True}, False
        if not self.ready(j, now):
            return {}, False
        attempt = review_id()
        j.update(dispatch_token=attempt, dispatch_after=now + RETRY,
                 dispatch_attempts=j["dispatch_attempts"] + 1)
        return {"token": attempt}, True

    def confirm_dispatch(self, state, now, id, token, call):
        j = state["jobs"].get(id)
        if j is None or j["dispatch_token"] != token:
            return False, False
        j["call"] = call
        return True, True

    def claim(self, state, now, id):
        j = state["jobs"].get(id)
        if (j is None or j["result"] is not None or j["attempts"] >= MODEL_ATTEMPTS
                or (j["attempt"] is not None and j["expires_at"] > now)):
            return None, False
        token = review_id()
        j.update(attempt=token, attempts=j["attempts"] + 1, expires_at=now + LEASE)
        return (token, j["identity"]), True

    @staticmethod
    def owns(j, now, token):
        return (j is not None and j["result"] is None and j["attempt"] == token
                and token is not None and now < j["expires_at"])

    def finish(self, state, now, id, token, result):
        j = state["jobs"].get(id)
        if not self.owns(j, now, token):
            return False, False
        j.update(result=result, terminal_at=now)
        return True, True

    def reserve_start_comment(self, state, now, id, token):
        j = state["jobs"].get(id)
        if not self.owns(j, now, token) or j["publish_after"] > now:
            return None, False
        j["publish_after"] = now + RETRY
        return [j["comment"]], True

    def remember_comment(self, state, now, id, token, comment):
        j = state["jobs"].get(id)
        if not self.owns(j, now, token):
            return False, False
        j.update(comment=comment, publish_after=now)
        return True, True

    def release_attempt(self, state, now, id, token):
        j = state["jobs"].get(id)
        if self.owns(j, now, token):
            j["expires_at"] = min(j["expires_at"], now + RETRY)
            return None, True
        return None, False

    def publications(self, state, now):
        return [(id, *j["identity"], j["comment"], j["result"])
                for id, j in state["jobs"].items()
                if j["result"] is not None and self.unpublished(j)
                and j["publish_after"] <= now][:BATCH], False

    def mark_published(self, state, now, id, comment):
        j = state["jobs"].get(id)
        if j and j["comment"] == comment and j["result"] is not None and j["published_at"] is None:
            j["published_at"] = now
            # GitHub is the long-term history. Retain revision/delivery identity,
            # not all the finding bodies, for the deduplication window.
            j["result"] = {"state": j["result"]["state"]}
            return True, True
        return False, False

    def outstanding_calls(self, state, now):
        return [(id, j["call"]) for id, j in state["jobs"].items()
                if j["result"] is None and j["call"] is not None and j["attempt"] is None][:BATCH], False

    def recover(self, state, now, ended):
        """Accept only verified terminal call IDs, never age out healthy queued calls."""
        ended = {tuple(pair) for pair in ended}
        dirty = False
        expired = set()
        for id, j in state["jobs"].items():
            if j["result"] is not None:
                done = j["published_at"] if j["published_at"] is not None else j["terminal_at"]
                if not self.unpublished(j) and now - done >= RETENTION:
                    expired.add(id)
                continue
            if j["attempt"] is not None and now < j["expires_at"]:
                continue
            if j["attempt"] is None and j["call"] is not None and (id, j["call"]) not in ended:
                continue
            if j["attempt"] is None and j["call"] is None and now < j["dispatch_after"]:
                continue
            if j["attempts"] >= MODEL_ATTEMPTS or j["dispatch_attempts"] >= DISPATCH_ATTEMPTS:
                j.update(result={"state":"failed"}, terminal_at=now)
                dirty = True
            elif j["call"] is not None or j["attempt"] is not None:
                j.update(call=None, dispatch_after=now, attempt=None, dispatch_token=None)
                dirty = True
        if expired:
            for id in expired:
                del state["jobs"][id]
            state["deliveries"] = {k: id for k, id in state["deliveries"].items()
                                   if id not in expired}
        return None, dirty or bool(expired)

    OPERATIONS = {f.__name__: f for f in (
        persist, pending, reserve_dispatch, confirm_dispatch, claim, finish,
        reserve_start_comment, remember_comment, release_attempt, publications,
        mark_published, outstanding_calls, recover,
    )}


class DictStore:
    """Worker-facing adapter. RPC targets a single serialized coordinator."""
    def __init__(self, invoke):
        self.call = invoke

    async def persist(self, review):
        return await self.call("persist", review.delivery, review.installation,
                               review.repository, review.number, review.head, review.base)

    async def dispatch(self, id, spawn):
        r = await self.call("reserve_dispatch", id)
        if r.get("accepted"):
            return
        token = r.get("token")
        if token is None:
            raise UnconfirmedDispatch()
        call = await spawn(id)
        if not call or not await self.call("confirm_dispatch", id, token, call):
            raise UnconfirmedDispatch()

    async def claim(self, id):
        return await self.call("claim", id)

    async def finish(self, id, token, result):
        return await self.call("finish", id, token, result)

    async def reserve_start_comment(self, id, token):
        return await self.call("reserve_start_comment", id, token)

    async def remember_comment(self, id, token, comment):
        return await self.call("remember_comment", id, token, comment)

    async def pending(self):
        return await self.call("pending")

    async def publications(self):
        return await self.call("publications")

    async def mark_published(self, id, comment):
        return await self.call("mark_published", id, comment)

    async def release_attempt(self, id, token):
        return await self.call("release_attempt", id, token)

    async def recover_abandoned(self, confirmed_terminal=None):
        ended = []
        if confirmed_terminal is not None:
            for id, call in await self.call("outstanding_calls"):
                if await confirmed_terminal(call):
                    ended.append((id, call))
        return await self.call("recover", ended)
