"""Pinned, authenticated Inkling B200 service candidate for Modal."""
import json
import os
import subprocess

import modal

IMAGE = (
    "vllm/vllm-openai@sha256:"
    "f5aca5f2081c8a7b9cadb046bd59c02dc3dcf214dd3254f9154a099ce9505b1a"
)
MODEL = "thinkingmachines/Inkling-2-Mini"
REVISION = "c9d028302cd0f035fdf8e7e91f089a4ce94d2195"
PORT = 8000
CACHE_MOUNT = "/model-cache"

# An environment-scoped cache must be provisioned explicitly before deployment.
cache = modal.Volume.from_name("mwrev-inkling-cache", create_if_missing=False)
image = modal.Image.from_registry(IMAGE, add_python="3.12").entrypoint([])
app = modal.App("mwrev-inkling")

def server_command():
    return [
        "vllm", "serve", MODEL, "--revision", REVISION,
        "--host", "0.0.0.0", "--port", str(PORT),
        "--tensor-parallel-size", "1", "--language-model-only",
        "--reasoning-parser", "inkling", "--tool-call-parser", "inkling",
        "--enable-auto-tool-choice",
        "--speculative-config", json.dumps({
            "method": "dspark", "num_speculative_tokens": 7,
            "draft_sample_method": "probabilistic",
        }),
    ]

@app.function(
    image=image, gpu="B200", cpu=4, memory=16384,
    volumes={CACHE_MOUNT: cache}, min_containers=0,
    max_containers=1, scaledown_window=60, timeout=240, startup_timeout=900,
)
@modal.concurrent(max_inputs=4)
@modal.web_server(PORT, startup_timeout=900, requires_proxy_auth=True)
def serve():
    env = {
        **os.environ,
        "HF_HOME": f"{CACHE_MOUNT}/{REVISION}",
        "HF_HUB_DISABLE_TELEMETRY": "1",
        "VLLM_NO_USAGE_STATS": "1",
    }
    env.pop("HF_TOKEN", None)
    env.pop("HUGGING_FACE_HUB_TOKEN", None)
    print("Starting pinned Inkling inference; gpu=B200 tp=1", flush=True)
    subprocess.Popen(server_command(), env=env)
