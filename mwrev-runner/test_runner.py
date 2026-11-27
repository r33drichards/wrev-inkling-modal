import json
import sys
import unittest
from pathlib import Path
import wasm_runner

class RunnerTest(unittest.TestCase):
    def test_guest_pipe_and_pinned_result(self):
        expected = {"head": "expected", "pr_number": 23}
        command = [sys.executable, "-c", """
import json, os, sys
assert 'MODAL_SECRET' not in os.environ
print(json.dumps({'type':'model.request','body':{'input':[]}}), flush=True)
assert json.loads(sys.stdin.readline()) == {'type':'model.response','output':[]}
print(json.dumps({'type':'review.result','result':{'pr':{'head':'expected','pr_number':23},'findings':[]}}), flush=True)
"""]
        self.assertEqual(wasm_runner.run(command, lambda body, timeout: [], expected)["findings"], [])
        with self.assertRaisesRegex(wasm_runner.ReviewError, "pinned PR"):
            wasm_runner.run(command, lambda body, timeout: [], {"head": "stale"})

    def test_no_http_network_grant(self):
        args = wasm_runner.wasi_arguments(Path("/bin/wasmtime"), Path("/app/wrev.wasm"),
                                                  Path("/tmp/data"), Path("/tmp/input"))
        self.assertNotIn("http", args)
        self.assertNotIn("--inherit-env", args)
        self.assertIn("WREV_MODEL_TRANSPORT=host-stdio", args)

    def test_unexpected_frame_is_rejected(self):
        with self.assertRaisesRegex(wasm_runner.ReviewError, "unknown guest"):
            wasm_runner.run([sys.executable, "-c", "print('{}')"], print, {})


    def test_more_than_twenty_turns_and_old_frame_sizes(self):
        command = [sys.executable, "-c", """
import json, sys
for i in range(22):
    data = "x" * (18 * 1024 * 1024) if i == 21 else str(i)
    print(json.dumps({"type":"model.request","body":{"input":[data]}}), flush=True)
    reply = json.loads(sys.stdin.readline())
    assert reply["output"] == (["y" * (9 * 1024 * 1024)] if i == 21 else [])
print(json.dumps({"type":"review.result","result":{"pr":{},"findings":[]}}), flush=True)
"""]
        seen = []
        def model(body, remaining):
            seen.append(len(body["input"][0]))
            return ["y" * (9 * 1024 * 1024)] if len(seen) == 22 else []
        self.assertEqual(wasm_runner.run(command, model, {})["findings"], [])
        self.assertEqual((len(seen), seen[-1]), (22, 18 * 1024 * 1024))
