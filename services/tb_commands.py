"""Officer TB orders and deterministic, event-scoped automation.

Android ProtoOutput (Unity 6000.3.27f2) CommandTerritoryRequest:
map=1, zone=2, message=3, state=4, instance=5 (BATTLE=1), notify=6.
TerritoryZoneCommandState: NOCOMMAND=1, FOCUSED=2, IGNORED=3.
No login or retry on writes. Always read the game before and after a change.
"""
import asyncio
import base64
from contextlib import closing, asynccontextmanager
import json
import secrets
import sqlite3
import time
from pathlib import Path

import database
from services import game_account as ga, game_account_readonly as accounts, tb_live, tb_forecast
from services.sync_queue import shared_file_lock
import tb_plan_reader

COMMAND_TIMEOUT_SECONDS = 30

STATES = {1: "Без приказа", 2: "Концентрация", 3: "Запрет"}


class Busy(ga.AccountError):
    pass


class AutomationPaused(Exception):
    pass


def _db():
    db = sqlite3.connect(database.DB_NAME, timeout=15)
    db.row_factory = sqlite3.Row
    db.executescript("""
        CREATE TABLE IF NOT EXISTS tb_command_config (
            guild_id INTEGER PRIMARY KEY, revision INTEGER NOT NULL DEFAULT 0,
            owner TEXT NOT NULL, code TEXT NOT NULL, profile TEXT NOT NULL,
            event TEXT NOT NULL, phase INTEGER NOT NULL, enabled INTEGER NOT NULL,
            rules TEXT NOT NULL, status TEXT NOT NULL DEFAULT '');
        CREATE TABLE IF NOT EXISTS tb_command_locks (
            guild_id INTEGER PRIMARY KEY, token TEXT NOT NULL, expires REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS tb_command_log (
            id INTEGER PRIMARY KEY, guild_id INTEGER NOT NULL, at INTEGER NOT NULL,
            owner TEXT NOT NULL, event TEXT NOT NULL, phase INTEGER NOT NULL,
            target TEXT NOT NULL, command INTEGER, message TEXT, reason TEXT NOT NULL,
            result TEXT NOT NULL);
    """)
    return db


def config(guild_id):
    with closing(_db()) as db:
        row = db.execute('SELECT * FROM tb_command_config WHERE guild_id=?', (guild_id,)).fetchone()
    return {**dict(row), 'rules': json.loads(row['rules'])} if row else None


def history(guild_id):
    with closing(_db()) as db:
        return [dict(r) for r in db.execute(
            'SELECT * FROM tb_command_log WHERE guild_id=? ORDER BY id DESC LIMIT 30', (guild_id,))]


def _log(guild_id, owner, state, target, command, message, reason, result):
    with closing(_db()) as db, db:
        db.execute('INSERT INTO tb_command_log VALUES(NULL,?,?,?,?,?,?,?,?,?,?)',
                   (guild_id, int(time.time()), str(owner), state['event'], state['phase'],
                    target, command, message, reason, result))
        db.execute('DELETE FROM tb_command_log WHERE guild_id=? AND id NOT IN '
                   '(SELECT id FROM tb_command_log WHERE guild_id=? ORDER BY id DESC LIMIT 300)',
                   (guild_id, guild_id))


def targets(state):
    """Use actual child zone IDs; each individual CM can be focused separately."""
    out = {}
    for key, zone in (state or {}).get('zones', {}).items():
        if zone['status'] != tb_live.OPEN_ZONE:
            continue
        where = tb_forecast.planet_of(key) if key.startswith('tb3_mixed_') else None
        planet = tb_plan_reader.PLANET_NAME_TO_RU.get(where[1], where[1]).capitalize() if where else key
        out[zone['id']] = {**zone, 'planet': key, 'label': planet + ' · Заливка', 'kind': 'deployment'}
        if recon := zone.get('recon_command'):
            out[recon['id']] = {**recon, 'planet': key, 'label': planet + ' · Взводы', 'kind': 'platoons'}
        for group, prefix in [('missions', 'Бой'), ('special', 'ОЗ')]:
            for index, row in enumerate(zone.get(group, []), 1):
                if row.get('status') not in (None, tb_live.OPEN_ZONE):
                    continue
                out[row['id']] = {**row, 'planet': key, 'label': f'{planet} · {prefix} {index}', 'kind': group}
    return out


def active(state, event, phase):
    if (not state or state['event'] != event or state['phase'] != int(phase)
            or state['end_ms'] <= time.time() * 1000):
        raise ga.AccountError('Этап изменился или закончился. Обновите приказы.')


