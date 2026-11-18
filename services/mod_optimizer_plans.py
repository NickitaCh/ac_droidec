"""Durable, owner-scoped optimizer jobs and one-shot application claims."""
from contextlib import contextmanager
import hashlib
import json
import secrets
import sqlite3
import time

import database

TTL = 3600


@contextmanager
def _db():
    db = sqlite3.connect(database.DB_NAME, timeout=20)
    db.row_factory = sqlite3.Row
    try:
        db.execute('''CREATE TABLE IF NOT EXISTS mod_optimizer_plans (
            id TEXT PRIMARY KEY, owner TEXT NOT NULL, guild_id INTEGER NOT NULL,
            ally_code TEXT NOT NULL, created REAL NOT NULL, updated REAL NOT NULL,
            state TEXT NOT NULL, data TEXT NOT NULL, message TEXT NOT NULL DEFAULT '')''')
        db.execute('''CREATE TABLE IF NOT EXISTS mod_optimizer_locks (
            owner TEXT NOT NULL, guild_id INTEGER NOT NULL, ally_code TEXT NOT NULL,
            characters TEXT NOT NULL, PRIMARY KEY (owner, guild_id, ally_code))''')
        db.execute('''CREATE TABLE IF NOT EXISTS mod_optimizer_saved (
            id TEXT PRIMARY KEY, owner TEXT NOT NULL, guild_id INTEGER NOT NULL,
            ally_code TEXT NOT NULL, created REAL NOT NULL, kind TEXT NOT NULL, data TEXT NOT NULL)''')
        db.commit()
        yield db
        db.commit()
    finally:
        db.close()


