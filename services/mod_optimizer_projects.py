"""Private optimizer drafts and their standard stat requirement rows."""
import json
import math
import secrets

import database
import stat_engine
from services import mod_optimizer_plans as plans, personal_plates

GROUP = 'Требования проекта'


def _table(db):
    db.execute("""CREATE TABLE IF NOT EXISTS mod_optimizer_projects (
        id TEXT PRIMARY KEY, owner TEXT NOT NULL, guild_id INTEGER NOT NULL,
        ally_code TEXT NOT NULL, data TEXT NOT NULL)""")


def list_projects(user, code):
    with plans._db() as db:
        _table(db)
        rows = db.execute(
            'SELECT id, data FROM mod_optimizer_projects WHERE owner=? AND guild_id=? AND ally_code=?',
            (str(user['discord_id']), user['guild_id'], code)).fetchall()
    return sorted(((row['id'], json.loads(row['data'])['name']) for row in rows), key=lambda row: row[1].casefold())


def get(user, code, project_id):
    with plans._db() as db:
        _table(db)
        row = db.execute('SELECT data FROM mod_optimizer_projects WHERE id=? AND owner=? AND guild_id=? AND ally_code=?',
                         (project_id, str(user['discord_id']), user['guild_id'], code)).fetchone()
    if row is None:
        raise ValueError('Проект недоступен.')
    return json.loads(row['data'])


def save(user, code, project_id, project, locked):
    if not project['name']:
        raise ValueError('Введите название проекта.')
    with plans._db() as db:
        _table(db)
        db.execute('BEGIN IMMEDIATE')
        owner = (str(user['discord_id']), user['guild_id'], code)
        data = json.dumps(project, ensure_ascii=False)
        if project_id:
            result = db.execute('UPDATE mod_optimizer_projects SET data=? WHERE id=? AND owner=? AND guild_id=? AND ally_code=?',
                                (data, project_id, *owner))
            if not result.rowcount:
                raise ValueError('Проект недоступен.')
        else:
            count = db.execute('SELECT count(*) FROM mod_optimizer_projects WHERE owner=? AND guild_id=? AND ally_code=?',
                               owner).fetchone()[0]
            if count >= 50:
                raise ValueError('Можно сохранить до 50 проектов на аккаунт.')
            project_id = secrets.token_hex(12)
            db.execute('INSERT INTO mod_optimizer_projects VALUES (?,?,?,?,?)', (project_id, *owner, data))
        db.execute('INSERT OR REPLACE INTO mod_optimizer_locks VALUES (?,?,?,?)',
                   (*owner, json.dumps(sorted(set(locked)))))
    return project_id


def delete(user, code, project_id):
    with plans._db() as db:
        _table(db)
        result = db.execute('DELETE FROM mod_optimizer_projects WHERE id=? AND owner=? AND guild_id=? AND ally_code=?',
                            (project_id, str(user['discord_id']), user['guild_id'], code))
        if not result.rowcount:
            raise ValueError('Проект недоступен.')


def editor_options():
    from cogs.stat_requirements import STAT_CHOICES, MOD_SLOT_LABELS
    return {
        'stats': [(stat.value, stat.name) for stat in STAT_CHOICES if stat.value != 'Relic'],
        'slots': {slot: {'label': MOD_SLOT_LABELS[slot], 'options': [
            (opt['unit_stat'], opt['label_ru'].split(' +')[0]) for opt in opts]}
            for slot, opts in stat_engine.MOD_PRIMARY_OPTIONS.items()},
        'sets': list(stat_engine.MOD_SET_NAMES_RU.items()),
    }


def _rule(rule, stats, slots, sets, base, allowed):
    if not isinstance(rule, dict) or rule.get('priority') not in ('required', 'optional'):
        raise ValueError('Проверьте приоритет требований.')
    kind = rule.get('kind')
    item = {'kind': kind, 'char': base, 'priority': rule['priority']}
    if kind in ('num', 'cmp'):
        stat, op, raw = rule.get('stat'), rule.get('op'), rule.get('value')
        if not isinstance(stat, str) or stat not in stats or op not in ('>=', '<=', '='):
            raise ValueError('Проверьте характеристику и оператор.')
        try:
            value = float(str(raw).replace(',', '.'))
        except (ValueError, OverflowError):
            raise ValueError('Введите числовое значение характеристики.') from None
        if not math.isfinite(value) or abs(value) > 1e9:
            raise ValueError('Введите допустимое значение характеристики.')
        item.update(stat=stat, op=op, value=value)
        if kind == 'cmp':
            peer = rule.get('compare')
            if not isinstance(peer, str) or peer == base or peer not in allowed or value < 0:
                raise ValueError('Выберите другого персонажа и укажите разницу от нуля.')
            item['compare'] = peer
    elif kind == 'set':
        value = rule.get('set_id')
        if type(value) is not int or value not in sets:
            raise ValueError('Выберите сет модулей.')
        item['set_id'] = value
    elif kind in ('any', 'only'):
        ids = rule.get('ids')
        if (not isinstance(ids, list) or not 1 <= len(ids) <= len(sets)
                or any(type(value) is not int or value not in sets for value in ids)):
            raise ValueError('Выберите допустимые сеты.')
        item['ids'] = list(dict.fromkeys(ids))
    elif kind == 'pri':
        slot, ids = rule.get('slot'), rule.get('ids')
        if (not isinstance(slot, str) or slot not in slots or not isinstance(ids, list)
                or not 1 <= len(ids) <= len(slots[slot])
                or any(type(value) is not int or value not in slots[slot] for value in ids)):
            raise ValueError('Выберите допустимые основы для этого слота.')
        item.update(slot=slot, ids=list(dict.fromkeys(ids)))
    else:
        raise ValueError('Неизвестное требование.')
    return item