def _authorize(user, code):
    owner, gid = str(user['discord_id']), user['guild_id']
    ga.require_game_officer(owner, gid, user['bot_profile'], user['tier'])
    if ga.select_account(owner, gid, ally_code=str(code)) != str(code):
        raise ga.AccountError('Выберите свой подключённый аккаунт.')
    roster = database.get_roster_by_player_id(gid)
    if not any(str(ally) == str(code) for ally, _ in roster.values()):
        raise ga.AccountError('Аккаунт должен состоять в вашей игровой гильдии.')


def _lease(gid, token, *, acquire=False, release=False, revision=None):
    now = time.time()
    with closing(_db()) as db, db:
        db.execute('BEGIN IMMEDIATE')
        if release:
            db.execute('DELETE FROM tb_command_locks WHERE guild_id=? AND token=?', (gid, token))
            return
        if acquire:
            db.execute('DELETE FROM tb_command_locks WHERE guild_id=? AND expires<?', (gid, now))
            db.execute('INSERT OR IGNORE INTO tb_command_locks VALUES(?,?,?)', (gid, token, now + 180))
        row = db.execute('SELECT token FROM tb_command_locks WHERE guild_id=?', (gid,)).fetchone()
        if not row or row['token'] != token:
            raise Busy('Приказы уже обновляются. Повторите после завершения.')
        if revision is not None and not db.execute(
            'SELECT 1 FROM tb_command_config WHERE guild_id=? AND enabled=1 AND revision=?', (gid, revision)
        ).fetchone():
            raise AutomationPaused
        db.execute('UPDATE tb_command_locks SET expires=? WHERE guild_id=? AND token=?', (now + 180, gid, token))


@asynccontextmanager
async def locked(gid):
    token = secrets.token_hex(16)
    await asyncio.to_thread(_lease, gid, token, acquire=True)
    try:
        yield token
    finally:
        await asyncio.to_thread(_lease, gid, token, release=True)


async def read(user, code):
    await asyncio.to_thread(_authorize, user, code)
    state = await accounts.read(str(user['discord_id']), user['guild_id'], user['bot_profile'],
                                user['tier'], str(code), event='tb')
    if not state:
        raise ga.AccountError('В игре нет активной ТБ.')
    await asyncio.to_thread(tb_live.save, user['guild_id'], str(user['discord_id']), int(time.time()), state)
    return state


def request(raw, player, event, target, command, message):
    if command not in STATES or len(message) > 140:
        raise ga.AccountError('Выберите приказ; подпись — до 140 символов.')
    rows = ga.fields(ga.read_request(raw, player))
    args = [(1, 2, event.encode()), (2, 2, target.encode()), (3, 2, message.encode()),
            (4, 0, command), (5, 0, 1), (6, 0, 0)]
    return ga.pack([r for r in rows if r[0] not in (4, 5, 6)] + [
        (4, 2, b'TerritoryRpc'), (5, 2, b'CommandTerritory'), (6, 2, ga.pack(args))])


def _send_lock(gid):
    db = Path(database.DB_NAME).resolve()
    return shared_file_lock(db.parent / "cache" / f"{db.name}.tb-command-{gid}.lock")


async def _apply(user, code, state, target, command, message, reason, token, *, revision=None):
    gid, owner = user['guild_id'], str(user['discord_id'])
    await asyncio.to_thread(_authorize, user, code)
    active(state, state['event'], state['phase'])
    row = targets(state).get(target)
    if not row:
        raise ga.AccountError('Цель недоступна в текущем этапе.')
    if row.get('command') == command and row.get('message', '') == message:
        return state
    connection, credentials, is_current = await asyncio.to_thread(
        accounts._load, owner, gid, user['bot_profile'], user['tier'], str(code))
    if not is_current():
        raise ga.AccountError('Подключение изменилось.')
    body = request(base64.b64decode(credentials['read_request'], validate=True), connection[1],
                   state['event'], target, command, message)
    try:
        async with _send_lock(gid):
            await asyncio.to_thread(_lease, gid, token, revision=revision)
            try:
                async with asyncio.timeout(COMMAND_TIMEOUT_SECONDS):
                    await ga._read_payload(body, 'TerritoryRpc', 'CommandTerritory', allow_empty=True)
            except TimeoutError:
                raise ga.AccountError('Игра не подтвердила приказ вовремя. Обновите состояние.') from None
        if not is_current():
            raise ga.AccountError('Подключение изменилось во время изменения приказа.')
        fresh = await read(user, code)
        active(fresh, state['event'], state['phase'])
        actual = targets(fresh).get(target, {})
        if actual.get('command') != command or actual.get('message', '') != message:
            raise ga.AccountError('Игра не подтвердила приказ. Обновите состояние перед повтором.')
    except (AutomationPaused, Busy):
        raise
    except BaseException:
        await asyncio.to_thread(_pause, gid, 'Изменение не подтверждено. Проверьте приказы и включите правила заново.',
                                revision=revision)
        await asyncio.to_thread(_log, gid, owner, state, target, command, message, reason, 'Не подтверждён')
        raise
    await asyncio.to_thread(_log, gid, owner, fresh, target, command, message, reason, 'Подтверждён')
    return fresh


