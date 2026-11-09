"""Persistent progress for manual dashboard sync; the request only starts a job."""
import asyncio
import sqlite3
import time
import uuid

import database
from services import activity_diff
from services.sync_queue import serialized_sync

_tasks = set()
ACTIVE = ('queued', 'running')


def _connect():
    c = sqlite3.connect(database.DB_NAME, timeout=20)
    c.row_factory = sqlite3.Row
    c.execute('''CREATE TABLE IF NOT EXISTS dashboard_sync_jobs (
        guild_id INTEGER PRIMARY KEY, token TEXT NOT NULL, state TEXT NOT NULL,
        total INTEGER NOT NULL DEFAULT 0, completed INTEGER NOT NULL DEFAULT 0,
        failed INTEGER NOT NULL DEFAULT 0, added INTEGER NOT NULL DEFAULT 0,
        heartbeat REAL NOT NULL, message TEXT NOT NULL DEFAULT '')''')
    c.commit()
    return c


def status(gid):
    c = _connect()
    try:
        c.execute("UPDATE dashboard_sync_jobs SET state='error', message=? WHERE guild_id=? AND state IN ('queued','running') AND heartbeat<?",
                  ('Обновление прервано. Запустите его ещё раз.', gid, time.time() - 120))
        c.commit()
        row = c.execute('SELECT * FROM dashboard_sync_jobs WHERE guild_id=?', (gid,)).fetchone()
        result = dict(row) if row else {'state': 'idle'}
        result.pop('token', None)
        result.pop('heartbeat', None)
        return result
    finally:
        c.close()


def _claim(gid):
    c = _connect()
    try:
        c.execute('BEGIN IMMEDIATE')
        row = c.execute('SELECT state,heartbeat FROM dashboard_sync_jobs WHERE guild_id=?', (gid,)).fetchone()
        if row and row['state'] in ACTIVE and row['heartbeat'] > time.time() - 120:
            return None
        token = uuid.uuid4().hex
        c.execute('INSERT OR REPLACE INTO dashboard_sync_jobs(guild_id,token,state,heartbeat) VALUES(?,?,?,?)',
                  (gid, token, 'queued', time.time()))
        c.commit()
        return token
    finally:
        c.close()


def _update(gid, token, **fields):
    fields['heartbeat'] = time.time()
    c = _connect()
    try:
        c.execute('UPDATE dashboard_sync_jobs SET ' + ','.join(k + '=?' for k in fields) + ' WHERE guild_id=? AND token=?',
                  (*fields.values(), gid, token))
        c.commit()
    finally:
        c.close()


@serialized_sync('ручное обновление гильдии')
async def _sync(gid, token, comlink):
    from datetime import datetime
    from zoneinfo import ZoneInfo
    codes = [code for _, code, _ in await asyncio.to_thread(database.get_all_user_mappings, gid)]
    if not codes:
        raise ValueError('В составе нет игроков. Дождитесь загрузки состава гильдии.')
    thresholds = await asyncio.to_thread(database.get_all_skill_tier_thresholds)
    today = datetime.now(ZoneInfo('Europe/Moscow')).date().isoformat()
    await asyncio.to_thread(_update, gid, token, state='running', total=len(codes))
    completed = failed = added = 0
    semaphore = asyncio.Semaphore(6)
    progress_lock = asyncio.Lock()

    async def one(code):
        nonlocal completed, failed, added
        async with semaphore:
            count, error = 0, False
            try:
                gids = await asyncio.to_thread(database.get_guild_ids_for_ally_code, code)
                ok, count, _ = await activity_diff.sync_player(comlink, code, gids or {gid}, today, thresholds)
                error = not ok
            except Exception as exc:
                error = True
                print(f'⚠️ [Ручное обновление] {gid}/{code}: {exc}', flush=True)
            async with progress_lock:
                completed += 1
                failed += int(error)
                added += count
                await asyncio.to_thread(_update, gid, token, completed=completed, failed=failed, added=added)

    await asyncio.gather(*(one(code) for code in codes))
    await asyncio.to_thread(_update, gid, token, state='done',
                            message=f'Обновлено {completed - failed} из {len(codes)}. Новых событий: {added}.'
                            + (f' Не удалось обновить: {failed}. Можно повторить.' if failed else ''))


async def _run(gid, token, comlink):
    async def heartbeat():
        while True:
            await asyncio.sleep(20)
            await asyncio.to_thread(_update, gid, token)
    pulse = asyncio.create_task(heartbeat())
    try:
        await _sync(gid, token, comlink)
    except asyncio.CancelledError:
        await asyncio.to_thread(_update, gid, token, state='error', message='Обновление прервано. Запустите его ещё раз.')
        raise
    except Exception as exc:
        print(f'⚠️ [Ручное обновление] {gid}: {exc}', flush=True)
        await asyncio.to_thread(_update, gid, token, state='error', message='Не удалось обновить состав. Повторите позже.')
    finally:
        pulse.cancel()
        await asyncio.gather(pulse, return_exceptions=True)


async def start(gid, comlink):
    token = await asyncio.to_thread(_claim, gid)
    if token:
        task = asyncio.create_task(_run(gid, token, comlink))
        _tasks.add(task)
        task.add_done_callback(_tasks.discard)
    return await asyncio.to_thread(status, gid)
