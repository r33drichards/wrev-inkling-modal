"""CPU-only integration probe; never loads production credentials or apps."""
from pathlib import Path

import modal

here = Path(__file__).resolve().parent
image = (modal.Image.debian_slim(python_version="3.12")
         .add_local_file(here / "dict_store.py", "/opt/dictprobe/dict_store.py", copy=True)
         .add_local_file(here.parent / "mwrev-candidate/store.py", "/opt/dictprobe/store.py", copy=True))
app = modal.App("mwrev-dict-prototype")


@app.function(image=image, max_containers=1, cpu=0.125, memory=128, timeout=30)
async def coordinate(dict_id, *args):
    import sys
    sys.path.insert(0, "/opt/dictprobe")
    from dict_store import Coordinator
    return await Coordinator(modal.Dict.from_id(dict_id)).invoke(*args)


@app.function(image=image, cpu=0.125, memory=128, timeout=30)
async def mock_review(dict_id, review_id):
    return await coordinate.remote.aio(dict_id, "claim", review_id)


@app.local_entrypoint()
def verify():
    import asyncio

    async def exercise(id):
        async def invoke(*args):
            return await coordinate.remote.aio(id, *args)
        events = [(("deadbeef" if i < 10 else "ffff"),7,11,4,"a"*40,"b"*40) for i in range(12)]
        ids = await asyncio.gather(*(invoke("persist", *event) for event in events))
        assert len(set(ids)) == 1
        rid = ids[0]
        attempts = await asyncio.gather(*(invoke("claim", rid) for _ in range(8)))
        claim, = [a for a in attempts if a is not None]
        token, identity = claim
        assert tuple(identity) == (7,11,4,"a"*40,"b"*40)
        assert await invoke("reserve_start_comment", rid, token) == [None]
        assert await invoke("remember_comment", rid, token, 42)
        assert not await invoke("finish", rid, "stale", {"state":"completed"})
        assert await invoke("finish", rid, token, {"state":"completed","review":{"findings":[]}})
        pending = await invoke("publications")
        assert len(pending) == 1 and pending[0][0] == rid and pending[0][-2] == 42
        assert await invoke("publications") == pending
        assert await invoke("mark_published", rid, 42)
        assert not await invoke("publications")
        assert await invoke("persist", *events[0]) == rid
        next_id = await invoke("persist", "1234", *events[0][1:])
        assert next_id != rid
        dispatch = await invoke("reserve_dispatch", next_id)
        input_call = await mock_review.spawn.aio(id, next_id)
        assert await invoke("confirm_dispatch", next_id, dispatch["token"], input_call.object_id)
        assert await input_call.get.aio(timeout=20) is not None
        assert await invoke("claim", next_id) is None
        print("Real Modal Dict and concurrent RPCs: 12 deliveries, 8 claim attempts, "
              "one review, stable publication retry, relabel starts a new review.")

    with modal.Dict.ephemeral() as state:
        asyncio.run(exercise(state.object_id))