async def change(user, code, event, phase, target, command, message, expected):
    """A manual override disables automation for this target until re-enabled."""
    async with locked(user['guild_id']) as token:
        state = await read(user, code)
        active(state, event, phase)
        row = targets(state).get(target)
        if not row or [row.get('command'), row.get('message', '')] != expected:
            raise ga.AccountError('Приказ уже изменился. Обновите и проверьте его перед сохранением.')
        # Validate before disabling rules, including no-op edits.
        if command not in STATES or len(message) > 140:
            raise ga.AccountError('Выберите приказ; подпись — до 140 символов.')
        await asyncio.to_thread(_disable_target, user['guild_id'], target)
        state = await _apply(user, code, state, target, command, message, 'Вручную', token)
    return state


def _pause(gid, status, *, revision=None):
    with closing(_db()) as db, db:
        db.execute('UPDATE tb_command_config SET enabled=0,status=?,revision=revision+1 '
                   'WHERE guild_id=? AND (? IS NULL OR revision=?)', (status, gid, revision, revision))


def pause(gid):
    with closing(_db()) as db, db:
        db.execute('UPDATE tb_command_config SET enabled=0,status=?,revision=revision+1 '
                   'WHERE guild_id=? AND enabled=1', ('Пауза по команде офицера.', gid))


async def pause_and_wait(gid):
    await asyncio.to_thread(pause, gid)
    # Confirm only after any RPC that passed the old revision has left the send lock.
    async with _send_lock(gid):
        pass


def pause_owner(gid, owner, status):
    with closing(_db()) as db, db:
        db.execute('UPDATE tb_command_config SET enabled=0,status=?,revision=revision+1 '
                   'WHERE guild_id=? AND owner=? AND enabled=1', (status, gid, str(owner)))


async def restore():
    """Resume enabled watches after a web restart using only saved sessions."""
    from services import game_watch
    with closing(_db()) as db:
        configs = [dict(r) for r in db.execute('SELECT * FROM tb_command_config WHERE enabled=1')]
    for cfg in configs:
        user = {'discord_id': cfg['owner'], 'guild_id': cfg['guild_id'],
                'bot_profile': cfg['profile'], 'tier': 'officer'}
        try:
            await asyncio.to_thread(_authorize, user, cfg['code'])
            await game_watch.start(cfg['owner'], cfg['guild_id'], cfg['profile'], 'officer',
                                   cfg['code'], 'tb', 60, saved_session=True)
        except ga.AccountError as error:
            await asyncio.to_thread(pause_owner, cfg['guild_id'], cfg['owner'], str(error))


def _disable_target(gid, target):
    cfg = config(gid)
    if not cfg:
        return
    for rule in cfg['rules']:
        if rule['target'] == target:
            rule['enabled'] = False
    with closing(_db()) as db, db:
        db.execute('UPDATE tb_command_config SET rules=?,revision=revision+1 WHERE guild_id=?',
                   (json.dumps(cfg['rules'], ensure_ascii=False), gid))


def validate_rules(rules, state):
    available = targets(state)
    if len(rules) > 100:
        raise ga.AccountError('Не более 100 правил на этап.')
    for rule in rules:
        if (rule['target'] not in available or rule['command'] not in STATES or
                not 1 <= rule['priority'] <= 999 or not rule['name'].strip() or
                len(rule['name']) > 100 or len(rule.get('message', '')) > 140):
            raise ga.AccountError('Проверьте цель, название, приоритет и приказ правила.')
        for cond in rule['conditions']:
            kind = cond['kind']
            if kind == 'points':
                if cond['zone'] not in state['zones'] or not 0 <= cond['value'] <= 10**12:
                    raise ga.AccountError('Проверьте порог очков планеты.')
            elif kind == 'remaining':
                if not 0 <= cond['value'] <= 48 * 3600:
                    raise ga.AccountError('Проверьте время до конца этапа.')
            elif kind in ('below_max', 'platoons'):
                if cond['zone'] not in state['zones']:
                    raise ga.AccountError('Планета недоступна.')
            else:
                raise ga.AccountError('Неизвестное условие правила.')


def save_rules(user, code, state, rules, enabled, revision):
    _authorize(user, code)
    active(state, state['event'], state['phase'])
    validate_rules(rules, state)
    with closing(_db()) as db, db:
        db.execute('BEGIN IMMEDIATE')
        previous = db.execute('SELECT revision FROM tb_command_config WHERE guild_id=?', (user['guild_id'],)).fetchone()
        if (previous['revision'] if previous else 0) != revision:
            raise ga.AccountError('Правила уже изменились. Обновите страницу.')
        db.execute('INSERT OR REPLACE INTO tb_command_config VALUES(?,?,?,?,?,?,?,?,?,?)',
                   (user['guild_id'], revision + 1, str(user['discord_id']), str(code), user['bot_profile'],
                    state['event'], state['phase'], int(enabled), json.dumps(rules, ensure_ascii=False), ''))


