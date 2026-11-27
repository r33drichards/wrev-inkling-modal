"""One bounded repair pass, intended for Modal's scheduled CPU Function."""
import asyncio
from dataclasses import dataclass

@dataclass(frozen=True)
class RepairResult:
    redispatched: int
    unconfirmed: int

async def repair(store, spawn):
    redispatched = unconfirmed = 0
    for review_id in await store.pending():
        try:
            async with asyncio.timeout(8):
                await store.dispatch(review_id, spawn)
        except Exception:
            unconfirmed += 1
        else:
            redispatched += 1
    return RepairResult(redispatched, unconfirmed)
