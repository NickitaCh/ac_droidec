"""Officer event watches share one reader per account in each worker."""
import asyncio
from dataclasses import dataclass, field
import logging
import time

from services import event_panels, game_account, game_account_readonly, game_event_live, tb_live, tb_live_order

INTERVALS = (60, 120, 300, 600)
KINDS = {"tb": "ТБ", "tw": "ВГ", "raid": "рейд"}
log = logging.getLogger(__name__)
_SESSION_ENDED = "Войдите заново, чтобы продолжить наблюдение."


@dataclass
class Watch:
    interval: int
    wake: asyncio.Event = field(default_factory=asyncio.Event)
    task: asyncio.Task | None = None
    status: str = "starting"
    message: str = "Подключаемся к игре."


@dataclass
class Account:
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    watches: dict = field(default_factory=dict)
    login: asyncio.Task | None = None
    session_ended: bool = False


_accounts = {}


def get(guild_id, owner, kind):
    account = _accounts.get((guild_id, owner))
    return account.watches.get(kind) if account else None


def _running(watch):
    return bool(watch and watch.task and not watch.task.done())


def active(guild_id, owner, kind):
    return _running(get(guild_id, owner, kind))


async def _capture(reader, guild_id, owner, kind):
    data = await reader(kind)
    captured = int(time.time())
    if kind == "tb":
        if data:
            await asyncio.to_thread(tb_live_order.capture, guild_id, data)
        await asyncio.to_thread(tb_live.save, guild_id, owner, captured, data)
    else:
        await asyncio.to_thread(game_event_live.save, guild_id, owner, kind, captured, data)
    try:
        await event_panels.publish(guild_id, kind, data, captured)
    except Exception as error:
        log.warning("Event panel update failed (%s)", type(error).__name__)
    return data


def _end_session(account):
    account.session_ended = True
    for watch in account.watches.values():
        if _running(watch):
            watch.wake.set()


async def refresh(owner, guild_id, profile, tier, code, kind):
    account = _accounts.setdefault((guild_id, owner), Account())
    async with account.lock:
        has_watch = any(_running(watch) for watch in account.watches.values())
        try:
            if has_watch:
                if account.session_ended:
                    raise game_account.SessionExpired(_SESSION_ENDED)
                reader = await asyncio.shield(account.login)
            else:
                reader = await game_account_readonly.login_for_events(owner, guild_id, profile, tier, code)
            return await _capture(reader, guild_id, owner, kind)
        except game_account.SessionExpired:
            _end_session(account)
            raise


async def refresh_saved(owner, guild_id, profile, tier, code, kind, *, reconnect_if_expired=False):
    account = _accounts.setdefault((guild_id, owner), Account())
    async with account.lock:
        async def reader(event):
            return await game_account_readonly.read(owner, guild_id, profile, tier, code, event=event)
        try:
            return await _capture(reader, guild_id, owner, kind)
        except game_account.SessionExpired:
            if not reconnect_if_expired:
                _end_session(account)
                raise
            try:
                await game_account.reconnect(owner, guild_id, profile, tier, code)
                return await _capture(reader, guild_id, owner, kind)
            except game_account.AccountError:
                _end_session(account)
                raise


async def start(owner, guild_id, profile, tier, code, kind, interval, *, saved_session=False):
    if kind not in KINDS or interval not in INTERVALS:
        raise ValueError("Выберите событие и интервал наблюдения.")
    account = _accounts.setdefault((guild_id, owner), Account())
    async with account.lock:
        if _running(account.watches.get(kind)):
            return
        if not any(_running(watch) for watch in account.watches.values()):
            account.session_ended = False
            async def load_reader():
                if saved_session:
                    async def reader(event):
                        return await game_account_readonly.read(owner, guild_id, profile, tier, code, event=event)
                    return reader
                return await game_account_readonly.login_for_events(owner, guild_id, profile, tier, code)
            account.login = asyncio.create_task(load_reader())
        watch = Watch(interval)
        account.watches[kind] = watch
        watch.task = asyncio.create_task(_run(account, watch, owner, guild_id, kind, delay_first=saved_session))


async def _run(account, watch, owner, guild_id, kind, *, delay_first=False):
    try:
        reader = await asyncio.shield(account.login)
        if delay_first:
            watch.status = "watching"
            watch.message = "Наблюдение идёт."
            try:
                await asyncio.wait_for(watch.wake.wait(), timeout=watch.interval)
            except TimeoutError:
                pass
        while not account.session_ended and not watch.wake.is_set():
            data = await _capture(reader, guild_id, owner, kind)
            event_active = bool(data and (kind != "raid" or data["active"]))
            if not event_active:
                watch.message = f"Игра не вернула активное событие «{KINDS[kind]}». Наблюдение завершено."
                break
            if watch.wake.is_set():
                break
            watch.status = "watching"
            watch.message = "Наблюдение идёт."
            try:
                await asyncio.wait_for(watch.wake.wait(), timeout=watch.interval)
            except TimeoutError:
                continue
    except game_account.SessionExpired:
        _end_session(account)
    except game_account.AccountError as error:
        watch.message = str(error)
    except Exception:
        log.exception("Event watch failed")
        watch.message = "Не удалось получить снимок. Наблюдение остановлено."
    finally:
        if account.session_ended:
            watch.message = _SESSION_ENDED
        elif watch.status == "stopping":
            watch.message = "Наблюдение остановлено вручную."
        watch.status = "ended"
        if not any(other is not watch and _running(other) for other in account.watches.values()):
            account.login = None


def stop(guild_id, owner, kind):
    watch = get(guild_id, owner, kind)
    if _running(watch):
        watch.status = "stopping"
        watch.message = "Останавливаем наблюдение."
        watch.wake.set()


async def shutdown():
    running = [task for account in _accounts.values()
               for task in (account.login, *(watch.task for watch in account.watches.values()))
               if task and not task.done()]
    for task in running:
        task.cancel()
    await asyncio.gather(*running, return_exceptions=True)
    _accounts.clear()
