"""Run a pinned, mounted Wrev component with no guest network or secrets."""
import json
import os
import select
import subprocess
import time
from pathlib import Path

MAX_FRAME_BYTES = 17 * 1024 * 1024
MAX_MODEL_REPLY_BYTES = 8 * 1024 * 1024
MAX_MODEL_TURNS = 20
REVIEW_TIMEOUT_SECONDS = 1500

class ReviewError(RuntimeError):
    pass

def wasi_arguments(wasmtime: Path, component: Path, snapshot: Path, request_dir: Path):
    return [str(wasmtime), "run", "--dir", f"{snapshot}::/work",
            "--dir", f"{request_dir}::/input",
            "--env", "WREV_MODEL_TRANSPORT=host-stdio",
            "--env", "OPENAI_MODEL=thinkingmachines/Inkling-2-Mini",
            "--env", "WREV_MODEL_TIMEOUT_SECS=180",
            "--env", "WREV_SCRIPT_MEMORY_BYTES=104857600",
            "--env", "WREV_SCRIPT_OUTPUT_BYTES=104857600",
            str(component), "review-snapshot", "/input/request.json", "/work"]

def remaining(deadline):
    left = deadline - time.monotonic()
    if left <= 0:
        raise ReviewError("review timed out")
    return left

class Frames:
    def __init__(self, stream):
        self.fd = stream.fileno()
        self.pending = bytearray()

    def read(self, deadline):
        while True:
            end = self.pending.find(b"\n")
            if end >= 0:
                raw = bytes(self.pending[:end])
                del self.pending[:end + 1]
                try:
                    value = json.loads(raw)
                except (ValueError, RecursionError) as exc:
                    raise ReviewError("invalid guest response frame") from exc
                if not isinstance(value, dict):
                    raise ReviewError("invalid guest response type")
                return value
            if len(self.pending) >= MAX_FRAME_BYTES:
                raise ReviewError("guest frame too large")
            if not select.select([self.fd], [], [], remaining(deadline))[0]:
                raise ReviewError("review timed out")
            chunk = os.read(self.fd, min(65536, MAX_FRAME_BYTES - len(self.pending)))
            if not chunk:
                raise ReviewError("guest exited without a complete result")
            self.pending.extend(chunk)

def send(stream, frame, deadline):
    data = json.dumps(frame, ensure_ascii=True, allow_nan=False).encode() + b"\n"
    if len(data) > MAX_MODEL_REPLY_BYTES:
        raise ReviewError("model output frame too large")
    fd = stream.fileno()
    os.set_blocking(fd, False)
    view = memoryview(data)
    while view:
        if not select.select([], [fd], [], remaining(deadline))[1]:
            raise ReviewError("review timed out writing model reply")
        try:
            view = view[os.write(fd, view):]
        except BlockingIOError:
            continue

def run(command, model_call, expected, *, synthetic_fixture_debug=False):
    """model_call(body, remaining_seconds) returns the completed output array."""
    deadline = time.monotonic() + REVIEW_TIMEOUT_SECONDS
    with subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                          stderr=None if synthetic_fixture_debug else subprocess.DEVNULL, env={}, bufsize=0) as guest:
        try:
            frames = Frames(guest.stdout)
            turns = 0
            while True:
                f = frames.read(deadline)
                if f.get("type") == "model.request" and isinstance(f.get("body"), dict):
                    turns += 1
                    if turns > MAX_MODEL_TURNS:
                        raise ReviewError("model turn cap exceeded")
                    output = model_call(f["body"], remaining(deadline))
                    if not isinstance(output, list):
                        raise ReviewError("model output is not an array")
                    send(guest.stdin, {"type": "model.response", "output": output}, deadline)
                elif f.get("type") == "review.result":
                    result = f.get("result")
                    pr = result.get("pr") if isinstance(result, dict) else None
                    if (turns < 1 or not isinstance(pr, dict) or
                            any(pr.get(k) != v for k, v in expected.items()) or
                            not isinstance(result.get("findings"), list)):
                        raise ReviewError("review did not produce findings for the pinned PR")
                    guest.stdin.close()
                    if guest.wait(timeout=remaining(deadline)) != 0:
                        raise ReviewError("guest failed after returning result")
                    return result
                else:
                    raise ReviewError("unknown guest frame")
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ReviewError("guest execution failed") from exc
        finally:
            if guest.poll() is None:
                guest.kill()
