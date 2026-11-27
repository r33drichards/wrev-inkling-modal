#!/usr/bin/env bash
set -euo pipefail

: "${WASIX_RUSTC:?Set WASIX_RUSTC to the WASIX toolchain rustc}"
: "${WASI_SDK:?Set WASI_SDK to the WASI SDK directory}"
cd "$(dirname "$0")"

probe_target="$PWD/target"
sys_src="$probe_target/wasix-rquickjs-sys-0.14.0"
if [[ ! -f "$sys_src/.wasix-probe-patched" ]]; then
    mkdir -p "$probe_target"
    staging=$(mktemp -d "$probe_target/rquickjs-stage.XXXXXX")
    trap 'rm -rf "$staging"' EXIT
    curl -fsSL https://static.crates.io/crates/rquickjs-sys/rquickjs-sys-0.14.0.crate -o "$staging/source.crate"
    printf '%s  %s\n' cee271d0eeba64f0915b846cb7ae02e16faf3dfdffdca91731101d9d30fe3423 "$staging/source.crate" | sha256sum --check --status
    tar -xzf "$staging/source.crate" -C "$staging"
    extracted="$staging/rquickjs-sys-0.14.0"
    cp "$extracted/src/bindings/wasm32-wasip1.rs" "$extracted/src/bindings/wasm32-wasmer-wasi.rs"
    patch -d "$extracted" -p1 < wasix-clock.patch
    touch "$extracted/.wasix-probe-patched"
    mv -T "$extracted" "$sys_src"
fi

RUSTC="$WASIX_RUSTC" \
CARGO_TARGET_DIR="$probe_target" \
RQUICKJS_SYS_NO_WASI_SDK=1 \
CC_wasm32_wasmer_wasi="$WASI_SDK/bin/clang" \
AR_wasm32_wasmer_wasi="$WASI_SDK/bin/llvm-ar" \
CFLAGS_wasm32_wasmer_wasi="--target=wasm32-wasip1 -DQUICKJS_WASIX_PROBE" \
    cargo --config "patch.crates-io.rquickjs-sys.path='$sys_src'" build --locked --target wasm32-wasmer-wasi

"${WASMER_BIN:-wasmer}" run "$probe_target/wasm32-wasmer-wasi/debug/quickjs-wasix-rust-probe.wasm"
