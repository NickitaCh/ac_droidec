"""Structured snapshots of the active Territory Battle."""
from contextlib import closing
import json
import re
import sqlite3

import database
from services import game_account as ga

_ZONE = re.compile(r"(.+_phase\d+_conflict\d+(?:_bonus)?)(?:_|$)")
_HISTORY_LIMIT = 288
OPEN_ZONE = 3


def _values(rows, tag, wire=2):
    return (value for number, kind, value in rows if (number, kind) == (tag, wire))


def _messages(rows, tag):
    return (ga.fields(value) for value in _values(rows, tag))


def _text(rows, tag):
    return next(_values(rows, tag), b"").decode("utf-8", "replace")


def _number(rows, tag):
    return next(_values(rows, tag, 0), None)


def parse(payload):
    active = list(_messages(ga.fields(payload), 1))
    if not active:
        return None
    if len(active) != 1:
        raise ga.AccountError("Игра вернула несколько активных ТБ.")
    tb = active[0]
    phase, end_ms, event_id = _number(tb, 9), _number(tb, 11), _text(tb, 1)
    if not event_id or not phase or not end_ms:
        raise ga.AccountError("Ответ игры не содержит идентификатор, этап или время окончания ТБ.")

    zones = {}
    for conflict in _messages(tb, 5):
        for zone in _messages(conflict, 5):
            zone_id = _text(zone, 1)
            if match := _ZONE.match(zone_id):
                zones[match[1]] = {
                    "id": zone_id, "status": _number(zone, 2), "points": _number(zone, 3),
                    "missions": [], "special": [], "operations": [],
                }
    for strike in _messages(tb, 6):
        for zone in _messages(strike, 5):
            zone_id = _text(zone, 1)
            match = _ZONE.match(zone_id)
            if match and match[1] in zones:
                zones[match[1]]["missions"].append({"id": zone_id, "players": _number(strike, 3)})
    for mission in _messages(tb, 13):
        for zone in _messages(mission, 1):
            zone_id = _text(zone, 1)
            match = _ZONE.match(zone_id)
            if match and match[1] in zones:
                zones[match[1]]["special"].append({
                    "id": zone_id, "players": _number(mission, 2), "wins": _number(mission, 3),
                })
    for recon in _messages(tb, 7):
        for zone in _messages(recon, 6):
            match = _ZONE.match(_text(zone, 1))
            if match and match[1] in zones:
                zones[match[1]]["recon_id"] = _text(zone, 1)
                zones[match[1]]["operations"] = [
                    {"id": _text(operation, 1), "slots": [
                        {"unit": _text(slot, 1).partition(":")[0], "donor": _text(slot, 5),
                         "squad_id": _text(row, 1)}
                        for row in _messages(operation, 2) for slot in _messages(row, 2)
                    ]}
                    for operation in _messages(recon, 7)
                ]

    players = {}
    for stat in _messages(tb, 14):
        if _text(stat, 1) == "summary":
            for player in _messages(stat, 2):
                pid, score = _text(player, 1), _number(player, 2)
                if pid and score is not None:
                    players[pid] = score
    return {"event": event_id, "phase": phase, "end_ms": end_ms, "zones": zones, "players": players}


def _ensure(db):
    db.execute("""CREATE TABLE IF NOT EXISTS tb_live_snapshots (
        guild_id INTEGER NOT NULL, owner TEXT NOT NULL, id INTEGER PRIMARY KEY,
        captured INTEGER NOT NULL, state TEXT NOT NULL)""")
    db.execute("CREATE INDEX IF NOT EXISTS tb_live_owner ON tb_live_snapshots (guild_id, owner, id)")


def save(guild_id, owner, captured, state):
    data = json.dumps(state, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    with closing(sqlite3.connect(database.DB_NAME)) as db, db:
        _ensure(db)
        db.execute("BEGIN IMMEDIATE")
        last = db.execute("SELECT captured FROM tb_live_snapshots WHERE guild_id=? AND owner=? ORDER BY id DESC LIMIT 1",
                          (guild_id, owner)).fetchone()
        if last and captured <= last[0]:
            return
        db.execute("INSERT INTO tb_live_snapshots (guild_id,owner,captured,state) VALUES (?,?,?,?)",
                   (guild_id, owner, captured, data))
        db.execute("""DELETE FROM tb_live_snapshots WHERE guild_id=? AND owner=? AND id IN (
            SELECT id FROM tb_live_snapshots WHERE guild_id=? AND owner=? ORDER BY id DESC LIMIT -1 OFFSET ?)""",
                   (guild_id, owner, guild_id, owner, _HISTORY_LIMIT))


def history(guild_id, owner, limit=12):
    with closing(sqlite3.connect(database.DB_NAME)) as db:
        _ensure(db)
        return [(ts, json.loads(state)) for ts, state in db.execute(
            "SELECT captured,state FROM tb_live_snapshots WHERE guild_id=? AND owner=? ORDER BY id DESC LIMIT ?",
            (guild_id, owner, limit))]


def latest_guild_state(guild_id):
    with closing(sqlite3.connect(database.DB_NAME)) as db:
        _ensure(db)
        row = db.execute(
            "SELECT state FROM tb_live_snapshots WHERE guild_id=? ORDER BY captured DESC, id DESC LIMIT 1",
            (guild_id,),
        ).fetchone()
    return json.loads(row[0]) if row else None
