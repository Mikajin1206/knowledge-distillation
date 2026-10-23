"""Focused tests for publication progress polling without a database or waits."""
import unittest
from unittest.mock import MagicMock, call, patch

from tests._path import *  # noqa: F401,F403
from ckp import knowledge_tools


class _FakeServer:
    def __init__(self):
        self.tools = {}

    def tool(self, **_kwargs):
        def register(fn):
            self.tools[fn.__name__] = fn
            return fn
        return register


def _task(status="queued", phase="embedding_and_writing", completed=0,
          total=3, error=None):
    return {
        "publish_task_id": "publish-1", "knowledge_id": "knowledge-1",
        "status": status, "phase": phase, "completed": completed,
        "total": total, "error": error, "title": "测试知识", "filenames": [],
    }


class GetPublishTaskProgressTests(unittest.TestCase):
    def setUp(self):
        self.server = _FakeServer()
        knowledge_tools.register_knowledge_tools(
            self.server, lambda: object(), object(), lambda fn: fn)
        self.get_task = self.server.tools["get_publish_task"]
        self.service = MagicMock()
        self.knowledge = MagicMock(return_value=self.service)
        self.knowledge.return_value.__enter__.return_value = self.service

    def test_zero_wait_queries_once_and_returns_immediately(self):
        self.service.task.return_value = _task()
        with patch.object(knowledge_tools, "Knowledge", self.knowledge), \
             patch.object(knowledge_tools, "report_progress") as report, \
             patch.object(knowledge_tools.time, "sleep") as sleep:
            result = self.get_task("publish-1", wait_seconds=0)

        self.assertEqual("queued", result["status"])
        self.assertEqual(1, self.service.task.call_count)
        report.assert_called_once()
        sleep.assert_not_called()
        self.knowledge.assert_called_once()
        self.assertTrue(self.knowledge.call_args.kwargs["readonly"])

    def test_terminal_state_returns_without_sleeping(self):
        self.service.task.return_value = _task(
            status="published", phase="completed", completed=3, total=3)
        with patch.object(knowledge_tools, "Knowledge", self.knowledge), \
             patch.object(knowledge_tools, "report_progress") as report, \
             patch.object(knowledge_tools.time, "sleep") as sleep:
            result = self.get_task("publish-1", wait_seconds=30)

        self.assertEqual("published", result["status"])
        self.service.task.assert_called_once_with("publish-1")
        report.assert_called_once()
        sleep.assert_not_called()

    def test_unchanged_state_does_not_repeat_notification(self):
        self.service.task.side_effect = [
            _task(), _task(), _task(status="publish_failed", phase="failed", error="failed")
        ]
        with patch.object(knowledge_tools, "Knowledge", self.knowledge), \
             patch.object(knowledge_tools, "report_progress") as report, \
             patch.object(knowledge_tools.time, "monotonic", return_value=0), \
             patch.object(knowledge_tools.time, "sleep"):
            result = self.get_task("publish-1", wait_seconds=2)

        self.assertEqual("publish_failed", result["status"])
        self.assertEqual(3, self.service.task.call_count)
        self.assertEqual(2, report.call_count)
        self.assertEqual([call(None, 0, None, "测试知识：生成向量并写入 0/3 块"),
                          call(None, 1, None, "测试知识：入库失败 0/3 块")], report.call_args_list)

    def test_wait_range_rejects_out_of_bounds_before_opening_service(self):
        for seconds in (-1, 31):
            with self.subTest(wait_seconds=seconds), \
                 patch.object(knowledge_tools, "Knowledge", self.knowledge), \
                 patch.object(knowledge_tools, "report_progress") as report:
                with self.assertRaisesRegex(ValueError, "0 到 30"):
                    self.get_task("publish-1", wait_seconds=seconds)
                self.knowledge.assert_not_called()
                report.assert_not_called()


if __name__ == "__main__":
    unittest.main()
