"""Check the release configuration without loading Modal or allocating GPUs."""
import importlib.util
import json
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch

class InferenceConfigTest(unittest.TestCase):
    def test_release_contract(self):
        recorded = {}
        def decorator(name):
            def register(*args, **kwargs):
                recorded[name] = (args, kwargs)
                return lambda f: f
            return register
        fake = types.SimpleNamespace(
            Volume=types.SimpleNamespace(from_name=decorator("volume")),
            Image=types.SimpleNamespace(from_registry=lambda *a, **k:
                types.SimpleNamespace(entrypoint=lambda x: "pinned-image")),
            App=lambda name: types.SimpleNamespace(function=decorator("function")),
            web_server=decorator("server"),
            concurrent=decorator("concurrent"),
        )
        spec = importlib.util.spec_from_file_location(
            "inkling", Path(__file__).with_name("inkling.py"))
        self.assertIsNotNone(spec)
        candidate = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, modal=fake):
            spec.loader.exec_module(candidate)
        cfg = recorded["function"][1]
        self.assertEqual((cfg["gpu"], cfg["min_containers"], cfg["max_containers"]), ("L40S", 0, 1))
        self.assertTrue(recorded["server"][1]["requires_proxy_auth"])
        self.assertEqual(cfg["startup_timeout"], 900)
        self.assertEqual(recorded["concurrent"][1]["max_inputs"], 4)
        self.assertFalse(recorded["volume"][1]["create_if_missing"])
        cmd = candidate.server_command()
        self.assertEqual(cmd[cmd.index("--max-model-len")+1], "131072")
        self.assertEqual(cmd[cmd.index("--max-num-seqs")+1], "4")
        self.assertEqual(cmd[cmd.index("--reasoning-parser")+1], "inkling")
        self.assertEqual(json.loads(cmd[cmd.index("--speculative-config")+1])["method"], "dspark")
        self.assertIn("@sha256:", candidate.IMAGE)

if __name__ == "__main__":
    unittest.main()
