"""Side colours from the shared game catalogue, never from character names."""
import json
import database

COLORS = {"light": (32, 78, 135), "dark": (120, 30, 30), "neutral": (210, 212, 218)}


def side(base_id):
    raw = database.get_bot_state("unit_alignments", guild_id=0)
    data = json.loads(raw) if raw else {}
    value = data.get(base_id)
    if value is not None:
        return {1: "neutral", 2: "light", 3: "dark"}.get(value)
    from services.faction import factions
    for key, label in (("alignment_light", "light"), ("alignment_dark", "dark"), ("alignment_neutral", "neutral")):
        if base_id in factions().get(key, {}).get("units", []):
            return label
    return None


def color(base_id, default=(50, 54, 64)):
    return COLORS.get(side(base_id), default)
