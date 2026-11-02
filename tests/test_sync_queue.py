"""No game/Discord calls: contention, failure, crash and thread cancellation."""
import asyncio
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from services import sync_queue


class SyncQueueTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "sync.lock"
        self.path_patch = patch.object(sync_queue, "LOCK_PATH", self.path)
        self.path_patch.start()

    def tearDown(self):
        self.path_patch.stop()
        self.temp.cleanup()

    async def test_fifo_and_nested_calls(self):
        entered = asyncio.Event()
        release = asyncio.Event()
        order = []

        @sync_queue.serialized_sync("nested")
        async def nested():
            order.append("nested")

        @sync_queue.serialized_sync("sync")
        async def job(n):
            order.append(n)
            if n == 1:
                entered.set()
                await release.wait()
                await nested()

        first = asyncio.create_task(job(1))
        await entered.wait()
        second = asyncio.create_task(job(2))
        third = asyncio.create_task(job(3))
        await asyncio.sleep(0)
        self.assertEqual(order, [1])
        release.set()
        await asyncio.wait_for(asyncio.gather(first, second, third), 3)
        self.assertEqual(order, [1, "nested", 2, 3])

    async def test_failure_releases_slot(self):
        @sync_queue.serialized_sync("failure")
        async def fail():
            raise ValueError("expected")

        @sync_queue.serialized_sync("next")
        async def next_job():
            return 42

        with self.assertRaises(ValueError):
            await fail()
        self.assertEqual(await asyncio.wait_for(next_job(), 2), 42)

    async def test_cancelled_waiter_never_runs(self):
        started = asyncio.Event()
        release = asyncio.Event()
        ran = []

        @sync_queue.serialized_sync("first")
        async def first_job():
            started.set()
            await release.wait()

        @sync_queue.serialized_sync("waiting")
        async def waiting_job():
            ran.append(True)

        first = asyncio.create_task(first_job())
        await started.wait()
        waiter = asyncio.create_task(waiting_job())
        await asyncio.sleep(0)
        waiter.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await waiter
        release.set()
        await first
        self.assertEqual(ran, [])

    async def test_cancelled_thread_keeps_slot_until_completion(self):
        started = threading.Event()
        release = threading.Event()
        next_started = asyncio.Event()

        def blocking():
            started.set()
            if not release.wait(5):
                raise TimeoutError("test did not release worker")

        @sync_queue.serialized_sync("blocking")
        async def first_job():
            await asyncio.to_thread(blocking)

        @sync_queue.serialized_sync("next")
        async def next_job():
            next_started.set()

        first = asyncio.create_task(first_job())
        try:
            self.assertTrue(await asyncio.to_thread(started.wait, 2))
            first.cancel()
            following = asyncio.create_task(next_job())
            await asyncio.sleep(0.05)
            self.assertFalse(next_started.is_set())
            self.assertFalse(first.done())
        finally:
            release.set()
        with self.assertRaises(asyncio.CancelledError):
            await first
        await asyncio.wait_for(following, 2)

    async def test_process_crash_releases_shared_lock(self):
        # A distinct interpreter holds the very same file, like web vs bot.
        code = """
import sys,time
from pathlib import Path
from services.sync_queue import _try_lock
with Path(sys.argv[1]).open('a+b') as handle:
    handle.write(b'0'); handle.flush()
    assert _try_lock(handle)
    print('locked', flush=True)
    time.sleep(30)
"""
        child = subprocess.Popen(
            [sys.executable, "-c", code, str(self.path)],
            cwd=Path(__file__).resolve().parents[1],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        entered = asyncio.Event()

        @sync_queue.serialized_sync("other process")
        async def job():
            entered.set()

        task = None
        try:
            line = await asyncio.wait_for(asyncio.to_thread(child.stdout.readline), 5)
            self.assertEqual(line.strip(), b"locked")
            task = asyncio.create_task(job())
            await asyncio.sleep(0.3)
            self.assertFalse(entered.is_set())
            child.kill()
            await asyncio.to_thread(child.wait, 5)
            await asyncio.wait_for(task, 3)
            self.assertTrue(entered.is_set())
        finally:
            if child.poll() is None:
                child.kill()
            await asyncio.to_thread(child.wait, 5)
            child.stdout.close()
            child.stderr.close()
            if task and not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
