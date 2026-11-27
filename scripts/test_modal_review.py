"""CPU-only tests for opt-in PR comment lifecycle; no Modal or GitHub calls."""
import importlib.util
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

def load():
    identity = lambda fn: fn
    image = Mock()
    image.entrypoint.return_value = image
    fake = SimpleNamespace(
        App=lambda name: SimpleNamespace(function=lambda **_: identity, local_entrypoint=lambda: identity),
        Image=SimpleNamespace(from_registry=lambda *_, **kw: image),
    )
    path = Path(__file__).with_name("modal_review.py")
    spec = importlib.util.spec_from_file_location("modal_review_fixture", path)
    module = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, {"modal":fake}):
        spec.loader.exec_module(module)
    return module

class CommentLifecycle(unittest.TestCase):
    def setUp(self):
        self.m = load()
        self.target={"nwo":"r33drichards/webhook","pr_number":11,"base":"b"*40,"head":"a"*40}
        self.m.public_target=Mock(return_value=self.target)
        self.m.assert_current=Mock()
        self.m.prepare=SimpleNamespace(remote=Mock(return_value=b"fixture"))
        result={"model_turns":3,"tool_calls":2,"review":{"findings":[]}}
        self.m.run_probe=SimpleNamespace(remote=Mock(return_value=result))
        self.m.github_json=Mock(side_effect=[{"id":12,"html_url":"start"},{"html_url":"finish"}])

    def test_last_turn_requests_final_json_without_tools(self):
        req={"instructions":"Review the PR."}
        self.assertFalse(self.m.reserve_final_turn(req,18,True))
        self.assertNotIn("tool_choice",req)
        self.assertTrue(self.m.reserve_final_turn(req,19,True))
        self.assertEqual(req["tool_choice"],"none")
        self.assertEqual(req["text"]["format"]["type"],"json_schema")
        with self.assertRaisesRegex(RuntimeError,"before reading"):
            self.m.reserve_final_turn({"instructions":"Review."},19,False)
        with self.assertRaisesRegex(RuntimeError,"after the final turn"):
            self.m.reserve_final_turn(req,20,True)

    def test_no_comment_by_default(self):
        self.m.main("r33drichards/webhook",11)
        self.m.github_json.assert_not_called()

    def test_completion_edits_start_comment(self):
        self.m.main("r33drichards/webhook",11,True)
        first, second=self.m.github_json.call_args_list
        self.assertEqual(first.kwargs["method"],"POST")
        self.assertEqual(second.kwargs["method"],"PATCH")
        self.assertTrue(second.args[0].endswith("comments/12"))
        self.assertIn("No actionable findings",second.kwargs["body"]["body"])

    def test_failure_edits_start_comment(self):
        self.m.run_probe.remote.side_effect=RuntimeError("fixture failed")
        with self.assertRaisesRegex(RuntimeError,"fixture failed"):
            self.m.main("r33drichards/webhook",11,True)
        last=self.m.github_json.call_args
        self.assertTrue(last.args[0].endswith("comments/12"))
        self.assertIn("did not publish findings",last.kwargs["body"]["body"])

    def test_stale_head_does_not_publish_findings(self):
        self.m.assert_current.side_effect=[None,None,RuntimeError("stale")]
        with self.assertRaisesRegex(RuntimeError,"stale"):
            self.m.main("r33drichards/webhook",11,True)
        self.assertIn("did not publish findings",self.m.github_json.call_args.kwargs["body"]["body"])

if __name__=="__main__":
    unittest.main()
