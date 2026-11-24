"""Compact Grand Arena scoring definitions, copied from Comlink's Guild data.

The bundled catalog is a game-data snapshot, not a hand-maintained score table.
The regular unit sync replaces it in bot_state when new seasons are released.
"""
import json
from pathlib import Path

import database

STATE_KEY = "arena_scoring_definitions"
_SNAPSHOT = Path(__file__).resolve().parent.parent / "data" / "arena_rules.json"


def compile_catalog(data):
    result = {"maps": {}, "rules": {}}
    groups = {}
    for definition in data.get("territoryTournamentDefinition", ()):
        kinds = {zone["zoneDefinition"]["zoneId"]: zone["combatType"]
                 for zone in definition.get("conflictZoneDefinition", ())}
        zones = []
        for zone in definition.get("duelZoneDefinition", ()):
            entry, raw = zone["entryCategoryAllowed"], zone["pointStructure"]
            points = {key: int(value["value"]) for key, value in raw.items()
                      if isinstance(value, dict) and value.get("type") == 2}
            points["attempts"] = {str(item["defendNumber"]): int(item["reward"]["value"])
                                  for item in raw.get("defendDefeatBonus", ())}
            parent = zone["zoneDefinition"]["linkedConflictId"]
            zones.append({"id": zone["zoneDefinition"]["zoneId"], "parent": parent,
                          "kind": kinds.get(parent), "size": entry["maximumAllowedUnitQuantity"],
                          "minimum": entry["minimumRequiredUnitQuantity"], "points": points})
        if not zones:
            continue
        key = json.dumps(zones, sort_keys=True)
        if key not in groups:
            groups[key] = "r" + str(len(groups))
            result["rules"][groups[key]] = zones
        result["maps"][definition["id"]] = groups[key]
    return result


def update_catalog(data):
    catalog = compile_catalog(data)
    if catalog["maps"]:
        database.set_bot_state(STATE_KEY, json.dumps(catalog), guild_id=0)


def zones(map_id):
    raw = database.get_bot_state(STATE_KEY, guild_id=0)
    catalogs = [json.loads(raw)] if raw else []
    catalogs.append(json.loads(_SNAPSHOT.read_text(encoding="utf-8")))
    for catalog in catalogs:
        if key := catalog["maps"].get(map_id):
            return catalog["rules"][key]
    return []
