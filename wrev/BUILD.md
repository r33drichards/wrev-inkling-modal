# Build and deploy wrev

## Wasmtime

Install [Wasmtime 51](https://docs.wasmtime.dev/cli-install.html) and the Rust
WASI Preview 2 target. The QuickJS build obtains WASI SDK automatically. Set
`WASI_SDK` to an existing SDK directory to use a local installation.

Create a request JSON file containing `nwo` (the GitHub owner/repository),
`pr_number`, `base` and `head` (full Git SHAs). Include `title` and `body`
when available. A trusted host should build the request from the queued PR
event or GitHub metadata and verify review eligibility before running it.

```sh
cargo build --locked --release --bin wrev
# Set WREV_GITHUB_TOKEN in the host environment. The mount path must be new.
target/release/wrev prepare-review request.json ./evidence
rustup target add wasm32-wasip2
cargo build --locked --release --target wasm32-wasip2 --bin wrev
wasmtime run -S http --dir ./evidence::/work --dir ./request-input::/input \
  --env OPENAI_MODEL --env CHATGPT_ACCESS_TOKEN \
  target/wasm32-wasip2/release/wrev.wasm review /input/request.json \
  > ./evidence/out/findings.json
```

Place a copy of `request.json` in the dedicated `./request-input` directory
before running Wasmtime. Supply immutable evidence per review. The directory
mirrors ReviewBench's `repo/`, `pr/pr.json`, `pr/diff.patch`, and `out/`.
Wrev also stages `base/` so the reviewer can compare the pinned trees. The pre-turn hook matches the mounted
metadata against the request before inference. QuickJS exposes
`fs.readText(path, {startLine, maxLines})` and `fs.listDir(path, {offset})`
for paginated, read-only access to the evidence under `/work`.
GitHub credentials stay with the host. The component uses WASI HTTP for
model requests and writes findings JSON to standard output.

### Per-session SQLite checkpoints

Build the component with `--features sqlite-backend` and use the WASI SDK's
`clang` as `CC_wasm32_wasip2`. Preopen a directory for this session and set
`WREV_SESSION_DIR` to its guest path. wrev stores its checkpoints in
`review.sqlite` within that directory. Reusing the directory resumes the
same PR and pinned commits. The host must run at most one worker per
directory. A checkpoint saved during a tool call stops recovery because
the tool outcome is uncertain.

## Existing Wasmer deployment

The WASIX build packages the `wrev` binary with embedded QuickJS-NG. Use
Wasmer 7.5.0, cargo-wasix 0.1.34, and WASI SDK 34. Install cargo-wasix and
run `cargo wasix download-toolchain v2026-09-18.1+rust-1.97` before building.
On Linux x86-64, make the toolchain linker executable with
`chmod +x "$(rustc +wasix --print sysroot)/lib/rustlib/x86_64-unknown-linux-gnu/bin/gcc-ld/ld.lld"`.

```sh
export CC_wasm32_wasmer_wasi=/path/to/wasi-sdk/bin/clang
python3 scripts/build_wasix.py --features mysql-backend
bash tests/wasmer_smoke.sh
```

The build script verifies the pinned Octocrab, rmcp, and rquickjs-sys source archives,
applies the WASIX compatibility changes in an ignored build directory, and
produces `dist/wrev.wasm`. It uses `wasix/Cargo.lock`. After changing the
dependency graph, run the build with `--update-lock` and commit the updated
lockfile after the build passes.

## GitHub Actions deployment

The [Wasmer workflow](.github/workflows/wasmer.yml) runs on manual dispatch
from `main`. The build job validates the package and uploads it. The deployment
job uses the GitHub `production` environment and reads `WASMER_TOKEN` from its
secrets. It deploys the artifact to `r33drichards/wrev` with a commit-specific
package version. It checks the deployment URL for a healthy WCGI handler and
verifies that an unsigned webhook receives HTTP 401.

Configure `WREV_WEBHOOK_SECRET`, `WREV_REPOSITORIES`, and
`OPENAI_MODEL` on the Wasmer app. The WCGI host must supply an immutable
per-request mount with `WREV_SNAPSHOT_DIR` pointing to its guest path.
The package starts `wrev wcgi` for each HTTP request. See
[the listener setup](README.md#listen-for-the-label) and
[hosted credential setup](README.md#hosted-chatgpt-credentials) for the webhook
and MySQL configuration. Apply the hosted credential migration separately
before enabling the credential endpoint. The workflow uses only the Wasmer
deployment token.
