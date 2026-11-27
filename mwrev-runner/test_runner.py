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
