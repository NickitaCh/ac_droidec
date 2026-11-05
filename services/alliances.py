"""Officer-managed alliances and explicitly shared guild configuration.

Linked resources are local, read-only materializations: existing bot checks keep
their guild scope and local IDs. Readers refresh them transactionally. Revoking
access freezes the last version as an independent copy; a copy cannot be recalled.
"""

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from urllib.parse import unquote

import database

KINDS = {"plate": "Плейты", "dc_base": "Обычные требования ДК",
         "dc_focused": "Персональные требования ДК", "omicrons": "Порядок омикронов"}


class AllianceError(ValueError):
    pass


def _ensure(c):
    statements = (
        """CREATE TABLE IF NOT EXISTS alliances (
            id INTEGER PRIMARY KEY, name TEXT NOT NULL, owner_guild_id INTEGER NOT NULL,
            created_by TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)""",
        """CREATE TABLE IF NOT EXISTS alliance_members (
            guild_id INTEGER PRIMARY KEY, alliance_id INTEGER NOT NULL,
            can_invite INTEGER NOT NULL DEFAULT 0, can_manage INTEGER NOT NULL DEFAULT 0,
            joined_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)""",
        """CREATE TABLE IF NOT EXISTS alliance_invites (
            id INTEGER PRIMARY KEY, alliance_id INTEGER NOT NULL, from_guild_id INTEGER NOT NULL,
            to_guild_id INTEGER NOT NULL, created_by TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending', created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            expires_at TEXT NOT NULL DEFAULT (datetime('now', '+14 days')))""",
        """CREATE UNIQUE INDEX IF NOT EXISTS alliance_pending_invite
            ON alliance_invites(alliance_id, to_guild_id) WHERE status = 'pending'""",
        """CREATE TABLE IF NOT EXISTS alliance_shares (
            id INTEGER PRIMARY KEY, alliance_id INTEGER NOT NULL, owner_guild_id INTEGER NOT NULL,
            kind TEXT NOT NULL, resource_key TEXT NOT NULL, allow_copy INTEGER NOT NULL,
            allow_link INTEGER NOT NULL, all_members INTEGER NOT NULL DEFAULT 0,
            UNIQUE(alliance_id, owner_guild_id, kind, resource_key))""",
        """CREATE TABLE IF NOT EXISTS alliance_share_recipients (
            share_id INTEGER NOT NULL, guild_id INTEGER NOT NULL, PRIMARY KEY(share_id, guild_id))""",
        """CREATE TABLE IF NOT EXISTS alliance_links (
            id INTEGER PRIMARY KEY, share_id INTEGER NOT NULL, guild_id INTEGER NOT NULL,
            kind TEXT NOT NULL, local_key TEXT NOT NULL, content_hash TEXT NOT NULL,
            source_version INTEGER NOT NULL DEFAULT -1,
            UNIQUE(guild_id, kind, local_key))""",
        """CREATE TABLE IF NOT EXISTS alliance_revisions (
            guild_id INTEGER NOT NULL, kind TEXT NOT NULL, version INTEGER NOT NULL,
            PRIMARY KEY(guild_id,kind))""",
        """CREATE TABLE IF NOT EXISTS alliance_plate_children (
            guild_id INTEGER NOT NULL, parent_plate TEXT NOT NULL, child_plate TEXT NOT NULL,
            PRIMARY KEY(guild_id,child_plate))""",
    )
    for sql in statements:
        c.execute(sql)


def _ensure_resources(c):
    for name in ("stat_requirements", "stat_plates", "stat_plate_components",
                 "stat_plate_character_schemes", "stat_plate_character_order", "stat_plate_squads",
                 "datacron_requirements", "datacron_focused_requirements", "datacron_requirement_stats",
                 "datacron_requirement_squads", "guild_omicron_priority", "guild_omicron_requirement_rules"):
        getattr(database, "_ensure_" + name + "_table")(c)
    tables = {
        "plate": ("stat_plates", "stat_requirements", "stat_plate_components", "stat_plate_character_schemes", "stat_plate_character_order", "stat_plate_squads", "stat_character_squads"),
        "dc_base": ("datacron_requirements", "datacron_requirement_stats", "datacron_requirement_squads"),
        "dc_focused": ("datacron_focused_requirements", "datacron_requirement_squads"),
        "omicrons": ("guild_omicron_priority", "guild_omicron_requirement_rules"),
    }
    for kind, names in tables.items():
        for table in names:
            for event in ("INSERT", "UPDATE", "DELETE"):
                record = "OLD" if event == "DELETE" else "NEW"
                c.execute("CREATE TRIGGER IF NOT EXISTS alliance_rev_%s_%s_%s AFTER %s ON %s BEGIN "
                          "INSERT INTO alliance_revisions(guild_id,kind,version) VALUES(%s.guild_id,'%s',1) "
                          "ON CONFLICT(guild_id,kind) DO UPDATE SET version=version+1; END" %
                          (kind, table, event, event, table, record, kind))


def _revision(c, gid, kind):
    row = c.execute("SELECT version FROM alliance_revisions WHERE guild_id=? AND kind=?", (gid, kind)).fetchone()
    return row[0] if row else 0


