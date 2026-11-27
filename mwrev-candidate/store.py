"""Atomic review persistence and bounded dispatch recovery."""
import uuid

class UnconfirmedDispatch(RuntimeError):
    pass

class Store:
    def __init__(self, connect):
        self.connect = connect

    async def persist(self, review):
        async with await self.connect() as conn:
            r = await conn.execute("""
            INSERT INTO mwrev_review (installation, repository, pr, head, base)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (installation, repository, pr, head, base)
            DO UPDATE SET head = EXCLUDED.head RETURNING id
            """, (review.installation, review.repository, review.number, review.head, review.base))
            proposed, = await r.fetchone()
            d = await conn.execute("""
            INSERT INTO mwrev_delivery (installation, repository, delivery, review_id)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (installation, repository, delivery)
            DO UPDATE SET delivery = EXCLUDED.delivery RETURNING review_id
            """, (review.installation, review.repository, review.delivery, proposed))
            existing, = await d.fetchone()
            if existing != proposed:
                raise ValueError("delivery identity changed")
            return existing  # connection context commits before result reaches ingress

    async def pending(self):
        async with await self.connect() as conn:
            r = await conn.execute("""
            SELECT id FROM mwrev_review WHERE modal_call_id IS NULL AND result IS NULL
            AND dispatch_after <= now() AND dispatch_attempts < 8
            ORDER BY dispatch_after, id LIMIT 50
            """)
            return [row[0] for row in await r.fetchall()]

    async def dispatch(self, review_id, spawn):
        token = uuid.uuid4()
        async with await self.connect() as conn:
            r = await conn.execute("""
            UPDATE mwrev_review SET dispatch_token = %s,
            dispatch_after = now() + interval '60 seconds',
            dispatch_attempts = dispatch_attempts + 1
            WHERE id = %s AND modal_call_id IS NULL AND result IS NULL
            AND dispatch_after <= now() AND dispatch_attempts < 8 RETURNING id
            """, (token, review_id))
            if await r.fetchone() is None:
                q = await conn.execute("""
                SELECT modal_call_id IS NOT NULL OR result IS NOT NULL
                FROM mwrev_review WHERE id = %s
                """, (review_id,))
                row = await q.fetchone()
                if row is not None and row[0]:
                    return
                raise UnconfirmedDispatch()
        call_id = await spawn(review_id)
        if not call_id:
            raise UnconfirmedDispatch()
        async with await self.connect() as conn:
            r = await conn.execute("""
            UPDATE mwrev_review SET modal_call_id = %s WHERE id = %s
            AND dispatch_token = %s RETURNING id
            """, (call_id, review_id, token))
            if await r.fetchone() is None:
                raise UnconfirmedDispatch()

    async def claim(self, review_id):
        token = uuid.uuid4()
        async with await self.connect() as conn:
            r = await conn.execute("""
            UPDATE mwrev_review SET attempt_token = %s,
            expires_at = now() + interval '30 minutes'
            WHERE id = %s AND result IS NULL
            AND (attempt_token IS NULL OR expires_at <= now())
            RETURNING installation, repository, pr, head, base
            """, (token, review_id))
            row = await r.fetchone()
            return (token, row) if row else None

    async def finish(self, review_id, token, result):
        from psycopg.types.json import Jsonb
        async with await self.connect() as conn:
            r = await conn.execute("""
            UPDATE mwrev_review SET result = %s WHERE id = %s AND attempt_token = %s
            AND expires_at > now() AND result IS NULL RETURNING id
            """, (Jsonb(result), review_id, token))
            return await r.fetchone() is not None
