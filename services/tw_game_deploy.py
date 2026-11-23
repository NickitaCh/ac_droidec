"""Personal TW defense: locked event roster, durable one-shot execution, readback.

Wire schema verified against Android 0.41.2115745 ProtoMember attributes and
live defense squads: DeployUnitsRequest map=1, zone=2, event=5, squad=6,
command=7. Squad cells=1/type=4/datacron=10; Cell id=1/definition=2/index=3/
type=12. Fleet order: commander, three starters, ordered reinforcements.
"""
import base64
import asyncio
from collections import Counter
from contextlib import closing
import json
import hashlib
import secrets
import sqlite3
import time
import unicodedata

import database
from services import game_account as ga, game_account_readonly as accounts
from services import game_event_live, tw_live_image
from services.tb_live import _messages, _number, _text


def _db():
    db = sqlite3.connect(database.DB_NAME, timeout=20)
    db.row_factory = sqlite3.Row
    db.execute("""CREATE TABLE IF NOT EXISTS tw_game_deploy (
        ticket TEXT PRIMARY KEY, owner TEXT NOT NULL, guild_id INTEGER NOT NULL,
        code TEXT NOT NULL, event TEXT NOT NULL, state TEXT NOT NULL,
        data TEXT NOT NULL, updated INTEGER NOT NULL,
        UNIQUE(owner,guild_id,code,event))""")
    db.commit()
    return db


def _authorize(user, code):
    owner, guild_id = str(user['discord_id']), user['guild_id']
    ga.require_game_officer(owner, guild_id, user['bot_profile'], user['tier'])
    if accounts.own_main(user) == str(code):
        return
    roster = database.get_roster_by_player_id(guild_id)
    if (ga.select_account(owner, guild_id, ally_code=code) != str(code)
            or not any(str(registered) == str(code) for registered, _ in roster.values())
            or not accounts._connection(owner, code)):
        raise ga.AccountError('Доступна защита своего подключённого аккаунта из состава гильдии.')


def load(user, ticket):
    with closing(_db()) as db:
        row = db.execute('SELECT * FROM tw_game_deploy WHERE ticket=? AND owner=? AND guild_id=?',
                         (ticket, str(user['discord_id']), user['guild_id'])).fetchone()
    if row is None:
        raise ga.AccountError('Назначение не найдено или принадлежит другому игроку.')
    _authorize(user, row['code'])
    return {**dict(row), 'data': json.loads(row['data'])}


def create(user, code, event, assignments):
    """Explicit personal draft; never changes the guild's active plan."""
    _authorize(user, code)
    if not event or not assignments or len(assignments) > 20:
        raise ga.AccountError('Нет назначений для расстановки.')
    data = {'assignments': assignments, 'results': []}
    with closing(_db()) as db, db:
        ticket = secrets.token_urlsafe(24)
        db.execute(
            'INSERT INTO tw_game_deploy VALUES (?,?,?,?,?,?,?,?) '
            'ON CONFLICT(owner,guild_id,code,event) DO NOTHING',
            (ticket, str(user['discord_id']), user['guild_id'], code, event,
             'draft', json.dumps(data, ensure_ascii=False), int(time.time())))
        ticket = db.execute(
            'SELECT ticket FROM tw_game_deploy WHERE owner=? AND guild_id=? AND code=? AND event=?',
            (str(user['discord_id']), user['guild_id'], code, event)).fetchone()['ticket']
    return ticket


def _save(ticket, data, state, *, expected=None, expected_data=None):
    with closing(_db()) as db, db:
        sql = 'UPDATE tw_game_deploy SET data=?,state=?,updated=? WHERE ticket=?'
        args = [json.dumps(data, ensure_ascii=False), state, int(time.time()), ticket]
        if expected:
            sql += ' AND state=?'
            args.append(expected)
        if expected_data is not None:
            sql += ' AND data=?'
            args.append(expected_data)
        if db.execute(sql, args).rowcount != 1:
            raise ga.AccountError('Запуск уже выполняется или предпросмотр устарел.')


def revision(data):
    return hashlib.sha256(json.dumps(data, ensure_ascii=False).encode()).hexdigest()[:12]


def _pack_key(row):
    leader, *others = row['units']
    return json.dumps([leader, *sorted(others)])


