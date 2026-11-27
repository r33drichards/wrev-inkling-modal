"""Dedicated state writer. Do not redeploy this with the review worker."""
from pathlib import Path

import modal

ROOT = Path(__file__).resolve().parents[1]
app = modal.App("mwrev-state")
image = (modal.Image.debian_slim(python_version="3.12")
         .add_local_file(ROOT / "mwrev-dict/dict_store.py", "/opt/mwrev-state/dict_store.py", copy=True)
         .add_local_file(ROOT / "mwrev-candidate/store.py", "/opt/mwrev-state/store.py", copy=True))
data = modal.Dict.from_name("mwrev-review-state-v1", create_if_missing=False)


@app.function(image=image, cpu=0.125, memory=128, min_containers=0, max_containers=1,
              timeout=30)
async def coordinate(action, *args):
    import sys
    sys.path.insert(0, "/opt/mwrev-state")
    from dict_store import Coordinator
    return await Coordinator(data).invoke(action, *args)
