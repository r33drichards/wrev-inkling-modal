#!/usr/bin/env bash
set -euo pipefail

wasmer run dist/wrev.wasm -- --help | grep -F 'wrev [login'
wasmer package build --check

request() {
  local method=$1 path=$2
  shift 2
  wasmer run dist/wrev.wasm \
    --env "REQUEST_METHOD=$method" \
    --env "PATH_INFO=$path" \
    --env WREV_WEBHOOK_SECRET=fixture-signing-key \
    --env WREV_REPOSITORIES=fixture/wrev \
    --env OPENAI_MODEL=fixture-model \
    "$@" -- wcgi
}

health=$(request GET /health </dev/null)
[[ "$health" == $'Status: 200 OK\r\n'* ]]
unsigned=$(printf '{}' | request POST /github/webhook)
[[ "$unsigned" == $'Status: 401 Unauthorized\r\n'* ]]
signature=$(python3 -c 'import hmac; print(hmac.digest(b"fixture-signing-key", b"{}", "sha256").hex())')
ping=$(printf '{}' | request POST /github/webhook \
  --env HTTP_X_GITHUB_EVENT=ping \
  --env "HTTP_X_HUB_SIGNATURE_256=sha256=$signature")
[[ "$ping" == $'Status: 204 No Content\r\n'* ]]
echo 'WCGI verified: health 200, unsigned webhook 401, signed ping 204'
