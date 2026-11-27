import unittest
from unittest.mock import Mock
from comment import COMMENT_BYTES, start, finish, marker

ID = "66000e6a-eb46-48db-a837-197ed34a9b3b"
HEAD = "a" * 40
BASE = "b" * 40
KEY = {"review_id": ID, "head": HEAD, "app_id": 51}
TERMINAL = {**KEY, "base": BASE, "comment_id": 43}
ELIGIBLE = {"state":"open", "head":{"sha":HEAD}, "base":{"sha":BASE}, "labels":[{"name":"mwrev"}]}
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
            finish(api, **TERMINAL, outcome="failed")
        api.edit_comment.assert_not_called()

    def test_moved_head_suppresses_findings(self):
        api = Mock()
        api.get_comment.return_value = OWNED
        api.get_pull.return_value = {"state": "open", "head": {"sha":"b" * 40},
                                    "labels": [{"name": "mwrev"}]}
        self.assertEqual(finish(api, **TERMINAL, outcome="completed", findings=[{"message":"a"},{"message":"b"}]),
                         "superseded")
        self.assertNotIn("findings", api.edit_comment.call_args.args[1])
        api.create_comment.assert_not_called()


    def test_renders_persisted_order_and_neutralizes_model_markdown(self):
        api = Mock(repo="owner/repo")
        api.get_comment.return_value = OWNED
        api.get_pull.return_value = ELIGIBLE
        values = [
            {"file":"src/a b#é.rs", "start_line":3, "end_line":5,
             "message":"First: @user ![img](https://outside.example/pixel) <tag>"},
            {"file":"../escape", "start_line":6, "end_line":6, "message":"Second issue"},
        ]
        self.assertEqual(finish(api, **TERMINAL, outcome="completed", findings=values), "completed")
        body = api.edit_comment.call_args.args[1]
        self.assertLess(body.index("First"), body.index("Second issue"))
        self.assertIn(f"https://github.com/owner/repo/blob/{HEAD}/src/a%20b%23%C3%A9.rs#L3-L5", body)
        for raw in ("@user", "https://outside", "<tag>", "../escape"):
            self.assertNotIn(raw, body)
        self.assertIn("&#64;user", body)
        self.assertIn(marker(ID), body.splitlines())
        api.create_comment.assert_not_called()

    def test_moved_base_does_not_publish_findings(self):
        api = Mock(repo="owner/repo")
        api.get_comment.return_value = OWNED
        api.get_pull.return_value = {**ELIGIBLE, "base":{"sha":"c"*40}}
        self.assertEqual(finish(api, **TERMINAL, outcome="completed",
                                findings=[{"message":"do not publish me"}]), "superseded")
        self.assertNotIn("do not publish me", api.edit_comment.call_args.args[1])
        api.create_comment.assert_not_called()

    def test_overflow_posts_once_if_original_edit_fails(self):
        api = Mock(repo="owner/repo")
        api.get_comment.return_value = OWNED
        api.get_pull.return_value = ELIGIBLE
        pages = []
        def create(body):
            pages.append({**OWNED, "id":44+len(pages), "body":body})
            return pages[-1]["id"]
        api.create_comment.side_effect = create
        api.list_comments.side_effect = lambda: iter(pages)
        api.edit_comment.side_effect = [RuntimeError("lost edit"), None]
        huge = {"file":"src/a.rs","start_line":1,"end_line":1,
                "message":"@" * 20000 + "END_OF_FINDING"}
        with self.assertRaisesRegex(RuntimeError, "lost edit"):
            finish(api, **TERMINAL, outcome="completed", findings=[huge])
        count = len(pages)
        self.assertGreaterEqual(count, 1)
        self.assertEqual(finish(api, **TERMINAL, outcome="completed", findings=[huge]), "completed")
        self.assertEqual(len(pages), count)
        self.assertEqual(api.create_comment.call_count, count)
        self.assertIn("END&#95;OF&#95;FINDING", pages[-1]["body"])
        self.assertIn("#issuecomment-44", api.edit_comment.call_args.args[1])
        for body in [page["body"] for page in pages] + [api.edit_comment.call_args.args[1]]:
            self.assertLessEqual(len(body.encode()), COMMENT_BYTES)
