"""Modal entrypoint for the isolated mwrev webhook and CPU runner."""
import asyncio
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace

import modal

ROOT = Path(__file__).resolve().parents[1]
CODE = "/opt/mwrev"
PORTABLE = ("mwrev-candidate", "mwrev-gateway", "mwrev-runner",
            "mwrev-publisher", "mwrev-service")
app = modal.App("mwrev")
base = modal.Image.debian_slim(python_version="3.12").pip_install(
    "psycopg[binary]==3.2.10", "PyJWT[crypto]==2.10.1", "fastapi",
)
runtime = base.apt_install("git", "curl", "xz-utils", "build-essential", "ca-certificates")
for directory in PORTABLE:
    base = base.add_local_dir(ROOT / directory, f"{CODE}/{directory}", copy=True,
                              ignore=["__pycache__"])
    runtime = runtime.add_local_dir(ROOT / directory, f"{CODE}/{directory}", copy=True,
                                    ignore=["__pycache__"])
runtime = runtime.add_local_dir(ROOT / "wrev", f"{CODE}/wrev", copy=True,
                                ignore=["target", "__pycache__"]).run_commands(
    "curl -fsSL --retry 3 https://github.com/bytecodealliance/wasmtime/releases/download/v51.0.0/wasmtime-v51.0.0-x86_64-linux.tar.xz | tar -xJ -C /opt",
    "curl -fsSL --retry 3 https://github.com/WebAssembly/wasi-sdk/releases/download/wasi-sdk-24/wasi-sdk-24.0-x86_64-linux.tar.gz | tar -xz -C /opt",
    "curl -fsSL --retry 3 https://static.rust-lang.org/rustup/dist/x86_64-unknown-linux-gnu/rustup-init -o /tmp/rustup-init && chmod 700 /tmp/rustup-init && /tmp/rustup-init -y --no-modify-path --default-toolchain 1.100.0 --profile minimal --target wasm32-wasip2 && rm /tmp/rustup-init",
    "cd /opt/mwrev/wrev && ~/.cargo/bin/cargo build --locked --release --bin wrev",
    "cd /opt/mwrev/wrev && CC_wasm32_wasip2=/opt/wasi-sdk-24.0-x86_64-linux/bin/clang WASI_SDK=/opt/wasi-sdk-24.0-x86_64-linux ~/.cargo/bin/cargo build --locked --release --target wasm32-wasip2 --features mwrev-host-stdio --bin wrev",
    "python3 /opt/mwrev/mwrev-runner/smoke_guest.py /opt/wasmtime-v51.0.0-x86_64-linux/wasmtime /opt/mwrev/wrev/target/wasm32-wasip2/release/wrev.wasm",
)
database = modal.Secret.from_name("mwrev-database")
allowlist = modal.Secret.from_name("mwrev-allowlist")
webhook = modal.Secret.from_name("mwrev-webhook")
github = modal.Secret.from_name("mwrev-github-app")
model = modal.Secret.from_name("mwrev-model-gateway")

def setup():
    for directory in PORTABLE:
        path = f"{CODE}/{directory}"
        if path not in sys.path:
            sys.path.append(path)

def allowed_repositories():
    rows = json.loads(os.environ["MWREV_REPOSITORIES"])
    if not isinstance(rows, list) or len(rows) != 1:
        raise RuntimeError("this release expects one explicitly approved repository")
    row = rows[0]
    return {(int(row["installation_id"]), int(row["repository_id"])):row["repository"]}

async def connect():
    import psycopg
    return await psycopg.AsyncConnection.connect(os.environ["MWREV_DATABASE_URL"], connect_timeout=5)

async def spawn(id):
    return (await review.spawn.aio(id)).object_id

def worker():
    setup()
    from service import Worker
    from store import Store
    cfg = SimpleNamespace(
        allowed=allowed_repositories(), app_id=int(os.environ["MWREV_GITHUB_APP_ID"]),
        private_key=os.environ["MWREV_GITHUB_APP_PRIVATE_KEY"], comments=True,
        native=Path(f"{CODE}/wrev/target/release/wrev"),
        component=Path(f"{CODE}/wrev/target/wasm32-wasip2/release/wrev.wasm"),
        wasmtime=Path("/opt/wasmtime-v51.0.0-x86_64-linux/wasmtime"),
        model_origin=os.environ["MWREV_MODEL_ORIGIN"],
        model_auth={"Modal-Key":os.environ["MWREV_MODAL_PROXY_KEY"],
                    "Modal-Secret":os.environ["MWREV_MODAL_PROXY_SECRET"]},
    )
    return Worker(Store(connect), cfg)

@app.function(image=base, secrets=[database], timeout=60)
async def migrate():
    async with await connect() as c:
        await c.execute(Path(f"{CODE}/mwrev-candidate/schema.sql").read_text())

@app.function(image=base, secrets=[database, allowlist, webhook],
              cpu=0.25, memory=256, min_containers=0, max_containers=2, timeout=10)
@modal.asgi_app()
def receiver():
    setup()
    from ingress import app as ingress
    from store import Store
    store = Store(connect)
    return ingress(os.environ["MWREV_WEBHOOK_SECRET"].encode(),
                   allowed_repositories(), store.persist, lambda id: store.dispatch(id, spawn))

@app.function(image=runtime, secrets=[database, allowlist, github, model],
              cpu=2, memory=4096, min_containers=0, max_containers=1, timeout=1500)
async def review(id):
    w = worker()
    state = await w.execute(id)
    print(json.dumps({"review_id":id, "state":state}), flush=True)
    # The only publisher is repair_tick, avoiding simultaneous writer processes.

@app.function(image=runtime, secrets=[database, allowlist, github, model],
              schedule=modal.Period(minutes=2), max_containers=1, timeout=480)
async def repair_tick():
    w = worker()
    from repair import repair
    await w.store.recover_abandoned()
    dispatched = await repair(w.store, spawn)
    published = await w.publish()
    if dispatched.redispatched or dispatched.unconfirmed or published:
        print(json.dumps({"redispatched":dispatched.redispatched,
                          "unconfirmed":dispatched.unconfirmed,
                          "published":published}), flush=True)
