# Wrev + Inkling 2 Mini on Modal

Apply `mwrev` or its alias `mrev` to an open GitHub pull request, including drafts.
The GitHub App must be installed on the PR's **base repository** under the
configured account. Unlabeled PRs do not run. The app posts a start comment,
updates it with the outcome and findings linked to the reviewed commit, and
removes the trigger label after success or
terminal failure. It leaves the label in place during retries.

```text
GitHub signed pull_request event
  -> Modal ASGI receiver -> Neon Postgres -> Modal CPU review function
  -> pinned base/head evidence -> Wrev under Wasmtime
  -> host model transport -> private Inkling vLLM on one Modal B200
  -> durable result -> publisher updates start comment + clears trigger
```

Only the host receives repository-scoped GitHub tokens and model proxy
credentials. The guest reads a staged evidence snapshot and uses a typed
stdio transport for model calls. It never runs PR code or receives network
access. The receiver verifies GitHub's HMAC and checks both the installation
and repo owner; the worker checks the repository's current owner by ID before
fetching evidence or writing to GitHub.

## Runtime configuration

- CPU reviews: zero warm containers; up to four concurrent review workers,
  each requesting 0.125 CPU and 128 MiB, with hard limits of 1 CPU and 256 MiB.
  Each attempt gets up to 60 minutes total, including up to 300 seconds for
  evidence prep. Its 65-minute database lease outlasts the function timeout.
- Model: one B200 (TP1), zero warm containers, 60-second idle window; up to four
  concurrent HTTP inputs enter the same server. vLLM derives context length
  from the pinned model and chooses its scheduler defaults. The host does not
  impose a per-turn token or time cap, a model turn count, or JSONL/SSE byte caps.
  The total deadline, model context and platform constraints still apply.
- Ingress: signed, deduplicated, durable before acknowledgment. GitHub App
  permissions are Contents read and Pull requests read/write; credentials are
  scoped to each PR's base repository.
- QuickJS memory and output settings remain 100 MiB each.

See [service setup](mwrev-service/README.md), [inference](mwrev-inference/README.md),
and the [architecture diagram](wrev-modal-spawn.svg). The
[original rollout plan](plan.md) predates the account-wide scope and updated limits.

The vendored `wrev/` tree derives from
[r33drichards/wrev at d871373](https://github.com/r33drichards/wrev/tree/d8713736595c2067796c72d00b91db63db28fab4)
with integration changes for Wasmtime. The original Go webhook service is not
part of this deployment and remains in its separate repository.
