"""Structured read-only snapshots for Territory War and guild raids."""
from collections import Counter
import json

import database

from services import game_account as ga, guild_compare
from services.tb_live import _messages, _number, _text


def _single(rows, tag, label):
    records = list(_messages(rows, tag))
    if len(records) > 1:
        raise ga.AccountError(f"Игра вернула несколько активных событий: {label}.")
    return records[0] if records else None


def _tw_teams(conflict):
    teams = []
    for record in _messages(conflict, 3):
        squad = next(_messages(record, 4), None)
        if not squad:
            continue
        units = [base for cell in _messages(squad, 1)
                 if (base := _text(cell, 2).partition(":")[0])]
        if units:
            teams.append({"units": units, "holds": _number(record, 8) or 0,
                          "player_name": _text(record, 2), "player_id": _text(record, 1)})
    return teams


def _tw_zones(war, field):
    result = []
    for guild in _messages(war, field):
        for conflict in _messages(guild, 2):
            zone = next(_messages(conflict, 1), None)
            if not zone or not _text(zone, 1):
                continue
            row = {"id": _text(zone, 1), "status": _number(zone, 2), "points": _number(zone, 3),
                   "capacity": _number(conflict, 2), "deployed": _number(conflict, 4),
                   "defeated": _number(conflict, 5)}
            if teams := _tw_teams(conflict):
                row["teams"] = teams
            result.append(row)
    return result


def _tw_profile(war, tag):
    # TerritoryWarGuildStatus.Profile → GuildProfile.Id / Name.
    guild = _single(war, tag, "гильдия ВГ")
    profile = _single(guild, 1, "профиль гильдии ВГ") if guild else None
    return {"id": _text(profile, 1), "name": _text(profile, 2)} if profile else {}


def parse_tw(payload):
    war = _single(ga.fields(payload), 3, "ВГ")
    if war is None:
        return None
    round_number, end = _number(war, 3), _number(war, 4)
    if round_number is None or not end:
        raise ga.AccountError("Ответ ВГ не содержит раунд или время его окончания.")
    return {
        "event_id": _text(war, 1), "map": _text(war, 2), "round": round_number, "end": end,
        "own": _tw_zones(war, 6), "opponent": _tw_zones(war, 7),
        "home_guild": _tw_profile(war, 6), "opponent_guild": _tw_profile(war, 7),
    }


def parse_raid(payload):
    root = ga.fields(payload)
    active = []
    for raid in _messages(root, 1):
        active.append({
            "id": _text(raid, 1), "end": _number(raid, 2), "points": _number(raid, 28),
            "players": [{"id": _text(p, 1), "points": _number(p, 2), "attempts": _number(p, 4)}
                        for p in _messages(raid, 23) if _text(p, 1)],
        })
    return {"active": active, "results": sum(1 for _ in _messages(root, 2))}


def epoch(value):
    return value / 1000 if value >= 100_000_000_000 else value


def tw_preview(plan, assignments, code, owned, zones, capacity):
    """Check the plan against the player's known roster. Zone IDs are verified separately."""
    mine = [entry for entry in assignments if str(entry["ally_code"]) == str(code)]
    uses = Counter(unit for entry in mine for unit in entry["units"] if unit)
    datacron_uses = Counter(
        identifier for entry in mine
        for identifier in (entry.get("datacron_id") or "").split(",") if identifier
    )
    by_zone = Counter(entry["zone"] for entry in assignments)
    excluded = {str(player) for player in plan.get("excluded", ())}
    result = []
    for entry in mine:
        units = [unit for unit in entry["units"] if unit]
        datacron_ids = [identifier for identifier in (entry.get("datacron_id") or "").split(",") if identifier]
        issues = []
        if not units:
            issues.append("В плане нет состава.")
        if any(uses[unit] > 1 for unit in units):
            issues.append("Юнит повторяется в назначениях.")
        if owned is not None and set(units) - owned:
            issues.append("Часть состава отсутствует в игровом ростере.")
        if any(datacron_uses[identifier] > 1 for identifier in datacron_ids):
            issues.append("Один датакрон назначен несколько раз.")
        if str(code) in excluded:
            issues.append("Игрок исключён из расстановки плана.")
        zone = zones.get(entry["zone"])
        if not zone:
            issues.append("Зона отсутствует в текущей карте плана.")
        elif by_zone[entry["zone"]] > capacity(plan, entry["zone"]):
            issues.append("Число назначений превышает вместимость зоны в плане.")
        result.append({"zone": zone["label"] if zone else entry["zone"],
                       "squad": entry["squad_name"], "units": units,
                       "datacron_ids": datacron_ids, "issues": issues})
    return result

def snapshot(guild_id, owner, kind):
    raw = database.get_bot_state(f"live_game:{kind}:{owner}", guild_id=guild_id)
    return json.loads(raw) if raw else {}


def save(guild_id, owner, kind, captured, data):
    database.set_bot_state(
        f"live_game:{kind}:{owner}", json.dumps({"at": captured, "data": data}), guild_id=guild_id,
    )
    if kind == "tw" and data and data.get("opponent_guild"):
        guild_compare.save_current_opponent(
            guild_id, data["home_guild"], data["opponent_guild"], data["event_id"], epoch(data["end"]),
        )