def _past_datacrons(record):
    with closing(_db()) as db:
        past = db.execute(
            'SELECT data FROM tw_game_deploy WHERE owner=? AND guild_id=? AND code=? AND event<>? '
            'ORDER BY updated DESC, rowid DESC',
            (record['owner'], record['guild_id'], record['code'], record['event']),
        ).fetchall()
    choices = {}
    for item in past:
        data = json.loads(item['data'])
        for key, dc in data.get('observed_dc', {}).items():
            choices.setdefault(key, dc)
        for row, result in zip(data['assignments'], data.get('results', [])):
            if result.get('status') == 'Выставлен и проверен' and not row['zone'].endswith('f'):
                choices.setdefault(_pack_key(row), row.get('datacron') or '')
    return choices


def _normalize_ability(value):
    return ''.join(c for c in unicodedata.normalize('NFKC', value.casefold()) if c.isalnum())


def _recommended(row, candidates, catalog, guild_id):
    setting = row.get('datacron_requirement')
    if setting:
        from tw_def_engine import Roster, datacron_options, pick_datacron
        requirements = {('base', r[0]): r for r in database.get_all_datacron_requirements(guild_id)}
        requirements.update({('focused', r[0]): r for r in database.get_all_datacron_focused_requirements(guild_id)})
        # One TW squad uses one datacron, regardless of the guild target quantity.
        requirements = {key: tuple(r[:11]) + (1,) + tuple(r[12:]) if key[0] == 'base' and len(r) > 11 else r
                        for key, r in requirements.items()}
        roster = Roster([], {}, {}, {}, {}, {}, dc_reqs=requirements)
        matches = []
        for dc in candidates:
            roster.datacrons['player'] = [dc]
            if any(pick_datacron(roster, 'player', {**opt, 'required': True})[0] == 'ok'
                   for opt in datacron_options(setting)):
                matches.append(str(dc['id']))
        return matches
    wanted = _normalize_ability(row.get('recommendation') or '')
    if not wanted:
        return []
    # The one-off assignment used a paraphrase of this catalog label.
    aliases = {
        _normalize_ability('Ход врага → бонусный ход Джокасты Ну, +100% урона'):
        _normalize_ability('Джокаста Ню: ход врага → доп. ход с +100% урона (не срабатывает на врагах).'),
    }
    wanted = aliases.get(wanted, wanted)
    matches = []
    for dc in candidates:
        season = (catalog or {}).get('seasons', {}).get(dc.get('setId'), {})
        labels = {key: label for level in (3, 6, 9) for key, label in season.get(f'level{level}', [])}
        if any(_normalize_ability(labels.get(a.get('abilityId')) or '') == wanted for a in dc.get('affix', [])):
            matches.append(str(dc['id']))
    return matches


def suggested_assignments(record, live, datacrons, catalog):
    rows = [dict(row) for row in record['data']['assignments']]
    if not live or record['state'] not in ('draft', 'stopped', 'partial'):
        return rows
    validate(live, record['event'], [])
    free = set(live['datacrons']) - live['used_dc']
    candidates = [dc for dc in datacrons if str(dc.get('id')) in free]
    placed = {frozenset(team['units']) for team in live['teams']}
    pending = [row for row in rows if not row['zone'].endswith('f') and frozenset(row['units']) not in placed]
    reserved = {row.get('datacron') for row in pending}
    pending = [row for row in pending if not row.get('datacron') and not row.get('datacron_chosen')]
    if not pending:
        return rows
    past = _past_datacrons(record)
    fallbacks = []
    for row in pending:
        previous = past.get(_pack_key(row))
        matches = [dc for dc in _recommended(row, candidates, catalog, record['guild_id']) if dc not in reserved]
        if matches:
            row['datacron'] = previous if previous in matches else matches[0]
            row['datacron_source'] = 'recommendation'
            reserved.add(row['datacron'])
        elif previous in free:
            fallbacks.append((row, previous))
    for row, previous in fallbacks:
        if previous not in reserved:
            row.update(datacron=previous, datacron_source='history')
            reserved.add(previous)
    return rows


async def edit_datacron(user, ticket, index, identifier, expected_revision):
    record = load(user, ticket)
    data = record['data']
    if record['state'] in ('applying', 'unknown', 'complete') or revision(data) != expected_revision:
        raise ga.AccountError('Панель устарела или расстановка уже запущена. Обновите её.')
    if not 0 <= index < len(data['assignments']):
        raise ga.AccountError('Назначение не найдено.')
    before = json.dumps(data, ensure_ascii=False)
    live = await snapshot(user, record['code'])
    rows = inspect_assignments(live, record['event'], data['assignments'])
    if rows[index]['done'] or data['assignments'][index]['zone'].endswith('f'):
        raise ga.AccountError('Датакрон этого назначения нельзя изменить: пак уже выставлен или это флот.')
    if identifier and (identifier not in live['datacrons'] or identifier in live['used_dc']):
        raise ga.AccountError('Датакрон уже выставлен или недоступен в этой ВГ.')
    if identifier and any(i != index and not row['done'] and row.get('datacron') == identifier
                          for i, row in enumerate(rows)):
        raise ga.AccountError('Этот датакрон уже выбран для другого пака.')
    data['assignments'][index]['datacron'] = identifier
    data['assignments'][index]['datacron_chosen'] = True
    data.pop('nonce', None)
    data.pop('preview', None)
    _save(ticket, data, 'draft', expected=record['state'], expected_data=before)
    return data


