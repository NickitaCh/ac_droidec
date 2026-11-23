"""Decode attack events from the game's Territory War activity channel."""
from contextlib import closing
import json
import sqlite3

import database
from services import game_account as ga
from services.tb_live import _messages, _number, _text

SERVICE = "ChannelRpc"
METHOD = "GetEvents"
ENDPOINT = "https://swprod-channel.capitalgames.com/rpc"
PAGE_SIZE = 100
_TW_CONFLICT_ACTIVITY = "TERRITORY_WAR_CONFLICT_ACTIVITY"
_WIN_KEYS = {"TERRITORY_CHANNEL_ACTIVITY_CONFLICT_SQUAD_WIN",
             "TERRITORY_CHANNEL_ACTIVITY_CONFLICT_FLEET_WIN"}
_STATE_KEY = "tw_battle_log"


def zone_channels(war):
    return {zone["channel_id"]: zone["id"] for side in ("own", "opponent") for zone in war[side]
            if zone.get("channel_id")}


def arguments(channels):
    # GetChannelEventsRequest.ChannelEventRequest → ChannelId / Limit.
    return ((1, 0, PAGE_SIZE), *((4, 2, ga.pack(((1, 2, channel.encode()), (5, 0, PAGE_SIZE))))
                                 for channel in channels))


def parse(payload, channels, war):
    """Decode wins, attack starts and recorded holds from zone events."""
    battles = {}
    guild_ids = {war[side].get("id") for side in ("home_guild", "opponent_guild")} - {None, ""}
    for event in _messages(ga.fields(payload), 1):
        identifier, timestamp = _text(event, 1), _number(event, 6)
        attacker_id, attacker = _text(event, 7), _text(event, 8)
        channel = _text(event, 3)
        if channel not in channels or not all((identifier, timestamp, attacker_id)):
            continue
        for data in _messages(event, 10):
            if _text(data, 5) != _TW_CONFLICT_ACTIVITY:
                continue
            activity = next(_messages(data, 6), [])
            zone = next(_messages(activity, 1), [])
            squad = next(_messages(activity, 2), [])
            # Zone IDs are mirrored. The guild on the activity is the attacker's side.
            zone_id, guild_id = _text(zone, 1), _text(zone, 12)
            defender_id = _text(squad, 1)
            if (not defender_id or defender_id == attacker_id or zone_id != channels[channel]
                    or guild_id not in guild_ids or _text(zone, 7) != war["event_id"]):
                continue
            activity_message = next(_messages(zone, 4), [])
            key = _text(activity_message, 1)
            status = _number(squad, 6)
            if key in _WIN_KEYS and status == 3:
                outcome = "win"
            elif key == "EMPTY" and status == 2:
                outcome = "start"
            elif key == "EMPTY" and status == 1 and (_number(squad, 8) or 0) > 0:
                outcome = "hold"
            else:
                continue
            cells = _messages(next(_messages(squad, 4), []), 1)
            leader = next((_text(cell, 2) for cell in cells), "")
            # The victory message carries the score in its first formatting parameter.
            banner_value = _text(next(_messages(activity_message, 2), []), 3) if outcome == "win" else ""
            banners = None
            if banner_value.isascii() and banner_value.isdecimal() and len(banner_value) <= 9:
                banners = int(banner_value)
            battles[identifier] = {
                "id": identifier, "at": timestamp, "zone": zone_id, "guild_id": guild_id,
                "attacker_id": attacker_id, "defender_id": defender_id,
                "attacker": attacker or "Игрок без имени", "defender": _text(squad, 2) or "Игрок без имени",
                "leader": leader.partition(":")[0], "squad_id": _text(squad, 5), "outcome": outcome,
                "banners": banners,
            }
    return sorted(battles.values(), key=lambda row: (row["at"], row["id"]), reverse=True)


