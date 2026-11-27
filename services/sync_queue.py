"""Serialize memory-heavy syncs across bot/web/tg sharing the application mount.

The OS releases the file lock even after a crash. Never delete the lock file:
replacing its inode would let a second process enter an active synchronization.
"""

import asyncio
import errno
import functools
import os
import time
import weakref
from contextlib import asynccontextmanager
from pathlib import Path

LOCK_PATH = Path(os.getenv("SHARED_CACHE_DIR", str(Path(__file__).resolve().parents[1] / "cache"))) / "heavy-sync.lock"
_locks = weakref.WeakKeyDictionary()
_owners = weakref.WeakSet()


def _try_lock(handle):
    if os.name == "nt":
        import msvcrt
        handle.seek(0)
        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            if exc.errno in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                return False
            raise
    else:
        import fcntl
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
    return True


def _unlock(handle):
    if os.name == "nt":
        import msvcrt
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl
        fcntl.flock(handle, fcntl.LOCK_UN)


@asynccontextmanager
async def shared_file_lock(path):
    """Hold an OS lock shared by workers using the same file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as handle:
        if handle.seek(0, 2) == 0:
            handle.write(b"0")
            handle.flush()
        while not _try_lock(handle):
            await asyncio.sleep(0.2)
        try:
            yield
        finally:
            _unlock(handle)


async def complete_on_cancel(coroutine):
    """Finish an operation already started when its caller is cancelled."""
    worker = asyncio.create_task(coroutine)
    try:
        return await asyncio.shield(worker)
    except asyncio.CancelledError:
        while not worker.done():
            try:
                await asyncio.shield(worker)
            except asyncio.CancelledError:
                continue
            except Exception:
                break
        if not worker.cancelled():
            worker.exception()
        raise


def serialized_sync(label, *, quiet=False):
    """FIFO within an event loop; mutually exclusive across application processes.

Cancellation waits for the running coroutine (and its to_thread calls) to finish
before releasing the slot. Waiting jobs can be cancelled without starting work.
Nested calls in the same task reuse the slot; unrelated child tasks do not.
"""
    def decorate(func):
        @functools.wraps(func)
        async def wrapped(*args, **kwargs):
            if asyncio.current_task() in _owners:
                return await func(*args, **kwargs)
            loop = asyncio.get_running_loop()
            lock = _locks.setdefault(loop, asyncio.Lock())
            queued = time.monotonic()
            if not quiet:
                print(f"⏳ [Синхронизация] В очереди: {label}", flush=True)
            async with lock, shared_file_lock(LOCK_PATH):
                started = time.monotonic()
                if not quiet:
                    print(f"▶ [Синхронизация] Начало: {label}; ожидание {started - queued:.1f} с", flush=True)

                async def run():
                    task = asyncio.current_task()
                    _owners.add(task)
                    try:
                        return await func(*args, **kwargs)
                    finally:
                        _owners.discard(task)

                try:
                    return await complete_on_cancel(run())
                finally:
                    if not quiet:
                        print(f"■ [Синхронизация] Завершено: {label}; {time.monotonic() - started:.1f} с", flush=True)
        return wrapped
    return decorate