def rpc(raw, player_id, method, args):
    rows = ga.fields(ga.read_request(raw, player_id))
    return ga.pack([r for r in rows if r[0] not in (4, 5, 6)] + [
        (4, 2, b'TerritoryRpc'), (5, 2, method.encode()), (6, 2, ga.pack(args))])


def parse(payload, player_id):
    state = game_event_live.parse_tw(payload)
    if not state:
        raise ga.AccountError('Игра не вернула активную ВГ.')
    wars = list(_messages(ga.fields(payload), 3))
    players = list(_messages(wars[0], 8))
    if len(players) != 1 or _number(wars[0], 12):
        raise ga.AccountError('Аккаунт не участвует в этой ВГ.')
    player = players[0]
    units, datacrons, used, used_dc, teams, commands = {}, {}, set(), set(), [], {}
    for unit in _messages(player, 3):
        definition, identifier = _text(unit, 13), _text(unit, 14)
        base = definition.partition(':')[0]
        if not base or not identifier or base in units:
            raise ga.AccountError('Не удалось однозначно прочитать зафиксированный ростер ВГ.')
        units[base] = {'id': identifier, 'definition': definition}
    for dc in _messages(player, 4):
        identifier = _text(dc, 1)
        if not identifier or identifier in datacrons:
            raise ga.AccountError('Не удалось однозначно прочитать датакроны ВГ.')
        datacrons[identifier] = ga.pack(dc)
    used.update(_text(u, 1) for u in _messages(player, 2) if _text(u, 2))
    used_dc.update(_text(dc, 1) for dc in _messages(player, 5) if _text(dc, 2))
    for guild in _messages(wars[0], 6):
        for conflict in _messages(guild, 2):
            zone = next(_messages(conflict, 1), None)
            if not zone:
                continue
            zid = _text(zone, 1)
            commands[zid] = _number(zone, 6)
            for team in _messages(conflict, 3):
                if _text(team, 1) != player_id:
                    continue
                squad = next(_messages(team, 4), None)
                if not squad:
                    raise ga.AccountError('Не удалось прочитать уже выставленный отряд.')
                cells = sorted(_messages(squad, 1), key=lambda c: _number(c, 3) or 0)
                dc = next(_messages(squad, 10), [])
                ids = [_text(c, 1) for c in cells]
                used.update(ids)
                if _text(dc, 1):
                    used_dc.add(_text(dc, 1))
                teams.append({'zone': zid, 'units': [_text(c, 2).partition(':')[0] for c in cells],
                              'datacron': _text(dc, 1),
                              'types': [_number(c, 12) for c in cells]})
    return {**state, 'units': units, 'datacrons': datacrons, 'used': used,
            'used_dc': used_dc, 'teams': teams, 'commands': commands}


async def snapshot(user, code):
    _authorize(user, code)
    connection, credentials, current = accounts._load(
        str(user['discord_id']), user['guild_id'], user['bot_profile'], user['tier'], code)
    raw = base64.b64decode(credentials['read_request'], validate=True)
    if not current():
        raise ga.AccountError('Подключение изменилось.')
    payload = await ga._read_payload(rpc(raw, connection[1], 'GetTerritoryMapStatuses', [(1, 0, 8)]),
                                    'TerritoryRpc', 'GetTerritoryMapStatuses')
    if not current():
        raise ga.AccountError('Подключение изменилось.')
    state = parse(payload, connection[1])
    guild = database.get_guild_config(user['guild_id']) or {}
    if not guild.get('swgoh_guild_id') or state['home_guild'].get('id') != guild['swgoh_guild_id']:
        raise ga.AccountError('Игровая гильдия не совпала с гильдией назначения.')
    state.update(_raw=raw, _player=connection[1], _current=current)
    return state


def cell_types(fleet, count):
    if fleet:
        if not 4 <= count <= 8:
            raise ga.AccountError('Флот: флагман, три корабля основы, затем до четырёх подкреплений.')
        return [3, 1, 1, 1] + [5] * (count - 4)
    if not 1 <= count <= 5:
        raise ga.AccountError('В отряде должно быть от одного до пяти персонажей.')
    return [2] + [1] * (count - 1)


