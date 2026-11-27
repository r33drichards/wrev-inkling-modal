# Inkling inference candidate

`inkling.py` describes a separate Modal B200 web function. It uses the image
digest, model revision, parser, and DSpark settings exercised by the earlier
smoke test. Modal proxy authentication is required, the service starts with
zero containers, and at most one is allocated. The configured cache is a
private, environment-scoped volume and is not created during module import.

This is not the public GitHub webhook. The eventual trusted CPU gateway must
hold the proxy credentials, enforce authenticated request/response and
concurrency limits, and preserve the Wrev streaming Responses contract. The
guest must never receive the proxy credentials. No real Modal staging or
GPU verification has been performed on this entrypoint. Verify unauthenticated
requests are denied, cache reuse works, and the cold start, streaming,
cancellation and timeout behavior meet the staging gates before release.

Run the CPU-only configuration check: `python -m unittest discover -s mwrev-inference -v`.
