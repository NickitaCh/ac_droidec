"""Release unused Python/libc memory without discarding working caches or restarting.

RSS may remain high after a large JSON payload is freed: glibc retains arenas.
Collection plus malloc_trim can release unused pages, but cannot free live caches.
Log the actual result so retention is not mistaken for a diagnosed/fixed leak.
"""

import asyncio
import ctypes
import gc
import sys
from pathlib import Path

from services.sync_queue import serialized_sync

INTERVAL_SECONDS = 60 * 60
_task = None


def rss_mib():
    try:
        for line in Path("/proc/self/status").read_text().splitlines():
            if line.startswith("VmRSS:"):
                return round(int(line.split()[1]) / 1024, 1)
    except OSError:
        pass
    return None


def collect_unused():
    before = rss_mib()
    collected = gc.collect()
    trimmed = False
    if sys.platform == "linux":
        try:
            trim = ctypes.CDLL(None).malloc_trim
            trim.argtypes = [ctypes.c_size_t]
            trim.restype = ctypes.c_int
            trimmed = bool(trim(0))
        except (AttributeError, OSError):
            pass  # musl/other libc: normal Python collection still works.
    after = rss_mib()
    return {"before": before, "after": after, "collected": collected,
            "released": round(before - after, 1) if before is not None and after is not None else None,
            "trimmed": trimmed}


@serialized_sync("освобождение неиспользуемой памяти")
async def reclaim(label):
    result = await asyncio.to_thread(collect_unused)
    print("[Память] %s: RSS %s → %s МиБ; разница %s МиБ; GC %s объектов; malloc_trim=%s" %
          (label, result["before"], result["after"], result["released"], result["collected"], result["trimmed"]), flush=True)
    return result


async def _run(label):
    # Enqueue after the initial calculator starts. The same shared queue prevents
    # collection from competing with another process's heavy synchronization.
    await asyncio.sleep(60)
    while True:
        try:
            await reclaim(label)
        except Exception as exc:
            print("[Память] %s: обслуживание не выполнено: %s" % (label, exc), flush=True)
        await asyncio.sleep(INTERVAL_SECONDS)


def start(label):
    global _task
    if _task is None or _task.done():
        _task = asyncio.create_task(_run(label), name="memory-maintenance")
    return _task


async def stop():
    global _task
    if _task is not None:
        _task.cancel()
        try:
            await _task
        except asyncio.CancelledError:
            pass
        _task = None
