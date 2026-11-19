"""Owner-scoped personal requirements, with explicit guild sharing.

Personal rows use the standard requirement shape, so the existing evaluator and
optimizer retain the same semantics. No personal row enters guild plate tables.
"""
from contextlib import contextmanager
from contextvars import ContextVar
import functools
import json
import secrets
import sqlite3
import re

import database

PREFIX = 'Личный · '
_actor = ContextVar('personal_plate_actor', default=None)


@contextmanager
def scope(user):
    token = _actor.set((str(user['discord_id']), user['guild_id']))
    try:
        yield
    finally:
        _actor.reset(token)


def scoped_job(func):
    @functools.wraps(func)
    async def wrapped(token, user, *args, **kwargs):
        with scope(user):
            return await func(token, user, *args, **kwargs)
    return wrapped


@contextmanager
def _db():
    db = sqlite3.connect(database.DB_NAME, timeout=20)
    db.row_factory = sqlite3.Row
    try:
        db.execute('''CREATE TABLE IF NOT EXISTS personal_stat_plates (
            id TEXT PRIMARY KEY, guild_id INTEGER NOT NULL, owner TEXT NOT NULL,
            name TEXT NOT NULL, shared INTEGER NOT NULL DEFAULT 0)''')
        db.execute('''CREATE TABLE IF NOT EXISTS personal_stat_rows (
            id INTEGER PRIMARY KEY AUTOINCREMENT, plate_id TEXT NOT NULL,
            data TEXT NOT NULL)''')
        db.commit()
        with db:
            yield db
    finally:
        db.close()


def reference(plate):
    return f"{PREFIX}{plate['name']} [{plate['id']}]"


def display_reference(value):
    return re.sub(r'(Личный · [^\[\]\r\n]+) \[[0-9a-f]{16}\]', r'\1', str(value))


def list_plates(user):
    with _db() as db:
        rows = db.execute('SELECT * FROM personal_stat_plates WHERE guild_id=? AND (owner=? OR shared=1) ORDER BY name,id',
                          (user['guild_id'], str(user['discord_id']))).fetchall()
    return [dict(row) for row in rows]


def get(plate_id, user, *, edit=False):
    with _db() as db:
        row = db.execute('SELECT * FROM personal_stat_plates WHERE id=? AND guild_id=?',
                         (plate_id, user['guild_id'])).fetchone()
    if row is None or (row['owner'] != str(user['discord_id']) and (edit or not row['shared'])):
        raise ValueError('Плейт недоступен.')
    return dict(row)


def resolve(ref, guild_id):
    actor = _actor.get()
    if not actor or actor[1] != guild_id:
        raise ValueError('Личный плейт недоступен.')
    short = re.fullmatch(re.escape(PREFIX) + r'#([0-9a-f]{16})', ref)
    plate_id = short.group(1) if short else ref.rsplit('[', 1)[-1].removesuffix(']')
    plate = get(plate_id, {'discord_id': actor[0], 'guild_id': guild_id})
    if not short and reference(plate) != ref:
        raise ValueError('Плейт изменился. Выберите его заново.')
    return plate


def rows(plate, character=None):
    with _db() as db:
        records = db.execute('SELECT id,data FROM personal_stat_rows WHERE plate_id=? ORDER BY id', (plate['id'],)).fetchall()
    result = []
    for record in records:
        data = json.loads(record['data'])
        # IDs occupy a separate range from guild requirement IDs.
        row = (10**12 + record['id'], reference(plate), *data[:7], plate['owner'], '', *data[7:])
        if character is None or row[2] == character:
            result.append(row)
    return result


def create(user, name, items=()):
    name = name.strip()
    if not name or len(name) > 80 or any(c in name for c in '\r\n[]'):
        raise ValueError('Название: от 1 до 80 символов, без квадратных скобок.')
    records = [item_data(it) for it in items]
    if len(records) > 1000:
        raise ValueError('В одном плейте допускается до 1000 требований.')
    plate_id = secrets.token_hex(8)
    with _db() as db:
        if db.execute('SELECT count(*) FROM personal_stat_plates WHERE owner=? AND guild_id=?',
                      (str(user['discord_id']), user['guild_id'])).fetchone()[0] >= 100:
            raise ValueError('Можно сохранить до 100 личных плейтов.')
        db.execute('INSERT INTO personal_stat_plates VALUES (?,?,?,?,0)',
                   (plate_id, user['guild_id'], str(user['discord_id']), name))
        db.executemany('INSERT INTO personal_stat_rows(plate_id,data) VALUES (?,?)',
                       [(plate_id, json.dumps(row, ensure_ascii=False)) for row in records])
    return plate_id


def item_data(item):
    from cogs.stat_requirements import STAT_MOD_PRIMARY, STAT_SET
    if item['kind'] == 'stored':
        return list(item['data'])
    kind = item['kind']
    stat, op, value = item.get('stat'), item.get('op', '>='), item.get('value', 1)
    skill, slot, compare = None, None, None
    if kind == 'pri':
        stat, op, value = STAT_MOD_PRIMARY, '=', item['ids'][0]
        slot = item['slot']
        skill = ','.join(map(str, item['ids'])) if len(item['ids']) > 1 else None
    elif kind == 'set':
        stat, skill = STAT_SET, str(item['set_id'])
    elif kind == 'cmp':
        compare = item['compare']
    elif kind not in ('num', 'cmp'):
        skill = ','.join(map(str, item['ids']))
    return [item['char'], stat, op, float(value), item.get('priority', 'required'),
            item.get('line', ''), '', skill, slot, compare, None]


