"""Side colours from the shared game catalogue, never from character names."""
import json
import database

COLORS = {"light": (32, 78, 135), "dark": (120, 30, 30), "neutral": (236, 236, 240)}


def sides(base_ids):
    raw = database.get_bot_state("unit_alignments", guild_id=0)
    data = json.loads(raw) if raw else {}
    labels = {1: "neutral", 2: "light", 3: "dark"}
    result = {bid: labels[data[bid]] for bid in base_ids if data.get(bid) in labels}
    missing = set(base_ids) - result.keys()
    if not missing:
        return result
    from services.faction import factions
    groups = factions()
    for label in labels.values():
        for bid in missing.intersection(groups.get(f"alignment_{label}", {}).get("units", [])):
            result[bid] = label
    return result


def side(base_id):
    return sides([base_id]).get(base_id)


def color(base_id, default=(50, 54, 64)):
    return COLORS.get(side(base_id), default)