@contextmanager
def _db(resources=False):
    conn = sqlite3.connect(database.DB_NAME, timeout=20)
    conn.row_factory = sqlite3.Row
    try:
        _ensure(conn)
        if resources:
            _ensure_resources(conn.cursor())
        conn.commit()
        conn.execute("BEGIN IMMEDIATE")
        yield conn
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


def _guild(c, gid):
    row = c.execute("SELECT * FROM guilds WHERE id = ? AND is_active = 1", (gid,)).fetchone()
    if not row:
        raise AllianceError("Гильдия не подключена или отключена.")
    return row


def _member(c, gid, alliance_id=None, permission=None):
    _guild(c, gid)
    row = c.execute("SELECT * FROM alliance_members WHERE guild_id = ?", (gid,)).fetchone()
    if not row or (alliance_id is not None and row["alliance_id"] != alliance_id):
        raise AllianceError("Гильдия не состоит в этом альянсе.")
    if permission and not row[permission]:
        raise AllianceError("Альянс не предоставил вашей гильдии это право.")
    return row


def _name(value):
    value = value.strip()
    if not value or len(value) > 100 or any(ord(x) < 32 for x in value):
        raise AllianceError("Название должно содержать от 1 до 100 символов.")
    return value


def create(gid, name, actor):
    with _db() as c:
        _guild(c, gid)
        if c.execute("SELECT 1 FROM alliance_members WHERE guild_id=?", (gid,)).fetchone():
            raise AllianceError("Гильдия уже состоит в альянсе.")
        aid = c.execute("INSERT INTO alliances(name,owner_guild_id,created_by) VALUES(?,?,?)",
                        (_name(name), gid, str(actor))).lastrowid
        c.execute("INSERT INTO alliance_members(guild_id,alliance_id,can_invite,can_manage) VALUES(?,?,1,1)", (gid, aid))
        c.execute("UPDATE alliance_invites SET status='cancelled' WHERE to_guild_id=? AND status='pending'", (gid,))
        return aid


def invite(gid, target, actor):
    with _db() as c:
        m = _member(c, gid, permission="can_invite")
        _guild(c, target)
        if c.execute("SELECT 1 FROM alliance_members WHERE guild_id=?", (target,)).fetchone():
            raise AllianceError("Эта гильдия уже состоит в альянсе.")
        c.execute("UPDATE alliance_invites SET status='expired' WHERE status='pending' AND expires_at<=datetime('now')")
        try:
            c.execute("INSERT INTO alliance_invites(alliance_id,from_guild_id,to_guild_id,created_by) VALUES(?,?,?,?)",
                      (m["alliance_id"], gid, target, str(actor)))
        except sqlite3.IntegrityError:
            raise AllianceError("Приглашение этой гильдии уже отправлено.") from None


def respond(gid, invite_id, accept):
    with _db() as c:
        _guild(c, gid)
        row = c.execute("SELECT * FROM alliance_invites WHERE id=? AND to_guild_id=? AND status='pending' AND expires_at>datetime('now')",
                        (invite_id, gid)).fetchone()
        if not row or not c.execute("SELECT 1 FROM alliances WHERE id=?", (row["alliance_id"],)).fetchone():
            raise AllianceError("Приглашение недоступно или истекло.")
        if accept:
            if c.execute("SELECT 1 FROM alliance_members WHERE guild_id=?", (gid,)).fetchone():
                raise AllianceError("Сначала выйдите из текущего альянса.")
            c.execute("INSERT INTO alliance_members(guild_id,alliance_id) VALUES(?,?)", (gid, row["alliance_id"]))
            c.execute("UPDATE alliance_invites SET status='cancelled' WHERE to_guild_id=? AND status='pending' AND id<>?", (gid, invite_id))
        c.execute("UPDATE alliance_invites SET status=? WHERE id=?", ("accepted" if accept else "declined", invite_id))


def cancel_invite(gid, invite_id):
    with _db() as c:
        m = _member(c, gid, permission="can_invite")
        c.execute("UPDATE alliance_invites SET status='cancelled' WHERE id=? AND alliance_id=? AND status='pending'", (invite_id, m["alliance_id"]))


