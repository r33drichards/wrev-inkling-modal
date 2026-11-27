# Wrev + Inkling 2 Mini on Modal

**Status: design and source vendoring.** No Modal app, GPU, live webhook, or GitHub write-back is enabled by this repository.

## Goal

Applying the `wrev` label to a GitHub pull request should start a review. Reuse the Go [webhook worker](https://github.com/r33drichards/webhook) for ingress and queueing, and use the [Wrev Wasmtime distribution](https://github.com/r33drichards/wrev/blob/main/BUILD.md#wasmtime) for execution, not the current production worker's native reviewer or the Wasmer/WASIX listener. Serve the official Inkling 2 Mini checkpoint separately on Modal.

```text
GitHub pull_request:labeled (wrev)
  -> HTTPS /github/webhook (Go worker, Modal CPU)
  -> verify HMAC on raw body + allowlist; durably enqueue; acknowledge
  -> worker claims job and stages immutable base/head evidence
  -> Wasmtime 51 + Wrev wasm32-wasip2 component -> findings JSON
       -> WASI HTTP -> authenticated vLLM on Modal -> Inkling 2 Mini
  -> record result; comment/label write-back only when explicitly enabled
```

## 1. Webhook and review runner

- Reuse the worker's `/github/webhook` handler, label lifecycle, job records and pinned snapshot preparation. Subscribe to GitHub **Pull requests** events, match `action=labeled` and the exact label `wrev`, and use `unlabeled` for cancellation. Draft PRs are in scope. Reject invalid signatures and unauthorized repositories before making any GitHub or model call.
- Acknowledge after a durable enqueue, comfortably within GitHub's [10-second window](https://docs.github.com/en/webhooks/using-webhooks/best-practices-for-using-webhooks). Deduplicate by installation/repo and delivery ID; ensure one active review per PR/head/label activation. Recheck label and head before processing or publishing; mark a moved head stale. Define a retry, cancellation, and reclaim path for an interrupted job. Do not enqueue in a background thread after acknowledging unless durability is already established.
- Modal CPU pilot: one Go worker container. Preserve SQLite semantics only if restart, durability and single-writer behavior are proven for the chosen storage; otherwise use a transactional Postgres queue. Don't use a Modal Volume as a concurrent shared SQLite database. Exercise scale-to-zero and recovery before enabling it for the CPU worker.
- Adapt the native runner to invoke the pinned `wasm32-wasip2` artifact with Wasmtime. Keep GitHub read and optional write credentials on the host, not in the guest. Preopen only per-job inputs and the optional checkpoint directory; scope model egress to the inference destination. Stage Git objects as evidence, never run PR code or build scripts. Validate output against the existing findings schema, and cap bytes, tool turns, runtime and retry attempts.
- Initial rollout: one fixture repo and a staging-only label or isolated webhook to avoid duplicate reviews with the Railway worker. Record findings without write-back; enable comments and label removal only after happy path, duplicate delivery, cancellation and stale-head checks pass. Do not bulk-install webhooks or change the production worker.

## 2. Inkling 2 Mini on Modal

- Serve [`thinkingmachines/Inkling-2-Mini`](https://huggingface.co/thinkingmachines/Inkling-2-Mini) at a pinned HF revision using its official mixed-precision checkpoint: NVFP4 routed experts with MXFP8 in the final four layers, about 20 GB on disk. No additional quantization or model weights in Git.
- Follow [Modal's vLLM pattern](https://modal.com/docs/examples/vllm_inference) for packaging and scaling, not its Gemma version pins. The [Inkling-specific vLLM recipe](https://recipes.vllm.ai/thinkingmachines/Inkling-2-Mini) currently requires a CUDA 13.2-compatible nightly (vLLM 0.35.0 or newer once released); pin a tested build/image digest and verify the Modal driver before staging.
- Pilot on one Blackwell GPU, TP1; try B200 first and use the upstream-verified B300 if the B200 runtime/kernel check fails. Start text-only at 32K context and concurrency 1; validate workload fit before raising either. Use Inkling's reasoning and tool-call parsers and enable automatic tool choice. Add the optional DSpark draft only after the baseline passes. Treat the advertised 1M window as a separate stress test, not a launch setting.
- Keep inference authenticated and separate from ingress: Modal proxy auth or an equivalent private authenticated endpoint backed by Modal secrets. Cache the pinned checkpoint on a private volume; use `min_containers=0`, `max_containers=1`, a short tested idle window, and explicit execution limits. Modal's [published GPU rates](https://modal.com/pricing) are about $6.25/hour for one B200 or $7.10/hour for one B300, GPU time only. Measure cold start and billed seconds per completed review; do not assume the GPU rate is the cost per review.

## 3. Blocking compatibility test

Wrev currently posts streaming `/v1/responses` requests to `api.openai.com` with ChatGPT sign-in. Inkling's official vLLM recipe demonstrates `/v1/chat/completions`. vLLM implements [Responses tool calling](https://docs.vllm.ai/en/latest/examples/tool_calling/openai_responses_client_with_tools/), but that does not establish compatibility with Wrev's exact model, parser, and SSE events.

1. Add explicit provider/endpoint configuration and use Modal-specific credentials. Never forward a ChatGPT access token to Modal. Prefer header injection on the trusted host if the embedding supports it.
2. Replay a captured, scrubbed Wrev request against a mock. Then test the pinned vLLM build for incremental SSE, `run_js` tool calls, call IDs, tool-result round trip, reasoning separation, and a final valid findings object. Preserve the existing provider path.
3. If direct streaming Responses fails the contract, use a narrow Chat Completions adapter that preserves tool IDs, ordering, failures and output schema. Do not switch Wrev into assuming provider compatibility or silent fallbacks.

## Acceptance gates

- **CPU-only:** synthetic signed label event queues one review; duplicate delivery does not spawn another; invalid signature/wrong repo triggers nothing; cancellation and restart behave predictably.
- **Wasmtime + mock model:** a synthetic pinned PR triggers a real WASI HTTP request, a QuickJS evidence read via the Rust bridge, a tool-result round trip, and schema-valid findings with no GitHub credential in the guest.
- **Bounded GPU staging:** confirm endpoint auth, actual NVFP4 checkpoint loading, parser output, cold start, time-to-first-token, and cost per completed sample. Then apply the label to one throwaway PR and trace it to a persisted review before considering write-back.
- **Release:** record commit IDs and artifact checksum, deploy CPU and GPU separately, and keep rollback independent. No model or GPU jobs run from the vendoring workflow.

## Vendored sources

These are intended as pinned source trees in one integration repository, not GitHub fork relationships or submodules. Upstream Git histories remain at the linked repositories.

| Directory | Upstream | Pinned commit |
| --- | --- | --- |
| `wrev/` | [r33drichards/wrev](https://github.com/r33drichards/wrev) | `d8713736595c2067796c72d00b91db63db28fab4` |
| `webhook/` | [r33drichards/webhook](https://github.com/r33drichards/webhook) | `af3d2a160d817018e7477544e7bd098d3ce04843` |

Preserve each source's notices and license status. The pinned webhook fork includes an MIT license and dependency notices; the Wrev source does not declare a repository-wide license at the pinned revision. Vendoring does not add one. Nested upstream GitHub workflows do not run from this repository.
