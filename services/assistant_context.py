"""Bounded, read-only game facts and short conversations for the mention assistant."""
import asyncio
import json
import logging
import re
import sqlite3
import time
from contextlib import closing
from datetime import datetime, timedelta, timezone

import database
from services import raids, tb_schedule

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
        if state is None:
            return {'active': False, 'source': 'сохранённый ответ игры', 'confirmed_live': True,
                    'captured_at': datetime.fromtimestamp(row[0], MSK).isoformat()}
        if state:
            end = float(state.get('end_ms') or 0) / 1000
            if 0 < end - now.timestamp() <= 2 * 86400 and 1 <= int(state.get('phase') or 0) <= 6:
                return {'phase': int(state['phase']), 'ends_at': datetime.fromtimestamp(end, MSK).isoformat(),
                        'remaining_seconds': int(end - now.timestamp()), 'source': 'сохранённый ответ игры',
                        'captured_at': datetime.fromtimestamp(row[0], MSK).isoformat(), 'confirmed_live': True}
    return tb_calendar(now)


def _epoch(value):
    value = float(value or 0)
    return value / 1000 if value >= 100_000_000_000 else value


def _state(db, gid, key):
    row = db.execute('SELECT value FROM bot_state WHERE guild_id=? AND key=?', (gid, key)).fetchone()
    return json.loads(row[0]) if row else None


def event_context(gid, kind, now):
    """Guild-wide aggregates only. Current game snapshots and completed history
    remain separate: Comlink history never supplies a live event deadline."""
    result = {'unavailable': True}
    with closing(sqlite3.connect(database.DB_NAME)) as db:
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        snapshot = {}
        if 'bot_state' in tables:
            row = db.execute("SELECT value FROM bot_state WHERE guild_id=? AND key LIKE ? "
                             "ORDER BY json_extract(value,'$.at') DESC,rowid DESC LIMIT 1",
                             (gid, f'live_game:{kind}:%')).fetchone()
            snapshot = json.loads(row[0]) if row else {}
        if snapshot:
            captured = _epoch(snapshot.get('at'))
            result['captured_at'] = datetime.fromtimestamp(captured, MSK).isoformat()
            if 0 <= now.timestamp() - captured <= 900:
                data = snapshot.get('data')
                result['source'] = 'сохранённый ответ игры'
                if data is None or (kind == 'raid' and not data.get('active')):
                    result.update(unavailable=False, active=False)
                else:
                    if kind == 'raid':
                        candidates = [r for r in data.get('active', []) if _epoch(r.get('end')) > now.timestamp()]
                        event = candidates[0] if candidates else {}
                    else:
                        event = data
                    end = _epoch(event.get('end'))
                    if 0 < end - now.timestamp() <= 7 * 86400:
                        result.update(unavailable=False, active=True, confirmed_live=True,
                                      ends_at=datetime.fromtimestamp(end, MSK).isoformat(),
                                      remaining_seconds=int(end - now.timestamp()))
                        if kind == 'tw':
                            round_number = int(event['round'])
                            result['phase'] = 'расстановка защиты' if round_number == 0 else f'раунд {round_number}'
                            result['opponent_name'] = str((event.get('opponent_guild') or {}).get('name') or '')[:100]
                            for side in ('own', 'opponent'):
                                zones = event.get(side) or []
                                result[side] = {}
                                for metric in ('capacity', 'deployed', 'remaining', 'defeated'):
                                    values = [z.get(metric) for z in zones]
                                    if values and all(isinstance(v, int) for v in values):
                                        result[side][metric] = sum(values)
                        else:
                            result['name'] = raids.raid_name(event.get('id'))
                            result['points'] = event.get('points')
                            result['participants'] = len(event.get('players') or [])
        if kind == 'raid' and 'raid_results' in tables:
            rows = db.execute('SELECT raid_id,end_time,guild_score FROM raid_results WHERE guild_id=? ORDER BY end_time DESC LIMIT 3',
                              (gid,)).fetchall()
            result['history'] = [{'name': raids.raid_name(rid), 'ended_at': datetime.fromtimestamp(end, MSK).isoformat(),
                                  'guild_score': score, 'source': 'итоги Comlink'} for rid, end, score in rows]
        if kind == 'tw' and 'bot_state' in tables:
            result['history'] = [{k: r[k] for k in ('name', 'end', 'score', 'opponent_score') if k in r}
                                 for r in (_state(db, gid, 'tw_recent_opponents') or [])[:3]]
    return result


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
    for kind in ('tw', 'raid'):
        try:
            context[kind] = event_context(gid, kind, now)
        except (sqlite3.Error, ValueError, TypeError, KeyError, OverflowError):
            log.warning('Assistant %s context unavailable', kind, exc_info=False)
            context[kind] = {'unavailable': True}
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


async def refresh_event(owner, access, profile, question, context):
    """Explicit mode questions may read the author's existing EA session.
    No login/reconnect, account actions, or use of somebody else's credentials."""
    if not access.get('ally_code') or access.get('tier') != 'officer':
        return False
    if not re.search(r'когда|сколько|остал|очки|сч[её]т|статус|соперник|противник|расстанов|ид[её]т|законч|сейчас', question, re.I):
        return False
    modes = [kind for kind, pattern in (('tb', r'\bтб\b|\bви\b'), ('tw', r'\bвг\b'), ('raid', r'рейд'))
             if re.search(pattern, question, re.I)]
    if len(modes) != 1:
        return False
    kind = modes[0]
    saved = context.get(kind, {})
    if saved.get('captured_at'):
        age = time.time() - datetime.fromisoformat(saved['captured_at']).timestamp()
        if 0 <= age < 60:
            return False
    from services import game_account, game_account_readonly, game_event_live, tb_live
    try:
        state = await asyncio.wait_for(game_account_readonly.read(
            str(owner), access['guild_id'], profile, access['tier'], access['ally_code'], event=kind), timeout=12)
        captured = int(time.time())
        if kind == 'tb':
            await asyncio.to_thread(tb_live.save, access['guild_id'], str(owner), captured, state)
        else:
            await asyncio.to_thread(game_event_live.save, access['guild_id'], str(owner), kind, captured, state)
        return True
    except (game_account.AccountError, asyncio.TimeoutError):
        # Keep existing facts when no connected/current session is available.
        return False
    except Exception:
        log.warning('Assistant event refresh unavailable for %s', kind, exc_info=False)
        return False


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
