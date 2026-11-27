# Host-side model transport

The CPU worker calls the configured, proxy-authenticated Modal HTTPS
`/v1/responses` endpoint. It never forwards guest-provided credentials or
destinations, rejects redirects for inference, and accepts only a completed
SSE result. Model requests share the review's overall deadline and have no
independent turn, output-token or transfer-size cap in this transport.
vLLM, Modal, and QuickJS still enforce their own constraints.

Run `python3 -m unittest discover -s mwrev-gateway -v` (mock transport, no GPU).
