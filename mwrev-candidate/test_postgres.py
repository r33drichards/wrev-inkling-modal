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
