# Mounted Wasmtime review

Build the vendored Wrev guest with
`cargo build --locked --release --target wasm32-wasip2 --features mwrev-host-stdio`.
This feature removes the guest's existing WASI HTTP client for this build.
The native and default WASI builds retain the original provider path.

Use the CPU runner with an empty guest environment apart from explicit Wasmtime
configuration, mounted immutable evidence prepared by the trusted host, and no
guest network capability. The host callback sends framed model requests through
the authenticated gateway and returns completed Responses output, never
credentials. Independently check the pinned base, head and PR number and
validate finding locations before publication.

The process protocol and SSE parser have local mock tests. The Rust/Wasm
artifact, actual Wasmtime link, transport on a real review, trusted evidence
preparation and production deployment are not yet verified by these tests.
