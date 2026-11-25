"""Bounded, read-only game facts and short conversations for the mention assistant."""
import json
import logging
import sqlite3
import time
from contextlib import closing
from datetime import datetime, timedelta, timezone

import database
from services import tb_schedule

MSK = timezone(timedelta(hours=3))
log = logging.getLogger(__name__)


def tb_calendar(now):
    """Use the same anchor and DST correction as TB orders, including evenings."""
    now = now.astimezone(MSK)
    anchor = tb_schedule.get_week_anchor_date()
    monday = now.date() - timedelta(days=now.weekday())
    for week in (monday - timedelta(days=7), monday):
        if ((week - anchor).days // 7) % 2:
            continue
        boundaries = []
        for day in range(7):
            date = week + timedelta(days=day)
            hour, minute = tb_schedule.apply_correction(
                20, 0, tb_schedule.effective_dst_offset_minutes(date))
            boundaries.append(datetime(date.year, date.month, date.day, hour, minute, tzinfo=MSK))
        for phase, (start, end) in enumerate(zip(boundaries, boundaries[1:]), 1):
            if start <= now < end:
                return {'phase': phase, 'ends_at': end.isoformat(),
                        'remaining_seconds': int((end - now).total_seconds()),
                        'source': 'расписание ТБ бота (якорь недели и поправка времени)',
                        'confirmed_live': False}
    return {'active': False, 'source': 'расписание ТБ бота', 'confirmed_live': False}


def tb_context(gid, now):
    # The live game endpoint has phase deadlines; Comlink's completed TB history
    # does not. Never infer the current phase from an old result or an order plan.
    with closing(sqlite3.connect(database.DB_NAME)) as db:
        exists = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='tb_live_snapshots'").fetchone()
        row = db.execute('SELECT captured,state FROM tb_live_snapshots WHERE guild_id=? ORDER BY captured DESC,id DESC LIMIT 1',
                         (gid,)).fetchone() if exists else None
    if row and 0 <= now.timestamp() - row[0] <= 900:
        state = json.loads(row[1])
        if state:
            end = float(state.get('end_ms') or 0) / 1000
            if 0 < end - now.timestamp() <= 2 * 86400 and 1 <= int(state.get('phase') or 0) <= 6:
                return {'phase': int(state['phase']), 'ends_at': datetime.fromtimestamp(end, MSK).isoformat(),
                        'remaining_seconds': int(end - now.timestamp()), 'source': 'сохранённый ответ игры',
                        'captured_at': datetime.fromtimestamp(row[0], MSK).isoformat(), 'confirmed_live': True}
    return tb_calendar(now)


def build(access, *, now=None):
    """Only allowlisted facts from the authorized guild; no raw account payloads."""
    now = now or datetime.now(MSK)
    gid = access['guild_id']
    context = {}
    cfg = database.get_guild_config(gid) or {}
    context['guild'] = {'name': cfg.get('name') or 'гильдия пользователя'}
    try:
        context['tb'] = tb_context(gid, now)
    except (sqlite3.Error, ValueError, TypeError, KeyError):
        log.warning('Assistant TB context unavailable', exc_info=False)
        context['tb'] = {'unavailable': True}
    # Reuse a small existing aggregate instead of re-reading all 50 rosters.
    try:
        with closing(sqlite3.connect(database.DB_NAME)) as db:
            exists = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='guild_daily_snapshots'").fetchone()
            row = db.execute('SELECT metrics_json,updated_at FROM guild_daily_snapshots WHERE guild_id=? ORDER BY day DESC LIMIT 1',
                             (gid,)).fetchone() if exists else None
        if row:
            metrics = json.loads(row[0])
            allowed = ('gp', 'members', 'g13', 'gls', 'zetas', 'omicrons', 'six_dot', 'speed20', 'modq', 'dc_l9')
            context['guild_snapshot'] = {'metrics': {k: metrics[k] for k in allowed if k in metrics},
                                         'updated_at': row[1], 'source': 'срез сохранённых ростеров Comlink'}
    except (sqlite3.Error, ValueError, TypeError):
        log.warning('Assistant guild snapshot unavailable', exc_info=False)
    return context


class Dialogue:
    """Three exchanges, 20 minutes, isolated by guild/channel/author, RAM only."""
    def __init__(self):
        self.entries = {}

    def get(self, key, *, now=None):
        now = time.monotonic() if now is None else now
        self.entries = {k: v for k, v in self.entries.items() if now - v[0] < 1200}
        return list(self.entries.get(key, (0, []))[1])

    def remember(self, key, question, answer, *, now=None):
        now = time.monotonic() if now is None else now
        turns = self.get(key, now=now)
        if key not in self.entries and len(self.entries) >= 128:
            self.entries.pop(min(self.entries, key=lambda k: self.entries[k][0]))
        turns.append({'question': question[:700], 'answer': answer[:1700]})
        self.entries[key] = (now, turns[-3:])
