# CPU-first Wrev ingress candidate

This is not a deployment entrypoint. It implements an injectable, signed ASGI
endpoint, a PostgreSQL receipt/review store, bounded dispatch reconciliation,
and a conditional review claim/completion contract. Run synthetic CPU tests:

```sh
python -m unittest discover -v
```

The webhook path is `POST /github/webhook`. Configure the GitHub App's signing
secret as bytes and an explicit set of (installation ID, repository ID) pairs.
Mount `ingress.app(secret, allowed, store.persist, dispatch)` on the Modal ASGI
function. Build `dispatch(id)` from `store.dispatch(id, spawn)`; the passed
`spawn` coroutine must return the confirmed Modal FunctionCall ID (never a PR
body or a credential). Use psycopg 3's AsyncConnection connector and an isolated
database/role, apply schema.sql through the migration job, and point a scheduled
Modal Function at `repair.repair(store, spawn)`. The transaction rolls back if
a reused delivery tries to change its review ID. A duplicate that is currently
being dispatched may return 503 until its call ID is known.

Before this can ship, verify schema/query behavior against real isolated
Postgres, including interleaved delivery/dispatch/claim operations. Add observed
Modal dispatch-and-crash recovery checks, reconciliation for exhausted dispatch
and confirmed-but-lost invocations, GitHub current label/head validation and
label reactivation/cancellation, the fenced Wasmtime runner, and comment
reconciliation. In particular, this partial schema deduplicates a revision even
after label removal/re-add; it does not yet implement PR activation epochs.
No app credentials, production database, model, Railway, or public webhook has
been used.