def record(guild_id, war, captured):
    if not war.get("event_id") or "battles" not in war:
        return
    with closing(sqlite3.connect(database.DB_NAME)) as db, db:
        db.execute("BEGIN IMMEDIATE")
        database._ensure_bot_state_table(db.cursor())
        previous = db.execute("SELECT value FROM bot_state WHERE guild_id=? AND key=?",
                              (guild_id, _STATE_KEY)).fetchone()
        saved = json.loads(previous[0]) if previous else {}
        if saved.get("event_id") != war["event_id"] and saved.get("at", 0) > captured:
            return
        same_war = saved.get("event_id") == war["event_id"]
        entries = {row["id"]: row for row in saved.get("rows", ())} if same_war else {}
        entries.update((row["id"], row) for row in war["battles"])
        rows = sorted(entries.values(), key=lambda row: (row["at"], row["id"]), reverse=True)
        result = {"event_id": war["event_id"], "at": max(captured, saved.get("at", 0)) if same_war else captured,
                  "home_id": war["home_guild"].get("id"), "rows": rows}
        db.execute("INSERT INTO bot_state(guild_id,key,value) VALUES(?,?,?) "
                   "ON CONFLICT(guild_id,key) DO UPDATE SET value=excluded.value",
                   (guild_id, _STATE_KEY, json.dumps(result)))


def latest(guild_id):
    raw = database.get_bot_state(_STATE_KEY, guild_id=guild_id)
    return json.loads(raw) if raw else {}


def _attempts(events):
    # A new lock on the same squad supersedes the previous unresolved lock.
    pending = {}
    attempts = []
    for row in sorted(events, key=lambda row: (row["at"], row["outcome"] != "start", row["id"])):
        squad = (row["guild_id"], row["zone"], row.get("squad_id"))
        start = pending.pop(squad, None) if squad[-1] else None
        if row["outcome"] == "start":
            attempt = {**row, "started_at": row["at"]}
            attempts.append(attempt)
            if squad[-1]:
                pending[squad] = attempt
        elif start is not None and all(
            (start[field + "_id"] == row[field + "_id"])
            if start.get(field + "_id") and row.get(field + "_id")
            else start[field] == row[field]
            for field in ("attacker", "defender")
        ):
            start.update(outcome=row["outcome"], banners=row.get("banners"), finished_at=row["at"])
        else:
            attempts.append({**row, "started_at": None, "finished_at": row["at"]})
    return attempts[::-1]


def zone_options(log):
    from services.tw_live_image import zone_key
    from tw_def_engine import ZONES

    known = {zone["key"]: zone["label"] for zone in ZONES}
    extra = {row["zone"]: row["zone"] for row in log.get("rows", ())
             if zone_key(row["zone"]) not in known}
    return {**known, **extra}


def selected_zone(log, zones, requested):
    from services.tw_live_image import zone_key

    if requested in zones:
        return requested
    if rows := log.get("rows"):
        first = rows[0]["zone"]
        return zone_key(first) or first
    return next(iter(zones), None)


def display_rows(log, *, zone=None):
    from services.tw_live_image import zone_key
    from tw_def_engine import ZONES_BY_KEY

    rows = _attempts(row for row in log.get("rows", [])
                     if zone is None or (zone_key(row["zone"]) or row["zone"]) == zone)
    units = sorted({row["leader"] for row in rows if row["leader"]})
    names = database.get_game_unit_names(units) if units else {}
    outcomes = {"win": "Победа", "start": "Ожидает исхода", "hold": "Защита устояла"}
    for row in rows:
        row["leader"] = names.get(row["leader"]) or row["leader"]
        row["zone"] = ZONES_BY_KEY.get(zone_key(row["zone"]), {}).get("label", row["zone"])
        row["side"] = "Наша атака" if row["guild_id"] == log.get("home_id") else "Атака соперника"
        row["result"] = outcomes[row["outcome"]]
        banners = row.get("banners")
        if row["outcome"] == "win" and banners is not None:
            row["result"] += f" · +{banners} стяг."
    return rows
