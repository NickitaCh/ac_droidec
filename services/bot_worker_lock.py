"""One gateway/notifier per platform and profile on the shared local volume."""

from contextlib import contextmanager
from pathlib import Path

import database
from services.bot_profiles import validate
from services.sync_queue import _try_lock


@contextmanager
def worker_instance(platform, profile):
    validate(profile)
    if platform not in {"discord", "telegram"}:
        raise ValueError("Unknown bot platform")
    path = Path(database.DB_NAME).resolve().parent / "cache" / f"{platform}-{profile}.worker.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as handle:
        if handle.seek(0, 2) == 0:
            handle.write(b"0")
            handle.flush()
        if not _try_lock(handle):
            raise RuntimeError(f"Бот {platform}/{profile} уже запущен.")
        yield
