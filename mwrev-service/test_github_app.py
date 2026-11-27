import sys
import types
import unittest
from unittest.mock import Mock, patch
import github_app

class GitHubAppTest(unittest.TestCase):
    def test_scoped_read_and_write_installation_tokens(self):
        req = Mock(return_value={"token":"fake-token"})
        jwt = types.SimpleNamespace(encode=Mock(return_value="fake-jwt"))
        with patch.dict(sys.modules, jwt=jwt):
            self.assertEqual(github_app.installation_token(1,"fixture",2,3,request=req), "fake-token")
            self.assertEqual(req.call_args.args, ("POST", "/app/installations/2/access_tokens",
                "fake-jwt", {"repository_ids":[3],"permissions":{"pull_requests":"read","contents":"read"}}))
            github_app.installation_token(1,"fixture",2,3,write=True,request=req)
            self.assertEqual(req.call_args.args[-1]["permissions"], {"pull_requests":"write"})

    def test_comment_routes_and_pagination(self):
        req = Mock(side_effect=[[{"id":x} for x in range(100)], [{"id":201}], {"id":213}, {}])
        pr = github_app.Pull("owner/repo", 4, "fixture", request=req)
        self.assertEqual(len(list(pr.list_comments())), 101)
        self.assertEqual(pr.create_comment("test"), 213)
        pr.edit_comment(213, "done")
        self.assertEqual(req.call_args.args, ("PATCH", "/repos/owner/repo/issues/comments/213", "fixture", {"body":"done"}))
        with self.assertRaises(ValueError):
            github_app.Pull("owner/..", 4, "fixture")
