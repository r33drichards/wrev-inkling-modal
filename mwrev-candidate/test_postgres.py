"""Isolated real PostgreSQL tests, opt in with MWREV_TEST_DATABASE_URL."""
import asyncio
import os
from pathlib import Path
import unittest
from uuid import uuid4

from ingress import Review
from store import Store, UnconfirmedDispatch

URL = os.environ.get("MWREV_TEST_DATABASE_URL")

@unittest.skipUnless(URL, "set MWREV_TEST_DATABASE_URL for PostgreSQL integration")
class PostgresTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        import psycopg
        from psycopg import sql
        self.schema = "mwrev_test_" + uuid4().hex
        async with await psycopg.AsyncConnection.connect(URL) as c:
            await c.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(self.schema)))
        async def connect():
            return await psycopg.AsyncConnection.connect(URL, options=f"-c search_path={self.schema}")
        self.store = Store(connect)
        async with await connect() as c:
            await c.execute(Path(__file__).with_name("schema.sql").read_text())

    async def asyncTearDown(self):
        import psycopg
        from psycopg import sql
        async with await psycopg.AsyncConnection.connect(URL) as c:
            await c.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(self.schema)))

    @staticmethod
    def review(delivery="abc", head="a" * 40):
        return Review(delivery, 7, 11, 23, head, "b" * 40)

    async def test_concurrent_receipts_dispatch_and_claim(self):
        one, two = await asyncio.gather(self.store.persist(self.review()),
                                        self.store.persist(self.review("def")))
        self.assertEqual(one, two)
        with self.assertRaises(ValueError):
            await self.store.persist(self.review(head="c" * 40))
        self.assertEqual(await self.store.pending(), [one])
        entered, release = asyncio.Event(), asyncio.Event()
        async def spawn(id):
            self.assertEqual(id, one)
            entered.set()
            await release.wait()
            return "fc_fixture"
        task = asyncio.create_task(self.store.dispatch(one, spawn))
        try:
            await asyncio.wait_for(entered.wait(), 5)
            with self.assertRaises(UnconfirmedDispatch):
                await self.store.dispatch(one, spawn)
        finally:
            release.set()
            await task
        self.assertEqual(await self.store.pending(), [])
        claims = await asyncio.gather(self.store.claim(one), self.store.claim(one))
        valid = [c for c in claims if c]
        self.assertEqual(len(valid), 1)
        old, _ = valid[0]
        async with await self.store.connect() as c:
            await c.execute("UPDATE mwrev_review SET expires_at=now() WHERE id=%s", (one,))
        new, _ = await self.store.claim(one)
        self.assertNotEqual(old, new)
        self.assertFalse(await self.store.finish(one, old, {"findings": []}))
        self.assertTrue(await self.store.finish(one, new, {"findings": []}))

    async def test_same_delivery_stays_deduped_but_new_activation_can_rerun(self):
        one = await self.store.persist(self.review())
        attempt, _ = await self.store.claim(one)
        self.assertTrue(await self.store.finish(one, attempt, {"state":"completed"}))
        self.assertEqual(await self.store.persist(self.review()), one)
        self.assertNotEqual(await self.store.persist(self.review("new-activation")), one)

    async def test_bridge_attaches_only_existing_active_jobs_and_seed_requires_drain(self):
        one = await self.store.persist(self.review())
        self.assertEqual(await self.store.attach_legacy_delivery(self.review("def")), one)
        self.assertIsNone(await self.store.attach_legacy_delivery(self.review("99","c"*40)))
        with self.assertRaisesRegex(RuntimeError, "have not drained"):
            await self.store.settled_manifest()
        attempt, _ = await self.store.claim(one)
        self.assertEqual(await self.store.reserve_start_comment(one,attempt), (None,))
        self.assertTrue(await self.store.remember_comment(one,attempt,13))
        self.assertTrue(await self.store.finish(one,attempt,{"state":"completed","review":{"findings":[]}}))
        with self.assertRaisesRegex(RuntimeError, "have not drained"):
            await self.store.settled_manifest()
        await self.store.mark_published(one,13)
        self.assertEqual(await self.store.attach_legacy_delivery(self.review("def")), one)
        self.assertIsNone(await self.store.attach_legacy_delivery(self.review("face")))
        id, identity, state, _, deliveries = (await self.store.settled_manifest())[0]
        self.assertEqual((id,identity,state),(one,[7,11,23,"a"*40,"b"*40],"completed"))
        self.assertEqual(set(deliveries), {"abc","def"})
