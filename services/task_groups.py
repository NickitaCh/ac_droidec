"""Named guild goals backed by ordinary audited tasks and cached rosters."""
import json
import sqlite3
import uuid

import database
from services import journey_guide


def connect():
    conn = sqlite3.connect(database.DB_NAME)
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS task_groups (
            id INTEGER PRIMARY KEY, guild_id INTEGER NOT NULL, name TEXT NOT NULL,
            requirements TEXT NOT NULL, deadline TEXT NOT NULL, created_by TEXT NOT NULL,
            archived INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS task_group_players (
            group_id INTEGER NOT NULL, ally_code TEXT NOT NULL,
            PRIMARY KEY(group_id, ally_code));
        CREATE TABLE IF NOT EXISTS task_group_links (
            group_id INTEGER NOT NULL, task_id INTEGER NOT NULL,
            PRIMARY KEY(group_id, task_id));
    """)
    return conn


def list_groups(guild_id, archived=False):
    conn = connect()
    try:
        rows = conn.execute("SELECT * FROM task_groups WHERE guild_id=? AND archived=? ORDER BY id DESC",
                            (guild_id, int(archived))).fetchall()
        groups = []
        for row in rows:
            group = dict(row)
            group['requirements'] = json.loads(group['requirements'])
            group['players'] = [r[0] for r in conn.execute(
                "SELECT ally_code FROM task_group_players WHERE group_id=?", (group['id'],))]
            groups.append(group)
        return groups
    finally:
        conn.close()


def meets(unit, req, thresholds):
    if not unit:
        return False
    if req['type'] != 'omicron':
        return journey_guide.unit_meets(unit, req['type'], req['value'])
    tier = thresholds.get(req['value'], (None, None))[1]
    return tier is not None and any(s.get('id') == req['value'] and s.get('tier', -1) >= tier
                                    for s in unit.get('skill', []))


def create(guild_id, name, requirements, codes, deadline, actor):
    """Create an immutable goal; add ordinary tasks only where still needed.

    Existing stronger tasks are reused, and different criteria remain separate.
    """
    thresholds = database.get_all_skill_tier_thresholds()
    rosters = database.get_player_units_bulk(codes)
    batch = uuid.uuid4().hex[:12]
    created = {}
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        cur = conn.execute("INSERT INTO task_groups(guild_id,name,requirements,deadline,created_by) VALUES (?,?,?,?,?)",
                           (guild_id, name, json.dumps(requirements, ensure_ascii=False), deadline, actor))
        gid = cur.lastrowid
        conn.executemany("INSERT INTO task_group_players VALUES (?,?)", [(gid, c) for c in codes])
        for code in codes:
            roster = rosters.get(code, {})
            for req in requirements:
                unit = roster.get(req['base_id'])
                if meets(unit, req, thresholds):
                    continue
                candidates = conn.execute("SELECT task_id,target_value FROM tasks WHERE guild_id=? AND ally_code=? AND base_id=? AND target_type=? AND status='ACTIVE' AND is_personal=0",
                                          (guild_id, code, req['base_id'], req['type'])).fetchall()
                existing = next((r for r in candidates if
                    (r['target_value'] == req['value'] if req['type'] == 'omicron'
                     else int(r['target_value']) >= int(req['value']))), None)
                if existing:
                    task_id = existing['task_id']
                else:
                    initial = journey_guide.unit_progress(unit, req['type']) if unit and req['type'] != 'omicron' else None
                    task_id = conn.execute("""INSERT INTO tasks
                        (ally_code,base_id,target_type,target_value,deadline,status,created_by,date_created,
                         guild_id,batch_id,initial_value,current_value,is_personal)
                        VALUES (?,?,?,?,?,'ACTIVE',?,datetime('now'),?,?,?,?,0)""",
                        (code,req['base_id'],req['type'],req['value'],deadline,actor,guild_id,batch,initial,initial)).lastrowid
                    created.setdefault(code, []).append(req)
                conn.execute("INSERT OR IGNORE INTO task_group_links VALUES (?,?)", (gid, task_id))
        conn.commit()
        return gid, created
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


def report(guild_id, group, *, rosters=None, names=None, current=None, thresholds=None):
    names = database.get_player_names(guild_id) if names is None else names
    current = {str(c) for _, c, _ in database.get_all_user_mappings(guild_id)} if current is None else current
    thresholds = database.get_all_skill_tier_thresholds() if thresholds is None else thresholds
    rosters = database.get_player_units_bulk(group['players']) if rosters is None else rosters
    rows = []
    for code in group['players']:
        if code not in current:
            continue
        roster = rosters.get(code, {})
        cells = [{'met': meets(roster.get(r['base_id']), r, thresholds),
                  'current': journey_guide.unit_progress(roster[r['base_id']], r['type'])
                  if r['base_id'] in roster and r['type'] != 'omicron' else ''}
                 for r in group['requirements']]
        done = sum(c['met'] for c in cells)
        rows.append({'code': code, 'name': names.get(code, code), 'done': done,
                     'total': len(cells), 'cells': cells, 'cached': bool(roster)})
    rows.sort(key=lambda p: (-p['done'], p['name'].casefold()))
    return {**group, 'rows': rows, 'ready': sum(p['done'] == p['total'] for p in rows),
            'total': len(rows), 'done': sum(p['done'] for p in rows),
            'checks': sum(p['total'] for p in rows)}


def reports(guild_id, groups):
    codes = list({c for g in groups for c in g['players']})
    rosters = database.get_player_units_bulk(codes)
    names = database.get_player_names(guild_id)
    current = {str(c) for _, c, _ in database.get_all_user_mappings(guild_id)}
    thresholds = database.get_all_skill_tier_thresholds()
    return [report(guild_id, g, rosters=rosters, names=names, current=current, thresholds=thresholds) for g in groups]


def archive(guild_id, group_id, archived=True):
    conn = connect()
    try:
        conn.execute("UPDATE task_groups SET archived=? WHERE guild_id=? AND id=?", (int(archived), guild_id, group_id))
        conn.commit()
    finally:
        conn.close()
