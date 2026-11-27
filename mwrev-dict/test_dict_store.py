import asyncio
import hmac
import json
from dataclasses import replace
from types import SimpleNamespace
import unittest

from dict_store import Coordinator, DictStore, KEY, LEASE, RETENTION
from ingress import Review, app
from github_app import AccountScope
from store import UnconfirmedDispatch


class Data:
    def __init__(self):
        self.raw = None
        self.fail = False
        self.get = SimpleNamespace(aio=self.read)
        self.put = SimpleNamespace(aio=self.write)

    async def read(self, key):
        assert key == KEY
        await asyncio.sleep(0)
        return self.raw

    async def write(self, key, value):
        assert key == KEY
        await asyncio.sleep(0)
        self.raw = value
        if self.fail:
            self.fail = False
            raise OSError("commit reply lost")


class DictStoreTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.data = Data()
        self.time = 1
        self.mutex = asyncio.Lock()
        self.store = DictStore(self.invoke)
        self.event = Review("deadbeef", 7, 11, 4, "a"*40, "b"*40)

    async def invoke(self, *args):
        async with self.mutex:
            # Recreate the coordinator to model a process restart every time.
            return await Coordinator(self.data, lambda:self.time).invoke(*args)

    async def spawned(self, id):
        return "fc-fixture"

    async def test_duplicate_deliveries_and_claims(self):
        unique = [self.event] * 10 + [replace(self.event, delivery="aaaa")]
        ids = await asyncio.gather(*(self.store.persist(e) for e in unique))
        self.assertEqual(len(set(ids)), 1)
        id = ids[0]
        dispatch = await asyncio.gather(*(self.store.dispatch(id,self.spawned) for _ in ids),
                                        return_exceptions=True)
        self.assertTrue(all(r is None or isinstance(r, UnconfirmedDispatch) for r in dispatch))
        claims = await asyncio.gather(*(self.store.claim(id) for _ in ids))
        self.assertEqual(sum(c is not None for c in claims), 1)
        with self.assertRaisesRegex(ValueError, "identity changed"):
            await self.store.persist(replace(self.event, number=5))

    async def test_ambiguous_store_write_does_not_ack_and_redelivery_is_deduped(self):
        self.data.fail = True
        with self.assertRaises(OSError):
            await self.store.persist(self.event)
        id = await self.store.persist(self.event)
        self.assertEqual(await self.store.pending(), [id])

    async def test_spawn_reply_lost_then_actual_worker_claims(self):
        id = await self.store.persist(self.event)
        async def ambiguous(_):
            raise TimeoutError()
        with self.assertRaises(TimeoutError):
            await self.store.dispatch(id, ambiguous)
        token, _ = await self.store.claim(id)
        self.time += 70
        await self.store.recover_abandoned()
        self.assertEqual(await self.store.pending(), [])
        self.assertTrue(await self.store.finish(id, token, {"state":"cancelled"}))

    async def test_queued_fifth_review_does_not_burn_retry(self):
        id = await self.store.persist(self.event)
        await self.store.dispatch(id, self.spawned)
        self.time += 60 * 60
        async def still_pending(call):
            self.assertEqual(call, "fc-fixture")
            return False
        await self.store.recover_abandoned(still_pending)
        self.assertEqual(await self.store.pending(), [])
        async def actually_ended(call):
            return call == "fc-fixture"
        await self.store.recover_abandoned(actually_ended)
        self.assertEqual(await self.store.pending(), [id])

    async def test_lost_worker_is_fenced_and_terminal_failure_can_publish(self):
        id = await self.store.persist(self.event)
        await self.store.dispatch(id, self.spawned)
        token, _ = await self.store.claim(id)
        self.assertEqual(await self.store.reserve_start_comment(id, token), [None])
        self.assertTrue(await self.store.remember_comment(id, token, 41))
        self.time += LEASE + 1
        await self.store.recover_abandoned()
        self.assertEqual(await self.store.pending(), [id])
        retry, _ = await self.store.claim(id)
        self.assertNotEqual(token, retry)
        self.assertFalse(await self.store.finish(id, token, {"state":"completed"}))
        self.assertEqual(await self.store.reserve_start_comment(id, retry), [41])
        await self.store.release_attempt(id, retry)
        self.time += 61
        await self.store.recover_abandoned()
        pub, = await self.store.publications()
        self.assertEqual(pub[-2:], (41, {"state":"failed"}))
        self.assertEqual(await self.store.publications(), [pub])
        self.assertTrue(await self.store.mark_published(id, 41))
        self.assertFalse(await self.store.mark_published(id, 41))
        self.assertEqual(await self.store.publications(), [])
        self.assertEqual(await self.store.persist(self.event), id)
        next_id = await self.store.persist(replace(self.event, delivery="d00d"))
        self.assertNotEqual(next_id, id)

    async def test_prune_only_terminal_history(self):
        done = await self.store.persist(self.event)
        token, _ = await self.store.claim(done)
        self.assertTrue(await self.store.finish(done, token, {"state":"cancelled"}))
        live = await self.store.persist(replace(self.event, delivery="beef", number=5))
        self.time += RETENTION + 1
        await self.store.recover_abandoned()
        self.assertEqual(await self.store.pending(), [live])
        self.assertNotEqual(await self.store.persist(self.event), done)


    async def test_signed_ingress_persists_before_dispatch_and_recovers_lost_spawn(self):
        payload = json.dumps({"action":"labeled","label":{"name":"mrev"},
            "installation":{"id":7},
            "repository":{"id":11,"full_name":"owner/repo","owner":{"login":"owner"}},
            "pull_request":{"state":"open","number":4,"head":{"sha":"a"*40},
                            "base":{"sha":"b"*40}}}).encode()
        secret = b"synthetic-only"
        found = []
        async def broken_spawn(id):
            found.append(id)
            self.assertIn(id, self.data.raw)
            raise TimeoutError()
        receiver = app(secret, AccountScope(7,"owner"), self.store.persist,
                       lambda id: self.store.dispatch(id,broken_spawn))
        messages = []
        async def receive():
            return {"type":"http.request","body":payload}
        async def send(message):
            messages.append(message)
        await receiver({"type":"http","path":"/github/webhook","method":"POST","headers":[
            (b"x-github-event",b"pull_request"),(b"x-github-delivery",b"deadbeef"),
            (b"x-hub-signature-256",b"sha256="+hmac.digest(secret,payload,"sha256").hex().encode())
        ]}, receive, send)
        self.assertEqual(messages[0]["status"],503)
        self.time += 61
        await self.store.recover_abandoned()
        self.assertEqual(await self.store.pending(),found)
        id, = found
        self.assertEqual(await self.store.persist(self.event),id)
        await self.store.dispatch(id,self.spawned)
        self.assertIsNotNone(await self.store.claim(id))


    async def test_legacy_seed_preserves_delivery_and_allows_new_activation(self):
        identity = [7,11,4,"a"*40,"b"*40]
        manifest = [(1,identity,"completed",self.time,["deadbeef"])]
        self.assertEqual(await self.invoke("seed_legacy",manifest),2)
        self.assertEqual(await self.invoke("seed_legacy",manifest),0)
        historical = await self.store.persist(self.event)
        self.assertIsNone(await self.store.claim(historical))
        active = await self.store.persist(replace(self.event,delivery="face"))
        self.assertNotEqual(active,historical)
        self.assertEqual(await self.store.pending(),[active])
