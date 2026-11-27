import sys
from pathlib import Path
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, Mock

for d in ("mwrev-publisher","mwrev-runner","mwrev-gateway"):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / d))
import service

class WorkerTest(IsolatedAsyncioTestCase):
    async def test_start_is_persisted_before_review_and_result(self):
        worker = service.Worker(AsyncMock(), SimpleNamespace(app_id=2, comments=True))
        head,base = "a"*40,"b"*40
        worker.store.claim.return_value = ("claim",(22,33,4,head,base))
        worker.store.reserve_start_comment.return_value = (None,)
        worker.store.remember_comment.return_value = True
        worker.store.finish.return_value = True
        api = Mock()
        api.get_pull.return_value = {"state":"open", "head":{"sha":head}, "base":{"sha":base},
                                     "labels":[{"name":"mwrev"}]}
        api.list_comments.return_value = []
        api.create_comment.return_value = 64
        worker.pull = Mock(return_value=("owner/repo","host-only",api))
        def review(*args):
            worker.store.remember_comment.assert_awaited_once_with(25,"claim",64)
            api.create_comment.assert_called_once()
            return {"findings":[]}
        worker.review = review
        self.assertEqual(await worker.execute(25), "completed")
        worker.store.finish.assert_awaited_once_with(25,"claim",{"state":"completed","review":{"findings":[]}})

    async def test_removed_label_does_not_start_or_infer(self):
        worker = service.Worker(AsyncMock(), SimpleNamespace(app_id=2, comments=True))
        worker.store.claim.return_value = ("claim",(22,33,4,"a"*40,"b"*40))
        api = Mock()
        api.get_pull.return_value = {"state":"open", "labels":[{"name":"wrev"}]}
        worker.pull = Mock(return_value=("owner/repo","host-only",api))
        worker.review = Mock()
        self.assertEqual(await worker.execute(25), "cancelled")
        worker.review.assert_not_called()
        api.create_comment.assert_not_called()