def membership(gid, target, action, can_invite=False, can_manage=False):
    with _db() as c:
        m = _member(c, gid)
        other = _member(c, target, m["alliance_id"])
        alliance = c.execute("SELECT * FROM alliances WHERE id=?", (m["alliance_id"],)).fetchone()
        if action == "leave":
            if gid != target:
                raise AllianceError("Можно выйти только от имени своей гильдии.")
        elif not m["can_manage"]:
            raise AllianceError("У вашей гильдии нет права управления альянсом.")
        if action == "permissions":
            if target == alliance["owner_guild_id"]:
                raise AllianceError("Права основателя не ограничиваются.")
            c.execute("UPDATE alliance_members SET can_invite=?,can_manage=? WHERE guild_id=?", (int(can_invite), int(can_manage), target))
            return
        if action == "transfer":
            if gid != alliance["owner_guild_id"]:
                raise AllianceError("Передать владение может только гильдия-основатель.")
            c.execute("UPDATE alliances SET owner_guild_id=? WHERE id=?", (target, m["alliance_id"]))
            c.execute("UPDATE alliance_members SET can_invite=1,can_manage=1 WHERE guild_id=?", (target,))
            return
        if action not in ("leave", "remove"):
            raise AllianceError("Неизвестное действие.")
        count = c.execute("SELECT COUNT(*) FROM alliance_members WHERE alliance_id=?", (m["alliance_id"],)).fetchone()[0]
        if target == alliance["owner_guild_id"] and count > 1:
            raise AllianceError("Сначала передайте владение другой гильдии.")
        _freeze_links(c, "guild_id=? OR share_id IN (SELECT id FROM alliance_shares WHERE owner_guild_id=?)", (target, target))
        c.execute("DELETE FROM alliance_share_recipients WHERE guild_id=? OR share_id IN (SELECT id FROM alliance_shares WHERE owner_guild_id=?)", (target, target))
        c.execute("DELETE FROM alliance_shares WHERE owner_guild_id=?", (target,))
        c.execute("DELETE FROM alliance_members WHERE guild_id=?", (target,))
        # A departed guild must not retain invitations sent on behalf of the alliance.
        c.execute("UPDATE alliance_invites SET status='cancelled' WHERE from_guild_id=? AND status='pending'", (target,))
        if count == 1:
            c.execute("DELETE FROM alliances WHERE id=?", (m["alliance_id"],))
            c.execute("UPDATE alliance_invites SET status='cancelled' WHERE alliance_id=? AND status='pending'", (m["alliance_id"],))


def rename(gid, name):
    with _db() as c:
        m = _member(c, gid, permission="can_manage")
        c.execute("UPDATE alliances SET name=? WHERE id=?", (_name(name), m["alliance_id"]))


def _freeze_links(c, where, params):
    c.execute("DELETE FROM alliance_links WHERE " + where, params)


def _rows(c, table, gid, extra="", args=()):
    return [dict(x) for x in c.execute("SELECT * FROM " + table + " WHERE guild_id=?" + extra, (gid, *args))]


def _snapshot(c, gid, kind, key):
    if kind not in KINDS:
        raise AllianceError("Неизвестный тип данных.")
    if kind == "omicrons" and key != "priority":
        raise AllianceError("Неизвестный список омикронов.")
    if c.execute("SELECT 1 FROM alliance_links WHERE guild_id=? AND kind=? AND local_key=?", (gid, kind, key)).fetchone():
        raise AllianceError("Подключённые данные нельзя передавать дальше. Сначала создайте независимую копию.")
    if kind == "plate" and c.execute("SELECT 1 FROM alliance_plate_children ch JOIN alliance_links l ON l.guild_id=ch.guild_id AND l.kind='plate' AND l.local_key=ch.parent_plate WHERE ch.guild_id=? AND ch.child_plate=?", (gid, key)).fetchone():
        raise AllianceError("Источники подключённого плейта нельзя передавать дальше до отключения обновлений.")
    if kind == "plate":
        plate = c.execute("SELECT * FROM stat_plates WHERE guild_id=? AND name=?", (gid, key)).fetchone()
        if not plate:
            raise AllianceError("Плейт больше не существует.")
        cursor = c.cursor()
        if plate["is_modular"]:
            pairs = database._resolve_modular_plate_leaf_pairs(key, gid, cursor)
        else:
            pairs = [(key, x[0]) for x in c.execute("SELECT DISTINCT character_key FROM stat_requirements WHERE guild_id=? AND plate_name=?", (gid, key))]
        reqs, schemes, order = [], [], []
        for leaf, character in pairs:
            if leaf != key and c.execute("SELECT 1 FROM alliance_links WHERE guild_id=? AND kind='plate' AND local_key=?", (gid, leaf)).fetchone():
                raise AllianceError("В модульном плейте есть подключённый источник. Сначала отключите его обновления.")
            if c.execute("SELECT 1 FROM alliance_plate_children ch JOIN alliance_links l ON l.guild_id=ch.guild_id AND l.kind='plate' AND l.local_key=ch.parent_plate WHERE ch.guild_id=? AND ch.child_plate=?", (gid, leaf)).fetchone():
                raise AllianceError("В модульном плейте есть источник подключённых данных. Сначала отключите его обновления.")
            reqs.extend(_rows(c, "stat_requirements", gid, " AND plate_name=? AND character_key=? ORDER BY id", (leaf, character)))
            schemes.extend(_rows(c, "stat_plate_character_schemes", gid, " AND plate_name=? AND character_key=?", (leaf, character)))
            if character not in order:
                order.append(character)
        if not plate["is_modular"]:
            preferred = [x["character_key"] for x in _rows(c, "stat_plate_character_order", gid, " AND plate_name=? ORDER BY sort_order", (key,))]
            order.sort(key=lambda x: (preferred.index(x) if x in preferred else len(preferred), x))
        leaves = list(dict.fromkeys(leaf for leaf, _ in pairs))
        return {"title": key, "description": plate["description"], "requirements": reqs,
                "schemes": schemes, "order": order, "squads": _rows(c, "stat_plate_squads", gid, " AND plate_name=?", (key,)),
                "char_squads": _rows(c, "stat_character_squads", gid, " AND plate_name=?", (key,)),
                "leaf_char_squads": {leaf: _rows(c, "stat_character_squads", gid, " AND plate_name=?", (leaf,)) for leaf in leaves},
                "modular": bool(plate["is_modular"]), "pairs": pairs,
                "leaf_squads": {leaf: _rows(c, "stat_plate_squads", gid, " AND plate_name=?", (leaf,)) for leaf in leaves}}
    if kind in ("dc_base", "dc_focused"):
        table = "datacron_requirements" if kind == "dc_base" else "datacron_focused_requirements"
        rows = _rows(c, table, gid, " AND id=?", (key,))
        if not rows:
            raise AllianceError("Требование ДК больше не существует.")
        row = rows[0]
        return {"title": "Сезон %s · %s" % (row["set_id"], row.get("pack") or row.get("character_key") or "ДК #%s" % key),
                "row": row, "stats": _rows(c, "datacron_requirement_stats", gid, " AND req_id=? ORDER BY id", (key,)) if kind == "dc_base" else [],
                "squads": _rows(c, "datacron_requirement_squads", gid, " AND kind=? AND req_id=?", ("base" if kind == "dc_base" else "focused", key))}
    return {"title": "Порядок и правила омикронов", "priority": _rows(c, "guild_omicron_priority", gid, " ORDER BY priority"),
            "rules": _rows(c, "guild_omicron_requirement_rules", gid)}


