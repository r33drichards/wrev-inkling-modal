# Modal production entrypoint candidate

This wires the receiver, worker, pinned evidence preparation, credential-free
Wasmtime and GitHub App status comments. Scope the first production deployment
to exactly one approved repository; it ignores Railway's `wrev` label. Deploy
only after the necessary secrets, database and App installation have been
provisioned. Run `migrate` against an isolated mwrev database before enabling
the GitHub delivery route. Never put a credential in the repository or the
Wasm guest.

| Modal secret | Host environment |
| --- | --- |
| mwrev-database | MWREV_DATABASE_URL |
| mwrev-allowlist | MWREV_REPOSITORIES (one JSON object in an array, keys installation_id, repository_id, repository) |
| mwrev-webhook | MWREV_WEBHOOK_SECRET |
| mwrev-github-app | MWREV_GITHUB_APP_ID, MWREV_GITHUB_APP_PRIVATE_KEY |
| mwrev-model-gateway | MWREV_MODEL_ORIGIN, MWREV_MODAL_PROXY_KEY, MWREV_MODAL_PROXY_SECRET |

The App needs Contents: read and Pull requests: write, and subscribes to Pull
request events. Install only on the allowed repository. Point the App at the
receiver URL plus `/github/webhook`. The separate B200 entrypoint uses
`mwrev-inkling-cache` and proxy authentication. Before launching real traffic,
verify the exact CPU image and hosted inference readiness independently.

Run CPU transport/worker tests with `python -m unittest discover -s mwrev-service -v`.
The real Modal deploy, signed app delivery, token permissions, and in-place
comment edit are not proven by these tests. Current implementation checks the
live label and PR revision on start and completion; re-adding a label at the
same base/head still deduplicates that revision. Do not present this as full
label activation epoch support.