def matches(rule, state, now=None):
    now = time.time() if now is None else now
    defs = None
    for cond in rule['conditions']:
        kind = cond['kind']
        if kind == 'remaining':
            if not 0 < state['end_ms'] / 1000 - now <= cond['value']:
                return False
            continue
        zone = state['zones'].get(cond['zone'])
        if not zone:
            return False
        if kind == 'points' and (zone.get('points') is None or zone['points'] < cond['value']):
            return False
        if kind == 'platoons':
            ops = zone.get('operations', [])
            if not ops or any(not op['slots'] or any(not s['donor'] for s in op['slots']) for op in ops):
                return False
        if kind == 'below_max':
            defs = defs if defs is not None else tb_forecast.zones().get('planets', {})
            where = tb_forecast.planet_of(cond['zone'])
            stars = defs.get(where[1], {}).get('stars', []) if where else []
            if not stars or zone.get('points') is None or zone['points'] >= stars[-1]:
                return False
    return True


def winners(rules, state, now=None):
    selected = {}
    available = targets(state)
    for rule in sorted(rules, key=lambda r: (r['priority'], r['id'])):
        if rule['enabled'] and rule['target'] in available and rule['target'] not in selected and matches(rule, state, now):
            selected[rule['target']] = rule
    return selected


def recipe(state, thresholds, buffer_points=5_000_000, final_minutes=60):
    """Five priorities per closing planet, mirroring the discussed HotUtils setup."""
    available, rules = targets(state), []
    defs = tb_forecast.zones().get('planets', {})
    group = [{'kind': 'points', 'zone': zone, 'value': points} for zone, points in thresholds.items()]
    for zone, threshold in thresholds.items():
        where = tb_forecast.planet_of(zone)
        stars = defs.get(where[1], {}).get('stars', []) if where else []
        target = state['zones'][zone]['id']
        if not stars or target not in available or not 0 <= threshold < stars[-1] - buffer_points:
            raise ga.AccountError('Порог должен быть ниже конечного буфера; нужны игровые пороги звёзд.')
        for priority, name, command, conditions in [
            (1, 'Финальная заливка', 2, [{'kind': 'remaining', 'value': final_minutes * 60}, {'kind': 'below_max', 'zone': zone}]),
            (2, 'Стоп: конечный буфер', 3, [{'kind': 'points', 'zone': zone, 'value': stars[-1] - buffer_points}]),
            (3, 'Равномерная заливка', 2, group),
            (4, 'Стоп: порог очков', 3, [{'kind': 'points', 'zone': zone, 'value': threshold}]),
            (5, 'Начальный приказ', 2, []),
        ]:
            rules.append({'id': secrets.token_hex(6), 'name': name, 'priority': priority, 'target': target,
                          'command': command, 'message': available[target].get('message', ''),
                          'enabled': True, 'conditions': conditions})
    return rules


async def process(gid, owner, state):
    """Called only by explicit TB observation; one persisted controlling officer."""
    cfg = await asyncio.to_thread(config, gid)
    if not cfg or not cfg['enabled'] or cfg['owner'] != str(owner):
        return state
    fresh = state
    try:
        active(state, cfg['event'], cfg['phase'])
        async with locked(gid) as token:
            # Reread config under the same lease as manual edits.
            cfg = await asyncio.to_thread(config, gid)
            if not cfg or not cfg['enabled'] or cfg['owner'] != str(owner):
                return state
            user = {'discord_id': owner, 'guild_id': gid, 'bot_profile': cfg['profile'], 'tier': 'officer'}
            fresh = await read(user, cfg['code'])
            active(fresh, cfg['event'], cfg['phase'])
            # Reevaluate after every write: points, platoons or the phase may change.
            for target in list(targets(fresh)):
                active(fresh, cfg['event'], cfg['phase'])
                selected = winners(cfg['rules'], fresh)
                if rule := selected.get(target):
                    fresh = await _apply(user, cfg['code'], fresh, target, rule['command'],
                                         rule.get('message', ''), rule['name'], token, revision=cfg['revision'])
            return fresh
    except Busy:
        return state
    except AutomationPaused:
        return fresh
    except ga.AccountError as error:
        await asyncio.to_thread(_pause, gid, str(error), revision=cfg['revision'])
        return fresh
    except Exception:
        await asyncio.to_thread(_pause, gid, 'Ошибка выполнения. Проверьте состояние и включите правила заново.',
                                revision=cfg['revision'])
        return fresh