def validate(raw, allowed):
    if len(raw) > 500_000:
        raise ValueError('Проект слишком большой.')
    try:
        project = json.loads(raw)
    except (ValueError, RecursionError):
        raise ValueError('Не удалось прочитать проект.') from None
    if not isinstance(project, dict) or not isinstance(project.get('name'), str) or len(project['name'].strip()) > 80:
        raise ValueError('Название проекта должно содержать до 80 символов.')
    entries = project.get('characters')
    if not isinstance(entries, list) or len(entries) > len(allowed):
        raise ValueError('Проверьте список персонажей.')
    options = editor_options()
    stats, sets = dict(options['stats']), dict(options['sets'])
    slots = {key: dict(value['options']) for key, value in options['slots'].items()}
    cleaned, seen = [], set()
    for entry in entries:
        base = entry.get('base') if isinstance(entry, dict) else None
        if not isinstance(base, str) or base not in allowed or base in seen:
            raise ValueError('Выберите уникальных персонажей из своего ростера.')
        rules = entry.get('rules')
        if not isinstance(rules, list) or len(rules) > 30 or type(entry.get('enabled')) is not bool:
            raise ValueError('Можно задать до 30 требований на персонажа.')
        seen.add(base)
        cleaned.append({'base': base, 'enabled': entry['enabled'],
                        'rules': [_rule(rule, stats, slots, sets, base, allowed) for rule in rules]})
    return {'name': project['name'].strip(), 'characters': cleaned}


def seed(plate, guild_id, allowed):
    from cogs.stat_requirements import STAT_MOD_PRIMARY, STAT_SET, STAT_SET_ANY, STAT_SET_ONLY, parse_id_list
    stats = dict(editor_options()['stats'])
    entries, skipped = [], 0
    for base in database.get_stat_requirement_characters(plate, guild_id=guild_id):
        if base not in allowed:
            skipped += 1
            continue
        rules = []
        source_rows = database.get_stat_requirements(plate, base, guild_id=guild_id)
        conditions = database.get_stat_requirement_conditions([row[0] for row in source_rows], guild_id)
        for row in source_rows:
            stat, op, value, priority = row[3:7]
            if row[14] or row[0] in conditions or len(rules) >= 30:
                skipped += 1
                continue
            rule = {'priority': 'required' if priority == 'required' else 'optional'}
            if stat == STAT_MOD_PRIMARY:
                rule.update(kind='pri', slot=row[12], ids=parse_id_list(row[11]) or [int(value)])
            elif stat == STAT_SET:
                rule.update(kind='set', set_id=int(value))
            elif stat in (STAT_SET_ANY, STAT_SET_ONLY):
                rule.update(kind='any' if stat == STAT_SET_ANY else 'only', ids=parse_id_list(row[11]))
            elif stat in stats and op in ('>=', '<=', '=') and (not row[13] or row[13] in allowed):
                rule.update(kind='cmp' if row[13] else 'num', stat=stat, op=op, value=value)
                if row[13]:
                    rule['compare'] = row[13]
            else:
                skipped += 1
                continue
            rules.append(rule)
        entries.append({'base': base, 'enabled': True, 'rules': rules})
    return {'name': personal_plates.display_reference(plate)[:80], 'characters': entries}, skipped


def requirements(project):
    from cogs.stat_requirements import STAT_SET_ANY, STAT_SET_ONLY
    rows = {}
    for entry in project['characters']:
        if not entry['enabled']:
            continue
        base = entry['base']
        rows[base] = []
        for rule in entry['rules']:
            kind = rule['kind']
            if kind in ('any', 'only'):
                rule = {**rule, 'stat': STAT_SET_ANY if kind == 'any' else STAT_SET_ONLY}
            data = personal_plates.item_data(rule)
            rows[base].append((None, GROUP, *data[:7], None, None, *data[7:]))
    return rows
