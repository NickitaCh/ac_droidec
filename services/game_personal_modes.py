"""Read models for the player's own Grand Arena and Conquest."""

from collections import Counter, defaultdict
from datetime import datetime
from zoneinfo import ZoneInfo

import database
from services import game_account, game_read_decode
from services.game_event_live import epoch
from services.tb_live import _messages, _number, _text, _values


def _only(rows, tag):
    entries = list(_messages(rows, tag))
    if len(entries) > 1:
        raise game_account.AccountError("Игра вернула несколько записей о текущем режиме.")
    return entries[0] if entries else None


def _time(value):
    if not value:
        return None
    try:
        return datetime.fromtimestamp(epoch(value), ZoneInfo("Europe/Moscow")).strftime("%d.%m %H:%M МСК")
    except (OSError, OverflowError, ValueError):
        return None


def _arena_zones(participant, *, battle=False):
    if participant is None:
        return []
    zones = []
    for zone in _messages(participant, 3):
        status = _only(zone, 1)
        teams = []
        for record in _messages(zone, 3):
            squad = _only(record, 4)
            units = []
            if squad is not None:
                for cell in _messages(squad, 1):
                    base = _text(cell, 2).partition(":")[0]
                    if base:
                        units.append(base)
            teams.append({"units": units, "defends": _number(record, 8), "state": _number(record, 6)})
        count = _number(zone, 4)
        row = {"id": _text(status, 1) if status else "", "status": _number(status, 2) if status else None,
                      "capacity": _number(zone, 2), "placed": max(count or 0, len(teams)),
                      "defeated": _number(zone, 5), "teams": teams}
        if battle:
            # The same WarConflict counters as TW: field 4 is survivors in combat.
            row["remaining"] = count
            states = [team["state"] for team in teams]
            if (row["defeated"] is None and states and all(s in (1, 2, 3) for s in states)
                    and sum(s in (1, 2) for s in states) == count):
                row["defeated"] = states.count(3)
            row["placed"] = count + row["defeated"] if count is not None and row["defeated"] is not None else None
        zones.append(row)
    return zones


def name_arena_units(state):
    zones = [zone for match in state["matches"] for side in ("home", "away") for zone in match[side]]
    ids = {unit for zone in zones for team in zone["teams"] for unit in team["units"]}
    names = database.get_game_unit_names(sorted(ids)) if ids else {}
    for zone in zones:
        zone["summary"] = arena_zone_summary(zone)
        for team in zone["teams"]:
            team["names"] = [names.get(unit) or unit for unit in team["units"]]
            team["battle_status"] = arena_team_status(team)


def arena_team_status(team):
    """WarSquad status, including the attempt counted while locked or defeated."""
    state = team.get("state")
    holds = max(0, (team.get("defends") or 0) - (state in (2, 3)))
    label = {1: "В защите", 2: "В бою", 3: "Побеждён"}.get(state, "Статус неизвестен")
    if holds:
        label += f" · удержаний: {holds}"
    return {"state": state, "label": label, "holds": holds}


def arena_zone_summary(zone):
    """Do not infer individual defeats from the zone's aggregate counter."""
    teams = zone.get("teams", [])
    defeated = zone.get("defeated")
    placed = zone.get("placed")
    complete = bool(teams) and len(teams) == placed and all(t.get("state") in (1, 2, 3) for t in teams)
    if defeated is None and complete:
        defeated = sum(t["state"] == 3 for t in teams)
    remaining = zone.get("remaining")
    if "remaining" not in zone and placed is not None and defeated is not None:
        remaining = max(0, placed - defeated)
    return {"remaining": remaining, "defeated": defeated, "capacity": zone.get("capacity")}


def arena(payload):
    event = _only(game_account.fields(payload), 4)
    if event is None or not _text(event, 1):
        return None
    match_id = _number(event, 3)
    matches = []
    for match in _messages(event, 2):
        opponent = _only(match, 16)
        home = _only(match, 2)
        round_number = _number(match, 5)
        battle = round_number is not None and round_number != 0
        home_zones = _arena_zones(home, battle=battle)
        away_zones = _arena_zones(_only(match, 3), battle=battle)
        capacity = [zone["capacity"] for zone in home_zones]
        index = _number(match, 8)
        matches.append({
            "map": _text(match, 1),
            "number": index, "round": _number(match, 5), "round_end": _time(_number(match, 6)),
            "start": _time(_number(match, 15)), "current": bool(match_id and index == match_id),
            "opponent": _text(opponent, 2) if opponent is not None else "",
            "defense": (sum(zone["placed"] for zone in home_zones), sum(capacity))
            if home_zones and None not in capacity and all(z["placed"] is not None for z in home_zones) else None,
            "home": home_zones, "away": away_zones,
        })
    league = _text(event, 11)
    leagues = {"KYBER": "Кайбер", "AURODIUM": "Ауродий", "CHROMIUM": "Хром",
               "BRONZIUM": "Бронзий", "CARBONITE": "Карбонит"}
    return {
        "joined": next(_values(event, 5, 0), 0) == 1,
        "rating": _number(event, 10), "league": leagues.get(league, league) or _number(event, 11),
        "division": _number(event, 12), "match_end": _time(_number(event, 4)),
        "matches": matches,
    }


def conquest(payload):
    rows = game_account.fields(payload)
    status = _only(rows, 1)
    if status is None or not _text(status, 6):
        return None
    energy = next(_values(rows, 2), None)
    sectors = defaultdict(Counter)
    for node in _messages(status, 11):
        sector, progress = _text(node, 2), _number(node, 1)
        if sector and progress is not None:
            sectors[sector][progress] += 1
    stamina = [
        {"unit": _text(unit, 1).partition(":")[0], "value": _number(unit, 2)}
        for unit in _messages(status, 14) if _text(unit, 1) and _number(unit, 2) is not None
    ]
    return {
        "difficulty": _number(status, 7), "pass_active": _number(status, 19) == 1,
        "energy": dict(game_read_decode.decode(energy, "energy")) if energy else {},
        "equipped_disks": sum(1 for _ in _values(status, 9)),
        "available_disks": sum(1 for _ in _values(status, 10)),
        "sectors": [{"id": sector, "nodes": sum(counts.values()),
                     "completed": sum(count for progress, count in counts.items() if progress > 0)}
                    for sector, counts in sorted(sectors.items())],
        "stamina": sorted(stamina, key=lambda row: (row["value"], row["unit"])),
    }
