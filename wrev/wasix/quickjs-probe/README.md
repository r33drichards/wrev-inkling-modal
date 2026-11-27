# QuickJS WASIX probe

A single WASIX binary statically links QuickJS-NG through rquickjs 0.14.0.
The probe checks JavaScript arithmetic, the realtime clock, a synchronous Rust
callback, tool discovery, and two awaited mock tool calls. Each mock Rust future
yields before resolving. Wasmer runs the binary without network access.

Run with Cargo, a WASIX Rust compiler, WASI SDK 34, and Wasmer 7.5.0:

```sh
WASIX_RUSTC=/path/to/wasix/bin/rustc \
WASI_SDK=/path/to/wasi-sdk \
WASMER_BIN=/path/to/wasmer \
./run-wasix.sh
```

The script verifies the published rquickjs-sys crate checksum, reuses its bundled
32-bit WASI bindings for the functions used here, and patches QuickJS's two clock
helpers to call the WASI clock API directly. WASI SDK libc headers use pointer
clock IDs while the WASIX libc in the linked binary expects integer IDs. Without
the patch, Wasmer exits during QuickJS context creation.

The probe uses mock tools and lives outside Minimal MCP Agent.