def add(user, plate_id, item):
    get(plate_id, user, edit=True)
    data = item_data(item)
    with _db() as db:
        if db.execute('SELECT count(*) FROM personal_stat_rows WHERE plate_id=?', (plate_id,)).fetchone()[0] >= 1000:
            raise ValueError('В одном плейте допускается до 1000 требований.')
        db.execute('INSERT INTO personal_stat_rows(plate_id,data) VALUES (?,?)',
                   (plate_id, json.dumps(data, ensure_ascii=False)))


def remove_row(user, plate_id, row_id):
    get(plate_id, user, edit=True)
    with _db() as db:
        db.execute('DELETE FROM personal_stat_rows WHERE plate_id=? AND id=?', (plate_id, row_id - 10**12))


def edit_numeric(user, plate_id, row_id, operator, value, priority):
    from cogs.stat_requirements import STAT_CHOICES
    get(plate_id, user, edit=True)
    with _db() as db:
        row = db.execute('SELECT data FROM personal_stat_rows WHERE plate_id=? AND id=?',
                         (plate_id, row_id - 10**12)).fetchone()
        if not row:
            raise ValueError('Требование недоступно.')
        data = json.loads(row[0])
        if data[1] not in {choice.value for choice in STAT_CHOICES} or data[9]:
            raise ValueError('Для изменения этого условия удалите строку и добавьте новую.')
        data[2:5] = [operator, value, priority]
        db.execute('UPDATE personal_stat_rows SET data=? WHERE plate_id=? AND id=?',
                   (json.dumps(data, ensure_ascii=False), plate_id, row_id - 10**12))


def share(user, plate_id, shared):
    get(plate_id, user, edit=True)
    with _db() as db:
        db.execute('UPDATE personal_stat_plates SET shared=? WHERE id=? AND owner=? AND guild_id=?',
                   (int(shared), plate_id, str(user['discord_id']), user['guild_id']))


def delete(user, plate_id):
    get(plate_id, user, edit=True)
    with _db() as db:
        db.execute('DELETE FROM personal_stat_rows WHERE plate_id=?', (plate_id,))
        db.execute('DELETE FROM personal_stat_plates WHERE id=?', (plate_id,))


def rename(user, plate_id, name):
    get(plate_id, user, edit=True)
    name = name.strip()
    if not name or len(name) > 80 or any(c in name for c in '\r\n[]'):
        raise ValueError('Название: от 1 до 80 символов, без квадратных скобок.')
    with _db() as db:
        db.execute('UPDATE personal_stat_plates SET name=? WHERE id=?', (name, plate_id))


def parse_hotbot(parsed):
    from services import hotbot_import as hb
    by_name = database.get_game_unit_en_names()
    names = sorted(by_name, key=len, reverse=True)
    items, skipped = [], []
    for section in parsed['sections']:
        for line in section['lines']:
            item = hb.parse_line(line, names, by_name)
            if isinstance(item, str):
                skipped.append({'line': line, 'reason': item})
            else:
                items.append(item)
    items = hb._expand(items)
    hb._resolve_crit(items, [])
    return items, skipped


def parse_source(messages, user):
    """A verified own-bot DM identifies the source plate; copy its actual rows."""
    from services import hotbot_import as hb
    first = messages[0] if messages else {}
    if first.get('_droidec_dm'):
        def texts(components):
            for component in components:
                if component.get('type') == 10:
                    yield component.get('content', '')
                yield from texts(component.get('components') or [])
        title = next((match.group(1) for text in texts(first.get('components') or [])
                      if (match := re.match(r'^### 📋 (.+) \(игрок: .+\) — ', text))), None)
        if not title:
            raise ValueError('Вставьте ссылку на ответ команды /статы из своей лички с Дроидцем.')
        with scope(user):
            if title.startswith(PREFIX):
                try:
                    title = reference(resolve(title, user['guild_id']))
                except ValueError:
                    matches = [p for p in list_plates(user) if display_reference(reference(p)) == title]
                    if len(matches) != 1:
                        raise ValueError('Личный плейт недоступен или его имя неоднозначно. Откройте его на сайте.') from None
                    title = reference(matches[0])
            elif title not in database.get_all_stat_requirement_plates(guild_id=user['guild_id']):
                raise ValueError('Исходный плейт недоступен в вашей гильдии.')
            items = []
            for base in database.get_stat_requirement_characters(title, guild_id=user['guild_id']):
                for row in database.get_stat_requirements(title, base, guild_id=user['guild_id']):
                    items.append({'kind': 'stored', 'data': [*row[2:9], *row[11:15]], 'label': hb.describe_row(row)})
        return display_reference(title).removeprefix(PREFIX), items, []
    parsed = hb.parse_messages(messages)
    items, skipped = parse_hotbot(parsed)
    return parsed['title'], items, skipped