def validate(state, event, assignments):
    if state['event_id'] != event or state['round'] != 0 or game_event_live.epoch(state['end']) <= time.time():
        raise ga.AccountError('Этап расстановки этой ВГ завершён или событие изменилось.')
    zones = {}
    for zone in state['own']:
        key = tw_live_image.zone_key(zone['id'])
        if key:
            if key in zones:
                raise ga.AccountError('Неоднозначная карта ВГ.')
            zones[key] = zone
    seen, dc_seen, counts, result = set(), set(), {}, []
    for row in assignments:
        bases = row['units']
        dc = row.get('datacron', '')
        zone = zones.get(row['zone'])
        types = cell_types(row['zone'].endswith('f'), len(bases))
        if not zone or len(set(bases)) != len(bases) or seen.intersection(bases):
            raise ga.AccountError('Зона не найдена или юниты повторяются в назначениях.')
        seen.update(bases)
        if dc and (dc in dc_seen or dc not in state['datacrons'] or row['zone'].endswith('f')):
            raise ga.AccountError('Датакрон повторяется, недоступен в ВГ или выбран для флота.')
        if dc:
            dc_seen.add(dc)
        existing = [t for t in state['teams'] if t['zone'] == zone['id']
                    and t['units'] == bases and t['datacron'] == dc and t['types'] == types]
        if existing:
            result.append({**row, 'zone_id': zone['id'], 'done': True, 'types': types})
            continue
        if any(b not in state['units'] or state['units'][b]['id'] in state['used'] for b in bases):
            raise ga.AccountError('В отряде «' + row['name'] + '» есть недоступные или уже выставленные юниты.')
        if dc in state['used_dc']:
            raise ga.AccountError('Выбранный датакрон уже выставлен.')
        if zone['status'] != 1 or zone['deployed'] is None or not zone['capacity']:
            raise ga.AccountError('Зона недоступна для расстановки.')
        counts[zone['id']] = counts.get(zone['id'], 0) + 1
        if zone['deployed'] + counts[zone['id']] > zone['capacity']:
            raise ga.AccountError('В зоне недостаточно свободных мест.')
        if state['commands'].get(zone['id']) not in (0, 1, 2):
            raise ga.AccountError('Не удалось прочитать приказ зоны.')
        result.append({**row, 'zone_id': zone['id'], 'done': False, 'types': types})
    return result


def deployment_request(state, row):
    cells = []
    for index, (base, kind) in enumerate(zip(row['units'], row['types'])):
        unit = state['units'][base]
        cells.append((1, 2, ga.pack([(1, 2, unit['id'].encode()),
                                    (2, 2, unit['definition'].encode()),
                                    (3, 0, index), (12, 0, kind)])))
    squad = cells + [(4, 0, 54)]
    if row.get('datacron'):
        squad.append((10, 2, state['datacrons'][row['datacron']]))
    args = [(1, 2, state['event_id'].encode()), (2, 2, row['zone_id'].encode()),
            (5, 0, 8), (6, 2, ga.pack(squad)), (7, 0, state['commands'][row['zone_id']])]
    if not state['_current']():
        raise ga.AccountError('Подключение изменилось.')
    return rpc(state['_raw'], state['_player'], 'DeployUnits', args)


def inspect_assignments(state, event, assignments):
    """An unavailable pack must not block independent assignments.

    A manually placed full pack is complete regardless of its zone/DC/order.
    Partial overlap is an error for this pack only, never a second deployment.
    """
    validate(state, event, [])
    unit_uses = Counter(b for row in assignments for b in row['units'])
    dc_uses = Counter(row.get('datacron') for row in assignments if row.get('datacron'))
    reserved, result = Counter(), []
    zones = {z['id']: z for z in state['own']}
    for row in assignments:
        already = next((t for t in state['teams'] if set(t['units']) == set(row['units'])), None)
        if already:
            result.append({**row, 'done': True, 'zone_id': already['zone'],
                           'actual_datacron': already['datacron'], 'error': ''})
            continue
        try:
            if any(unit_uses[b] > 1 for b in row['units']):
                raise ga.AccountError('Юнит повторяется в других назначениях.')
            if row.get('datacron') and dc_uses[row['datacron']] > 1:
                raise ga.AccountError('Датакрон выбран для нескольких отрядов.')
            checked = validate(state, event, [row])[0]
            zone = zones[checked['zone_id']]
            if zone['deployed'] + reserved[zone['id']] >= zone['capacity']:
                raise ga.AccountError('В зоне нет свободного места.')
            reserved[zone['id']] += 1
            result.append({**checked, 'error': ''})
        except ga.AccountError as exc:
            result.append({**row, 'done': False, 'error': str(exc)})
    return result


