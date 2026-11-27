import unittest
from unittest.mock import Mock
from comment import start, finish, marker

ID = "66000e6a-eb46-48db-a837-197ed34a9b3b"
HEAD = "a" * 40
KEY = {"review_id": ID, "head": HEAD, "app_id": 51}
OWNED = {"id": 43, "performed_via_github_app": {"id": 51},
         "body": "started\n\n" + marker(ID)}

class CommentTest(unittest.TestCase):
    def test_recovers_lost_post_response_by_app_marker(self):
        api = Mock()
        api.list_comments.side_effect = [[], [OWNED]]
        api.create_comment.side_effect = TimeoutError()
        with self.assertRaises(TimeoutError):
            start(api, **KEY)
        self.assertEqual(start(api, **KEY), 43)
        api.create_comment.assert_called_once()

    def test_rejects_wrong_app_comment_before_edit(self):
        api = Mock()
        api.get_comment.return_value = {**OWNED, "performed_via_github_app": {"id": 99}}
        with self.assertRaises(RuntimeError):
            finish(api, **KEY, comment_id=43, outcome="failed")
        api.edit_comment.assert_not_called()

    def test_moved_head_suppresses_findings(self):
        api = Mock()
        api.get_comment.return_value = OWNED
        api.get_pull.return_value = {"state": "open", "head": {"sha":"b" * 40},
                                    "labels": [{"name": "mwrev"}]}
        self.assertEqual(finish(api, **KEY, comment_id=43, outcome="completed", findings_count=2),
                         "superseded")
        self.assertNotIn("findings", api.edit_comment.call_args.args[1])
        api.create_comment.assert_not_called()
