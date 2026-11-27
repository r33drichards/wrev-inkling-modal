"""CPU-only Wasmtime/QuickJS/model framing acceptance test; no network."""
import json
import sys
import tempfile
from pathlib import Path

from wasm_runner import run, wasi_arguments

def main(wasmtime, component):
    with tempfile.TemporaryDirectory(prefix="mwrev-guest-") as tmp:
        work, inputs = Path(tmp) / "work", Path(tmp) / "inputs"
        for p in (work / "repo", work / "base", work / "pr", inputs):
            p.mkdir(parents=True, exist_ok=True)
        marker = "mwrev-fixture-current-head"
        (work / "repo" / "example.txt").write_text(marker + "\n")
        (work / "base" / "example.txt").write_text("base\n")
        (work / "pr" / "diff.patch").write_text("-base\n+" + marker + "\n")
        meta = {"nwo": "example/wrev-fixture", "pr_number": 23,
                "base": "a" * 40, "head": "b" * 40, "title": "Fixture", "body": ""}
        for p in (work / "pr" / "pr.json", inputs / "request.json"):
            p.write_text(json.dumps(meta))
        turns = 0
        def model(body, timeout_seconds):
            nonlocal turns
            turns += 1
            if turns == 1:
                return [{"type": "function_call", "id": "fc_fixture",
                         "call_id": "call_fixture", "name": "run_js",
                         "arguments": json.dumps({"code": "const p = await fs.readText('/work/repo/example.txt'); console.log(p.text);"})}]
            if turns != 2:
                raise RuntimeError("unexpected fixture turn")
            hits = [v for v in body["input"] if v.get("type") == "function_call_output"
                    and v.get("call_id") == "call_fixture" and marker in str(v.get("output"))]
            if len(hits) != 1:
                raise RuntimeError("model did not receive the exact tool result")
            return [{"type": "message", "role": "assistant", "id": "msg_fixture",
                     "content": [{"type": "output_text", "text": '{"findings":[]}'}]}]
        result = run(wasi_arguments(Path(wasmtime).resolve(), Path(component).resolve(),
                                    work, inputs), model,
                     {"head": meta["head"], "base": meta["base"], "pr_number": 23,
                      "repo": "https://github.com/example/wrev-fixture"})
        if turns != 2 or result["findings"] != []:
            raise RuntimeError("unexpected fixture outcome")
        print("Wasmtime mounted evidence, tool call, result, and pinned review passed")

if __name__ == "__main__":
    main(*sys.argv[1:])
