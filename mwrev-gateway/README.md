# Host-side model transport

This module is not a server and is not wired into the reviewer yet. The host
passes the configured Modal service origin and proxy headers; a future Wasmtime
transport adapter must enforce that only the intended guest can call it and
must never forward any guest-supplied destination or credentials. It keeps the
public inference endpoint's authentication and permits only the host-configured
HTTPS service `/v1/responses` route. The host must also enforce the overall
review deadline and concurrency caps. Live SSE, readiness, final-turn behavior,
and guest isolation remain acceptance gates.

Candidate defaults, not deployed settings: 16 MiB model request, 8 MiB stream,
8,192 output tokens per response, and 180 seconds per model request. These are
in `gateway.py`, separate from Wasmtime/QuickJS and Modal Function limits.

Run `python -m unittest discover -s mwrev-gateway -v` (mock transport, no GPU).