def _insert(c, table, row, **overrides):
    data = {k: v for k, v in row.items() if k != "id"}
    data.update(overrides)
    columns = list(data)
    return c.execute("INSERT INTO " + table + " (" + ",".join(columns) + ") VALUES (" + ",".join("?" for _ in columns) + ")", list(data.values())).lastrowid


def _apply(c, gid, kind, snapshot, key=None, actor="alliance"):
    if kind == "plate":
        name = _name(key)
        if snapshot.get("modular"):
            leaves = list(dict.fromkeys(leaf for leaf, _ in snapshot["pairs"]))
            known = {r[0] for r in c.execute("SELECT child_plate FROM alliance_plate_children WHERE guild_id=? AND parent_plate=?", (gid, name))}
            names = {leaf: name[:60] + " · " + leaf[:20] + " · " + hashlib.sha256((name + "\0" + leaf).encode()).hexdigest()[:8] for leaf in leaves}
            for child in names.values():
                if child not in known and c.execute("SELECT 1 FROM stat_plates WHERE guild_id=? AND name=?", (gid, child)).fetchone():
                    raise AllianceError("Имя источника модульного плейта занято. Выберите другое название копии.")
            for table in ("stat_requirements", "stat_plate_character_schemes", "stat_plate_character_order"):
                c.execute("DELETE FROM " + table + " WHERE guild_id=? AND plate_name=?", (gid, name))
            c.execute("DELETE FROM stat_plate_components WHERE guild_id=? AND parent_plate=?", (gid, name))
            # A removed source is frozen as an ordinary copy, so existing guild
            # references remain valid and officers can edit or remove it.
            for child in known - set(names.values()):
                c.execute("DELETE FROM alliance_plate_children WHERE guild_id=? AND child_plate=?", (gid, child))
            for leaf, child in names.items():
                # Repeated leaf/character occurrences keep their repeated rows;
                # components are unique in the existing database schema.
                reqs = [r for r in snapshot["requirements"] if r["plate_name"] == leaf]
                leaf_snapshot = {**snapshot, "modular": False, "description": "Источник плейта «%s»" % name,
                                 "requirements": reqs, "schemes": [r for r in snapshot["schemes"] if r["plate_name"] == leaf],
                                 "order": list(dict.fromkeys(ck for lp, ck in snapshot["pairs"] if lp == leaf)),
                                 "squads": snapshot["leaf_squads"].get(leaf, []),
                                 "char_squads": snapshot.get("leaf_char_squads", {}).get(leaf, [])}
                _apply(c, gid, "plate", leaf_snapshot, child, actor)
                c.execute("INSERT OR IGNORE INTO alliance_plate_children VALUES(?,?,?)", (gid, name, child))
            c.execute("INSERT INTO stat_plates(guild_id,name,description,created_by,created_at,is_modular) VALUES(?,?,?,?,datetime('now'),1) ON CONFLICT(guild_id,name) DO UPDATE SET description=excluded.description,is_modular=1", (gid, name, snapshot["description"], actor))
            for leaf, character in snapshot["pairs"]:
                c.execute("INSERT OR IGNORE INTO stat_plate_components(guild_id,parent_plate,source_plate,character_key,created_by,created_at) VALUES(?,?,?,?,?,datetime('now'))", (gid, name, names[leaf], character, actor))
            c.execute("DELETE FROM stat_plate_squads WHERE guild_id=? AND plate_name=?", (gid, name))
            c.execute("DELETE FROM stat_character_squads WHERE guild_id=? AND plate_name=?", (gid, name))
            for row in snapshot.get("char_squads", []):
                _insert(c, "stat_character_squads", row, guild_id=gid, plate_name=name)
            for row in snapshot["squads"]:
                _insert(c, "stat_plate_squads", row, guild_id=gid, plate_name=name)
            return name
        # Updates replace only the linked plate; unrelated guild data stays untouched.
        c.execute("DELETE FROM stat_plate_components WHERE guild_id=? AND parent_plate=?", (gid, name))
        c.execute("DELETE FROM alliance_plate_children WHERE guild_id=? AND parent_plate=?", (gid, name))
        for table in ("stat_requirements", "stat_plate_character_schemes", "stat_plate_character_order", "stat_plate_squads", "stat_character_squads"):
            c.execute("DELETE FROM " + table + " WHERE guild_id=? AND plate_name=?", (gid, name))
        c.execute("INSERT INTO stat_plates(guild_id,name,description,created_by,created_at,is_modular) VALUES(?,?,?,?,datetime('now'),0) ON CONFLICT(guild_id,name) DO UPDATE SET description=excluded.description,is_modular=0", (gid, name, snapshot["description"], actor))
        for row in snapshot["requirements"]:
            _insert(c, "stat_requirements", row, guild_id=gid, plate_name=name, created_by=actor)
        # Modular components can repeat a character. First effective label wins.
        seen = set()
        for row in snapshot["schemes"]:
            identity = (row["character_key"], row["scheme_num"])
            if identity not in seen:
                _insert(c, "stat_plate_character_schemes", row, guild_id=gid, plate_name=name)
                seen.add(identity)
        for n, character in enumerate(snapshot["order"]):
            c.execute("INSERT INTO stat_plate_character_order VALUES(?,?,?,?)", (gid, name, character, n))
        for row in snapshot["squads"]:
            _insert(c, "stat_plate_squads", row, guild_id=gid, plate_name=name)
        for row in snapshot.get("char_squads", []):
            _insert(c, "stat_character_squads", row, guild_id=gid, plate_name=name)
        return name
    if kind in ("dc_base", "dc_focused"):
        table = "datacron_requirements" if kind == "dc_base" else "datacron_focused_requirements"
        row = snapshot["row"]
        if key is None:
            key = _insert(c, table, row, guild_id=gid, created_by=actor)
        else:
            data = {k: v for k, v in row.items() if k not in ("id", "guild_id", "created_by", "created_at")}
            c.execute("UPDATE " + table + " SET " + ",".join(k + "=?" for k in data) + " WHERE guild_id=? AND id=?", (*data.values(), gid, key))
        dc_kind = "base" if kind == "dc_base" else "focused"
        if kind == "dc_base":
            c.execute("DELETE FROM datacron_requirement_stats WHERE guild_id=? AND req_id=?", (gid, key))
            for row in snapshot["stats"]:
                _insert(c, "datacron_requirement_stats", row, guild_id=gid, req_id=key)
        c.execute("DELETE FROM datacron_requirement_squads WHERE guild_id=? AND kind=? AND req_id=?", (gid, dc_kind, key))
        for row in snapshot["squads"]:
            _insert(c, "datacron_requirement_squads", row, guild_id=gid, req_id=key)
        return str(key)
    c.execute("DELETE FROM guild_omicron_priority WHERE guild_id=?", (gid,))
    c.execute("DELETE FROM guild_omicron_requirement_rules WHERE guild_id=?", (gid,))
    for row in snapshot["priority"]:
        _insert(c, "guild_omicron_priority", row, guild_id=gid)
    for row in snapshot["rules"]:
        _insert(c, "guild_omicron_requirement_rules", row, guild_id=gid, updated_by=actor)
    return "priority"


