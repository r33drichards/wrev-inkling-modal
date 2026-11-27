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
            "mwrev-publisher", "mwrev-service", "mwrev-dict")
BACKEND = os.environ.get("MWREV_DEPLOY_BACKEND", "dict")
if BACKEND not in {"postgres", "bridge", "dict"}:
    raise ValueError("unknown Wrev state backend")
backend_config = modal.Secret.from_dict({"MWREV_DEPLOY_BACKEND": BACKEND})
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

def allowed_account():
    from github_app import AccountScope
    rows = json.loads(os.environ["MWREV_REPOSITORIES"])
    if not isinstance(rows, list) or len(rows) != 1:
        raise RuntimeError("this release expects one explicitly approved repository")
    row = rows[0]
    return AccountScope(int(row["installation_id"]), row["repository"].split("/", 1)[0])

async def connect():
    import psycopg
    return await psycopg.AsyncConnection.connect(os.environ["MWREV_DATABASE_URL"], connect_timeout=5)

async def spawn(id):
    return (await review.spawn.aio(id)).object_id

def postgres():
    setup()
    from store import Store
    return Store(connect)

def dict_state():
    setup()
    from dict_store import DictStore
    coordinator = modal.Function.from_name("mwrev-state", "coordinate")
    return DictStore(coordinator.remote.aio)

def ingress_state():
    if BACKEND == "postgres":
        return postgres()
    current = dict_state()
    if BACKEND == "dict":
        return current
    from bridge import BridgeIngress
    return BridgeIngress(postgres(), current)

def storage_secrets():
    return [backend_config] + ([database] if BACKEND != "dict" else [])

def worker(state):
    setup()
    from service import Worker
    cfg = SimpleNamespace(
        allowed=allowed_account(), app_id=int(os.environ["MWREV_GITHUB_APP_ID"]),
        private_key=os.environ["MWREV_GITHUB_APP_PRIVATE_KEY"], comments=True,
        native=Path(f"{CODE}/wrev/target/release/wrev"),
        component=Path(f"{CODE}/wrev/target/wasm32-wasip2/release/wrev.wasm"),
        wasmtime=Path("/opt/wasmtime-v51.0.0-x86_64-linux/wasmtime"),
        model_origin=os.environ["MWREV_MODEL_ORIGIN"],
        model_auth={"Modal-Key":os.environ["MWREV_MODAL_PROXY_KEY"],
                    "Modal-Secret":os.environ["MWREV_MODAL_PROXY_SECRET"]},
    )
    return Worker(state, cfg)

if BACKEND != "dict":
    @app.function(image=base, secrets=[backend_config, database], timeout=60)
    async def migrate():
        async with await connect() as c:
            await c.execute(Path(f"{CODE}/mwrev-candidate/schema.sql").read_text())

if BACKEND == "bridge":
    @app.function(image=base, secrets=[backend_config, database], timeout=60)
    async def seed_legacy():
        manifest = await postgres().settled_manifest()
        state = dict_state()
        added = await state.call("seed_legacy", manifest)
        return {"legacy_reviews":len(manifest), "inserted":added, **await state.call("status")}


@app.function(image=base, secrets=[allowlist, webhook, *storage_secrets()],
              cpu=0.25, memory=256, min_containers=0, max_containers=2, timeout=10)
@modal.asgi_app()
def receiver():
    setup()
    from ingress import app as ingress
    store = ingress_state()
    return ingress(os.environ["MWREV_WEBHOOK_SECRET"].encode(),
                   allowed_account(), store.persist, lambda id: store.dispatch(id, spawn))

@app.function(image=runtime, secrets=[allowlist, github, model, *storage_secrets()],
              cpu=(0.125, 1.0), memory=(128, 256), min_containers=0, max_containers=2, timeout=3600)
async def review(id):
    if type(id) is int:
        if BACKEND == "dict":
            # A duplicate of an already-drained legacy input may still arrive.
            return "legacy_drained"
        state_store = postgres()
    else:
        if BACKEND == "postgres":
            return "unsupported_dict_job"
        state_store = dict_state()
    w = worker(state_store)
    state = await w.execute(id)
    print(json.dumps({"review_id":id, "state":state}), flush=True)
    # The only publisher is repair_tick, avoiding simultaneous writer processes.

async def repair_backend(name, state, check_call=None):
    from repair import repair

    if check_call is None:
        await state.recover_abandoned()
    else:
        await state.recover_abandoned(check_call)
    dispatched = await repair(state, spawn)
    published = await worker(state).publish()
    if dispatched.redispatched or dispatched.unconfirmed or published:
        print(json.dumps({"backend":name, "redispatched":dispatched.redispatched,
                          "unconfirmed":dispatched.unconfirmed,
                          "published":published}), flush=True)


@app.function(image=runtime, secrets=[allowlist, github, model, *storage_secrets()],
              schedule=modal.Period(minutes=2), max_containers=1, timeout=480)
async def repair_tick():
    setup()
    if BACKEND in {"postgres", "bridge"}:
        await repair_backend("postgres", postgres())
    if BACKEND in {"dict", "bridge"}:
        from dict_store import has_repair_work
        from modal_status import is_terminal

        data = modal.Dict.from_name("mwrev-review-state-v1", create_if_missing=False)
        # A webhook racing an idle snapshot dispatches its own job. If it
        # crashes before dispatch, a future tick sees the committed intent.
        if await has_repair_work(data):
            await repair_backend("dict", dict_state(), is_terminal)