def fingerprint(mods, units=None):
    records = [{k: m.get(k) for k in ('id', 'level', 'rarity', 'tier', 'set_id', 'slot_id', 'locked',
                                     'equipped_to', 'primary', 'secondaries')} for m in mods]
    data = {'mods': sorted(records, key=lambda m: m['id']), 'units': units or {}}
    return hashlib.sha256(json.dumps(data, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def requirements_fingerprint(plates, guild_id):
    rows = {p: {b: database.get_stat_requirements(p, b, guild_id=guild_id)
                for b in database.get_stat_requirement_characters(p, guild_id=guild_id)} for p in sorted(plates)}
    return hashlib.sha256(json.dumps(rows, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def get_locks(owner, guild_id, code):
    with _db() as db:
        row = db.execute('SELECT characters FROM mod_optimizer_locks WHERE owner=? AND guild_id=? AND ally_code=?',
                         (str(owner), guild_id, str(code))).fetchone()
    return json.loads(row[0]) if row else []


def set_locks(owner, guild_id, code, characters):
    with _db() as db:
        db.execute('INSERT OR REPLACE INTO mod_optimizer_locks VALUES (?,?,?,?)',
                   (str(owner), guild_id, str(code), json.dumps(sorted(set(characters)))))


def create(owner, guild_id, ally_code, data):
    now, token = time.time(), secrets.token_urlsafe(24)
    with _db() as db:
        db.execute('BEGIN IMMEDIATE')
        active = db.execute("SELECT 1 FROM mod_optimizer_plans WHERE owner=? AND ally_code=? "
                            "AND state IN ('running','applying') AND updated>?", (str(owner), str(ally_code), now - TTL)).fetchone()
        if active:
            raise ValueError('Для этого аккаунта уже выполняется расчёт или применение.')
        db.execute('DELETE FROM mod_optimizer_plans WHERE created<?', (now - 7 * 86400,))
        db.execute('INSERT INTO mod_optimizer_plans VALUES (?,?,?,?,?,?,?,?,?)',
                   (token, str(owner), guild_id, str(ally_code), now, now, 'running', json.dumps(data, ensure_ascii=False), ''))
    return token


def get(token, owner, guild_id):
    with _db() as db:
        row = db.execute('SELECT * FROM mod_optimizer_plans WHERE id=? AND owner=? AND guild_id=?',
                         (token, str(owner), guild_id)).fetchone()
        if row is None:
            row = db.execute("""SELECT *, 'saved' AS state, '' AS message, created AS updated
                                FROM mod_optimizer_saved WHERE id=? AND owner=? AND guild_id=?""",
                             (token, str(owner), guild_id)).fetchone()
    if row is None:
        raise ValueError('План не найден.')
    result = dict(row)
    result['data'] = json.loads(result['data'])
    result['expired'] = time.time() - result['created'] > TTL
    if time.time() - result['updated'] > TTL and result['state'] in ('running', 'applying'):
        result['message'] = 'Задача прервана или превысила время ожидания. Сначала проверьте расстановку в игре.'
        result['state'] = 'error'
    return result


def saved(owner, guild_id, code):
    with _db() as db:
        return [dict(row) for row in db.execute(
            """SELECT id, kind, created, json_extract(data, '$.preset_name') AS name,
                      json_extract(data, '$.preset_scope') AS scope,
                      CASE WHEN json_type(data, '$.preset_units') = 'array'
                           THEN json_array_length(data, '$.preset_units') END AS unit_count
               FROM mod_optimizer_saved WHERE owner=? AND guild_id=? AND ally_code=? ORDER BY created DESC""",
            (str(owner), guild_id, str(code)))]


def save_result(token, owner, guild_id, name=''):
    with _db() as db:
        row = db.execute("""SELECT ally_code, data FROM mod_optimizer_plans
                            WHERE id=? AND owner=? AND guild_id=? AND state IN ('ready','complete')""",
                         (token, str(owner), guild_id)).fetchone()
        if row is None:
            raise ValueError('Готовый результат не найден.')
        data = json.loads(row['data'])
        data['preset_name'] = name
        db.execute('INSERT OR IGNORE INTO mod_optimizer_saved VALUES (?,?,?,?,?,?,?)',
                   ('saved-' + token, str(owner), guild_id, row['ally_code'], time.time(), 'result',
                    json.dumps(data, ensure_ascii=False)))


def save_baseline(owner, guild_id, code, mods, *, name='', units=None, scope=None):
    token = secrets.token_urlsafe(24)
    with _db() as db:
        db.execute('INSERT INTO mod_optimizer_saved VALUES (?,?,?,?,?,?,?)',
                   (token, str(owner), guild_id, str(code), time.time(), 'baseline',
                    json.dumps({'baseline': mods, 'preset_name': name, 'preset_units': units,
                                'preset_scope': scope}, ensure_ascii=False)))
    return token


def baseline_units(preset):
    return set(preset.get('preset_units') or ()) | {
        mod['equipped_to'] for mod in preset['baseline'] if mod.get('equipped_to')
    }


def restore_results(preset, snapshot, *, selected=None):
    """Build the exact saved assignment using the account's current copies of the mods."""
    mods = {m['id']: m for m in snapshot['mods']}
    if 'baseline' in preset:
        known = baseline_units(preset)
        if selected is not None and (not selected or selected - known):
            raise ValueError('Выберите персонажей из этой раскладки.')
        desired = {base: [] for base in sorted(known if selected is None else selected)}
        for mod in preset['baseline']:
            if mod.get('equipped_to') in desired:
                desired[mod['equipped_to']].append(mod)
    else:
        desired = {r['base_id']: r['used_mods'] for r in preset.get('results', []) if r.get('owned')}
    if not desired:
        raise ValueError('В пресете нет раскладки для восстановления.')
    if any(base not in snapshot['unit_ids'] for base in desired):
        raise ValueError('В аккаунте больше нет персонажа из пресета.')
    if any(m['id'] not in mods for group in desired.values() for m in group):
        raise ValueError('Часть модулей из пресета отсутствует. Сохраните новую сборку.')
    return [{'owned': True, 'base_id': base, 'used_mods': [mods[m['id']] for m in group]}
            for base, group in desired.items()]


def finish(token, state, data, message='', expected='running'):
    with _db() as db:
        changed = db.execute('UPDATE mod_optimizer_plans SET state=?, data=?, message=?, updated=? WHERE id=? AND state=?',
                            (state, json.dumps(data, ensure_ascii=False), message, time.time(), token, expected)).rowcount
    if changed != 1:
        raise ValueError('Состояние плана изменилось. Обновите страницу.')


def claim(token, owner, guild_id):
    with _db() as db:
        db.execute('BEGIN IMMEDIATE')
        row = db.execute('SELECT * FROM mod_optimizer_plans WHERE id=? AND owner=? AND guild_id=?',
                         (token, str(owner), guild_id)).fetchone()
        if row is None or row['state'] != 'ready' or time.time() - row['created'] > TTL:
            raise ValueError('План устарел или уже применялся. Выполните новый расчёт.')
        if not json.loads(row['data']).get('game_source'):
            raise ValueError('План по CSV нельзя применить к игре.')
        if json.loads(row['data']).get('dry_run', True):
            raise ValueError('Этот план рассчитан только для Dry run и не может быть применён.')
        active = db.execute("SELECT 1 FROM mod_optimizer_plans WHERE owner=? AND ally_code=? "
                            "AND state='applying' AND id<>? AND updated>?",
                            (str(owner), row['ally_code'], token, time.time() - TTL)).fetchone()
        if active:
            raise ValueError('Для аккаунта уже применяется другой план.')
        db.execute("UPDATE mod_optimizer_plans SET state='applying', updated=? WHERE id=?", (time.time(), token))
    return json.loads(row['data'])