def _hash(snapshot):
    return hashlib.sha256(json.dumps(snapshot, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def _allowed(c, share, gid, mode=None):
    if not share or share["owner_guild_id"] == gid:
        return False
    member = c.execute("SELECT alliance_id FROM alliance_members WHERE guild_id=?", (gid,)).fetchone()
    owner = c.execute("SELECT alliance_id FROM alliance_members WHERE guild_id=?", (share["owner_guild_id"],)).fetchone()
    if not member or not owner or member[0] != share["alliance_id"] or owner[0] != share["alliance_id"]:
        return False
    if not c.execute("SELECT 1 FROM guilds WHERE id=? AND is_active=1", (share["owner_guild_id"],)).fetchone():
        return False
    if mode and not share["allow_" + mode]:
        return False
    return bool(share["all_members"] or c.execute("SELECT 1 FROM alliance_share_recipients WHERE share_id=? AND guild_id=?", (share["id"], gid)).fetchone())


def share(gid, kind, key, recipients, allow_copy, allow_link, all_members=False):
    with _db(resources=True) as c:
        m = _member(c, gid)
        _snapshot(c, gid, kind, str(key))  # Ownership and existence, not a client-supplied title.
        if not (allow_copy or allow_link):
            raise AllianceError("Выберите копирование или подключение.")
        recipients = {int(x) for x in recipients} - {gid}
        for target in recipients:
            _member(c, target, m["alliance_id"])
        if not recipients and not all_members:
            raise AllianceError("Выберите гильдии-получатели.")
        c.execute("""INSERT INTO alliance_shares(alliance_id,owner_guild_id,kind,resource_key,allow_copy,allow_link,all_members)
            VALUES(?,?,?,?,?,?,?) ON CONFLICT(alliance_id,owner_guild_id,kind,resource_key) DO UPDATE SET
            allow_copy=excluded.allow_copy,allow_link=excluded.allow_link,all_members=excluded.all_members""",
                  (m["alliance_id"], gid, kind, str(key), int(allow_copy), int(allow_link), int(all_members)))
        sid = c.execute("SELECT id FROM alliance_shares WHERE alliance_id=? AND owner_guild_id=? AND kind=? AND resource_key=?", (m["alliance_id"], gid, kind, str(key))).fetchone()[0]
        c.execute("DELETE FROM alliance_share_recipients WHERE share_id=?", (sid,))
        c.executemany("INSERT INTO alliance_share_recipients VALUES(?,?)", [(sid, x) for x in recipients])
        current = c.execute("SELECT * FROM alliance_shares WHERE id=?", (sid,)).fetchone()
        for link in c.execute("SELECT * FROM alliance_links WHERE share_id=?", (sid,)).fetchall():
            if not _allowed(c, current, link["guild_id"], "link"):
                _freeze_links(c, "id=?", (link["id"],))
        return sid


def revoke(gid, sid):
    with _db() as c:
        _member(c, gid)
        if not c.execute("SELECT 1 FROM alliance_shares WHERE id=? AND owner_guild_id=?", (sid, gid)).fetchone():
            raise AllianceError("Можно отозвать только собственные данные.")
        _freeze_links(c, "share_id=?", (sid,))
        c.execute("DELETE FROM alliance_share_recipients WHERE share_id=?", (sid,))
        c.execute("DELETE FROM alliance_shares WHERE id=?", (sid,))


def consume(gid, sid, mode, name, actor, replace=False):
    if mode not in ("copy", "link"):
        raise AllianceError("Выберите копирование или подключение.")
    with _db(resources=True) as c:
        _member(c, gid)
        row = c.execute("SELECT * FROM alliance_shares WHERE id=?", (sid,)).fetchone()
        if not _allowed(c, row, gid, mode):
            raise AllianceError("Владелец не предоставил вашей гильдии этот доступ.")
        kind = row["kind"]
        snapshot = _snapshot(c, row["owner_guild_id"], kind, row["resource_key"])
        key = None
        if kind == "plate":
            key = _name(name)
            if c.execute("SELECT 1 FROM stat_plates WHERE guild_id=? AND name=? UNION SELECT 1 FROM stat_requirements WHERE guild_id=? AND plate_name=?", (gid, key, gid, key)).fetchone():
                raise AllianceError("Плейт с таким именем уже существует. Выберите другое название.")
        if kind == "omicrons":
            if c.execute("SELECT 1 FROM alliance_links WHERE guild_id=? AND kind='omicrons'", (gid,)).fetchone():
                raise AllianceError("Порядок омикронов уже подключён. Сначала отключите его.")
            existing = c.execute("SELECT 1 FROM guild_omicron_priority WHERE guild_id=? UNION SELECT 1 FROM guild_omicron_requirement_rules WHERE guild_id=?", (gid, gid)).fetchone()
            if existing and not replace:
                raise AllianceError("Подтвердите замену собственного порядка и правил омикронов.")
        if mode == "link" and c.execute("SELECT 1 FROM alliance_links WHERE guild_id=? AND share_id=?", (gid, sid)).fetchone():
            raise AllianceError("Эти данные уже подключены.")
        key = _apply(c, gid, kind, snapshot, key, str(actor))
        if mode == "link":
            c.execute("INSERT INTO alliance_links(share_id,guild_id,kind,local_key,content_hash,source_version) VALUES(?,?,?,?,?,?)", (sid, gid, kind, key, _hash(snapshot), _revision(c, row["owner_guild_id"], kind)))
        return key


def detach(gid, link_id):
    with _db() as c:
        _member(c, gid)
        _freeze_links(c, "id=? AND guild_id=?", (link_id, gid))


def refresh_links(gid, kinds=None):
    """Called before configuration reads in both web and bot; no in-memory roster cache."""
    # Most guilds have no links. Do not migrate resource schemas for ordinary reads.
    c = sqlite3.connect(database.DB_NAME, timeout=20)
    try:
        if not c.execute("SELECT 1 FROM sqlite_master WHERE name='alliance_links'").fetchone():
            return
        # A character-by-character plate report can make thousands of reads.
        # Only acquire a write transaction when a source revision or access changed.
        kind_filter = " AND l.kind IN (" + ",".join("?" for _ in kinds) + ")" if kinds else ""
        dirty = c.execute("""SELECT 1 FROM alliance_links l
            LEFT JOIN alliance_shares s ON s.id=l.share_id
            LEFT JOIN alliance_members recipient ON recipient.guild_id=l.guild_id
            LEFT JOIN alliance_members owner ON owner.guild_id=s.owner_guild_id
            LEFT JOIN guilds g ON g.id=s.owner_guild_id
            LEFT JOIN alliance_revisions v ON v.guild_id=s.owner_guild_id AND v.kind=l.kind
            WHERE l.guild_id=?""" + kind_filter + """ AND (
                s.id IS NULL OR s.allow_link=0 OR recipient.alliance_id IS NULL OR owner.alliance_id IS NULL
                OR recipient.alliance_id<>s.alliance_id OR owner.alliance_id<>s.alliance_id
                OR g.id IS NULL OR g.is_active<>1 OR l.source_version<>COALESCE(v.version,0)
                OR (s.all_members=0 AND NOT EXISTS (SELECT 1 FROM alliance_share_recipients r WHERE r.share_id=s.id AND r.guild_id=l.guild_id))
            ) LIMIT 1""", (gid, *(kinds or ())))
        if not dirty.fetchone():
            return
    finally:
        c.close()
    with _db() as c:
        for link in c.execute("SELECT * FROM alliance_links WHERE guild_id=?", (gid,)).fetchall():
            if kinds and link["kind"] not in kinds:
                continue
            row = c.execute("SELECT * FROM alliance_shares WHERE id=?", (link["share_id"],)).fetchone()
            if not _allowed(c, row, gid, "link"):
                _freeze_links(c, "id=?", (link["id"],))
                continue
            revision = _revision(c, row["owner_guild_id"], row["kind"])
            if revision == link["source_version"]:
                continue
            try:
                snapshot = _snapshot(c, row["owner_guild_id"], row["kind"], row["resource_key"])
            except AllianceError:
                _freeze_links(c, "id=?", (link["id"],))
                continue
            digest = _hash(snapshot)
            if digest != link["content_hash"]:
                _apply(c, gid, link["kind"], snapshot, link["local_key"])
            c.execute("UPDATE alliance_links SET content_hash=?,source_version=? WHERE id=?", (digest, revision, link["id"]))


def assert_editable(gid, kind, key):
    c = sqlite3.connect(database.DB_NAME)
    try:
        if not c.execute("SELECT 1 FROM sqlite_master WHERE name='alliance_links'").fetchone():
            return
        if c.execute("SELECT 1 FROM alliance_links WHERE guild_id=? AND kind=? AND local_key=?", (gid, kind, str(key))).fetchone():
            raise AllianceError("Эти данные подключены из альянса. Отключите обновления на странице «Альянс», чтобы редактировать свою копию.")
        if kind == "plate" and c.execute("SELECT 1 FROM sqlite_master WHERE name='alliance_plate_children'").fetchone():
            if c.execute("SELECT 1 FROM alliance_plate_children ch JOIN alliance_links l ON l.guild_id=ch.guild_id AND l.kind='plate' AND l.local_key=ch.parent_plate WHERE ch.guild_id=? AND ch.child_plate=?", (gid, str(key))).fetchone():
                raise AllianceError("Это источник подключённого модульного плейта. Сначала отключите его обновления на странице «Альянс».")
    finally:
        c.close()


def page(gid):
    refresh_links(gid)
    with _db(resources=True) as c:
        _guild(c, gid)
        m = c.execute("SELECT * FROM alliance_members WHERE guild_id=?", (gid,)).fetchone()
        alliance = c.execute("SELECT * FROM alliances WHERE id=?", (m["alliance_id"],)).fetchone() if m else None
        members = c.execute("SELECT m.*,g.name FROM alliance_members m JOIN guilds g ON g.id=m.guild_id WHERE m.alliance_id=? ORDER BY g.name", (m["alliance_id"],)).fetchall() if m else []
        incoming = c.execute("SELECT i.*,a.name alliance_name,g.name from_name FROM alliance_invites i JOIN alliances a ON a.id=i.alliance_id JOIN guilds g ON g.id=i.from_guild_id WHERE i.to_guild_id=? AND i.status='pending' AND i.expires_at>datetime('now') ORDER BY i.id DESC", (gid,)).fetchall()
        outgoing = c.execute("SELECT i.*,g.name to_name FROM alliance_invites i JOIN guilds g ON g.id=i.to_guild_id WHERE i.alliance_id=? AND i.status='pending' AND i.expires_at>datetime('now') ORDER BY i.id DESC", (m["alliance_id"],)).fetchall() if m else []
        guilds = c.execute("SELECT id,name FROM guilds WHERE is_active=1 AND id<>? AND id NOT IN (SELECT guild_id FROM alliance_members) ORDER BY name", (gid,)).fetchall()
        resources = []
        if m:
            for row in c.execute("SELECT name FROM stat_plates WHERE guild_id=? ORDER BY name", (gid,)):
                resources.append(("plate", row[0], row[0]))
            for kind, table in (("dc_base", "datacron_requirements"), ("dc_focused", "datacron_focused_requirements")):
                for row in c.execute("SELECT * FROM " + table + " WHERE guild_id=? ORDER BY set_id DESC,id", (gid,)):
                    title = "Сезон %s · %s" % (row["set_id"], row["pack"] or (row["character_key"] if kind == "dc_focused" else "ДК #%s" % row["id"]))
                    resources.append((kind, str(row["id"]), title))
            resources.append(("omicrons", "priority", "Порядок и правила омикронов"))
        linked = {(x["kind"], x["local_key"]) for x in c.execute("SELECT * FROM alliance_links WHERE guild_id=?", (gid,))}
        linked.update(("plate", x[0]) for x in c.execute("SELECT ch.child_plate FROM alliance_plate_children ch JOIN alliance_links l ON l.guild_id=ch.guild_id AND l.kind='plate' AND l.local_key=ch.parent_plate WHERE ch.guild_id=?", (gid,)))
        resources = [x for x in resources if (x[0], x[1]) not in linked]
        mine, available = [], []
        if m:
            for row in c.execute("SELECT s.*,g.name owner_name FROM alliance_shares s JOIN guilds g ON g.id=s.owner_guild_id WHERE s.alliance_id=? ORDER BY g.name,s.kind,s.id", (m["alliance_id"],)).fetchall():
                if row["owner_guild_id"] != gid and not _allowed(c, row, gid):
                    continue
                try:
                    snapshot = _snapshot(c, row["owner_guild_id"], row["kind"], row["resource_key"])
                except AllianceError:
                    continue
                item = dict(row)
                item["title"] = snapshot["title"]
                item["recipients"] = [x[0] for x in c.execute("SELECT guild_id FROM alliance_share_recipients WHERE share_id=?", (row["id"],))]
                item["linked"] = c.execute("SELECT id FROM alliance_links WHERE guild_id=? AND share_id=?", (gid, row["id"])).fetchone() is not None
                (mine if row["owner_guild_id"] == gid else available).append(item)
        links = [dict(x) for x in c.execute("SELECT l.*,g.name owner_name FROM alliance_links l JOIN alliance_shares s ON s.id=l.share_id JOIN guilds g ON g.id=s.owner_guild_id WHERE l.guild_id=?", (gid,))]
        return {"alliance": dict(alliance) if alliance else None, "membership": dict(m) if m else None,
                "members": members, "incoming": incoming, "outgoing": outgoing, "guilds": guilds,
                "resources": resources, "mine": mine, "available": available, "links": links, "kinds": KINDS}


def preview(gid, sid):
    with _db(resources=True) as c:
        _member(c, gid)
        row = c.execute("SELECT * FROM alliance_shares WHERE id=?", (sid,)).fetchone()
        if not row or not (row["owner_guild_id"] == gid or _allowed(c, row, gid)):
            raise AllianceError("Данные не предоставлены вашей гильдии.")
        return row["kind"], _snapshot(c, row["owner_guild_id"], row["kind"], row["resource_key"])


def read_only_context(gid, path):
    """Small page notice; don't initialize schemas or expose other guild content."""
    if not path.startswith(("/plates", "/datacrons", "/omicrons/priority")):
        return []
    c = sqlite3.connect(database.DB_NAME)
    c.row_factory = sqlite3.Row
    try:
        if not c.execute("SELECT 1 FROM sqlite_master WHERE name='alliance_links'").fetchone():
            return []
        result = []
        for row in c.execute("SELECT l.*,g.name owner_name FROM alliance_links l JOIN alliance_shares s ON s.id=l.share_id JOIN guilds g ON g.id=s.owner_guild_id WHERE l.guild_id=?", (gid,)).fetchall():
            kind, key = row["kind"], row["local_key"]
            prefixes = []
            label = KINDS[kind]
            if kind == "plate" and path.startswith("/plates"):
                prefixes = ["/plates/" + key]
                prefixes.extend("/plates/" + x[0] for x in c.execute("SELECT child_plate FROM alliance_plate_children WHERE guild_id=? AND parent_plate=?", (gid, key)))
                if path != "/plates" and not any(unquote(path) == p or unquote(path).startswith(p + "/") for p in prefixes):
                    continue
                label = key
            elif kind in ("dc_base", "dc_focused") and path.startswith("/datacrons"):
                table = "datacron_requirements" if kind == "dc_base" else "datacron_focused_requirements"
                dc = c.execute("SELECT set_id,pack FROM " + table + " WHERE guild_id=? AND id=?", (gid, key)).fetchone()
                if not dc or (path != "/datacrons" and not path.startswith("/datacrons/%s" % dc["set_id"])):
                    continue
                prefixes = ["/datacrons/%s/%s/%s" % (dc["set_id"], "requirements" if kind == "dc_base" else "focused", key), "/datacrons/%s/clear" % dc["set_id"]]
                label = "%s · сезон %s · %s" % (KINDS[kind], dc["set_id"], dc["pack"] or "без пака")
            elif kind == "omicrons" and path.startswith("/omicrons/priority"):
                prefixes = ["/omicrons/priority"]
            else:
                continue
            result.append({"label": label, "owner": row["owner_name"], "prefixes": prefixes})
        return result
    finally:
        c.close()


def pending_count(gid):
    c = sqlite3.connect(database.DB_NAME)
    try:
        if not c.execute("SELECT 1 FROM sqlite_master WHERE name='alliance_invites'").fetchone():
            return 0
        return c.execute("SELECT COUNT(*) FROM alliance_invites i JOIN alliances a ON a.id=i.alliance_id WHERE i.to_guild_id=? AND i.status='pending' AND i.expires_at>datetime('now')", (gid,)).fetchone()[0]
    finally:
        c.close()
