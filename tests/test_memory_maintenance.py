import unittest
from unittest.mock import patch

from services import memory_maintenance as memory


class MemoryTests(unittest.TestCase):
    def test_collection_reports_measured_reduction(self):
        with patch.object(memory, "rss_mib", side_effect=[1000.0, 600.0]), patch.object(memory.gc, "collect", return_value=42), patch.object(memory.sys, "platform", "win32"):
            result = memory.collect_unused()
        self.assertEqual(result["released"], 400.0)
        self.assertEqual(result["collected"], 42)
        self.assertFalse(result["trimmed"])

    def test_missing_proc_is_supported(self):
        with patch.object(memory, "rss_mib", return_value=None), patch.object(memory.gc, "collect", return_value=0), patch.object(memory.sys, "platform", "win32"):
            self.assertIsNone(memory.collect_unused()["released"])


class LifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_reconnect_starts_only_one_task_and_shutdown_cancels(self):
        first = memory.start("test")
        self.assertIs(memory.start("test"), first)
        await memory.stop()
        self.assertTrue(first.done())
        self.assertIsNone(memory._task)