async def prepare(user, ticket, selections, catalog=None):
    record = load(user, ticket)
    if record['state'] in ('applying', 'unknown'):
        raise ga.AccountError('Предыдущая операция требует проверки результата. Повторный запуск заблокирован.')
    data = record['data']
    before = json.dumps(data, ensure_ascii=False)
    state = await snapshot(user, record['code'])
    rows = data['assignments']
    if catalog is not None:
        def suggestions():
            datacrons = database.get_player_datacrons_bulk([record['code']]).get(record['code'], [])
            return suggested_assignments(record, state, datacrons, catalog)
        rows = await asyncio.to_thread(suggestions)
    rows = [{**row, 'datacron': selections[str(i)], 'datacron_chosen': True}
            if str(i) in selections else row for i, row in enumerate(rows)]
    for row in rows:
        row.pop('datacron_source', None)
    checked = inspect_assignments(state, record['event'], rows)
    observed = data.setdefault('observed_dc', {})
    for row in checked:
        if row['done'] and not row['zone'].endswith('f'):
            observed[_pack_key(row)] = row['actual_datacron']
    data.update(assignments=rows, prepared=int(time.time()), nonce=secrets.token_urlsafe(24),
                preview=[{'done': r['done'], 'error': r.get('error', ''),
                          'actual_datacron': r.get('actual_datacron', '')} for r in checked], results=[])
    _save(ticket, data, 'ready', expected=record['state'], expected_data=before)
    return data


async def apply(user, ticket, nonce):
    record = load(user, ticket)
    data = record['data']
    if (record['state'] != 'ready' or not secrets.compare_digest(data.get('nonce', ''), nonce)
            or time.time() - data.get('prepared', 0) > 300):
        raise ga.AccountError('Предпросмотр устарел или запуск уже выполнен. Проверьте назначения заново.')
    _save(ticket, data, 'applying', expected='ready')
    sent = False
    try:
        for index in range(len(data['assignments'])):
            state = await snapshot(user, record['code'])
            rows = inspect_assignments(state, record['event'], data['assignments'])
            row = rows[index]
            if row['done']:
                if not row['zone'].endswith('f'):
                    data.setdefault('observed_dc', {})[_pack_key(row)] = row['actual_datacron']
                data['results'].append({'name': row['name'], 'status': 'Уже выставлен'})
                _save(ticket, data, 'applying')
                continue
            if row.get('error'):
                data['results'].append({'name': row['name'], 'status': 'Пропущен: ' + row['error']})
                _save(ticket, data, 'applying')
                continue
            data['in_flight'] = row['name']
            _save(ticket, data, 'applying')
            sent = True
            try:
                await ga._read_payload(deployment_request(state, row), 'TerritoryRpc', 'DeployUnits', allow_empty=True)
            except ga.AccountError as exc:
                # Explicit INVALIDREQUEST/INVALIDDATA means this write was rejected;
                # a timeout/auth failure has an uncertain outcome and stops the run.
                if str(exc) not in ('Игра отклонила чтение (код 8).', 'Игра отклонила чтение (код 9).'):
                    raise
                data['results'].append({'name': row['name'], 'status': 'Пропущен: игра отклонила выставление.'})
                data.pop('in_flight', None)
                _save(ticket, data, 'applying')
                sent = False
                continue
            after = await snapshot(user, record['code'])
            # Readback is exact for our write, even though manually placed packs
            # are accepted with their actual zone and DC in the preview.
            exact = any(t['zone'] == row['zone_id'] and t['units'] == row['units']
                        and t['datacron'] == row.get('datacron', '') and t['types'] == row['types']
                        for t in after['teams'])
            if after['event_id'] != record['event'] or not exact:
                raise ga.AccountError('Игра не подтвердила состав, зону и датакрон после выставления.')
            data['results'].append({'name': row['name'], 'status': 'Выставлен и проверен'})
            if not row['zone'].endswith('f'):
                data.setdefault('observed_dc', {})[_pack_key(row)] = row.get('datacron', '')
            data.pop('in_flight', None)
            _save(ticket, data, 'applying')
            sent = False
        _save(ticket, data, 'partial' if any(r['status'].startswith('Пропущен:') for r in data['results']) else 'complete')
    except Exception:
        _save(ticket, data, 'unknown' if sent else 'stopped')
        raise
    return data
