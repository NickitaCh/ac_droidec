"""/фракция — все персонажи одной фракции у игрока (как HotBot `/faction`). Фракции —
видимые категории игры (принадлежность, профессия, вид, ГЛ), их пишет units_sync в
bot_state FACTIONS_STATE. Картинка — services/faction_image.py."""

import json

import database
from services import guild_units, player_profile
from services.units_sync import FACTIONS_STATE


def factions() -> dict:
    """{category_id: {"name", "units": [base_id]}}; пусто — справочник ещё не синкался."""
    raw = database.get_bot_state(FACTIONS_STATE, guild_id=0)
    return json.loads(raw) if raw else {}


def search(text: str, limit: int = 25) -> list[tuple[str, str, int]]:
    """[(category_id, название, персонажей)] по подстроке названия или id, по алфавиту."""
    needle = (text or "").strip().lower()
    hits = [(cid, f["name"], len(f["units"])) for cid, f in factions().items()
            if needle in f["name"].lower() or needle in cid.lower()]
    return sorted(hits, key=lambda h: h[1].lower())[:limit]


def resolve(text: str) -> str | None:
    """id фракции из выбора в подсказке или вписанного руками названия."""
    data = factions()
    if text in data:
        return text
    needle = (text or "").strip().lower()
    exact = [cid for cid, f in data.items() if f["name"].lower() == needle]
    if exact:
        return exact[0]
    hits = search(text)
    return hits[0][0] if len(hits) == 1 else None


def build(faction_id: str, roster: dict) -> dict:
    """{name, units: [{base_id, name, owned, stars, gear, relic, zetas, omicrons, ultimate,
    gl}], owned, total, seven, relic5} — сначала ГЛ, дальше по реликвии/гиру/звёздам;
    кого нет — в конце по алфавиту. roster — {base_id: сырой rosterUnit}."""
    faction = factions()[faction_id]
    ids = faction["units"]
    names = database.get_game_unit_names(ids)
    skills = database.get_all_skill_tier_info()
    gls = set(player_profile.galactic_legends())
    owned, missing = [], []
    for base_id in ids:
        unit = roster.get(base_id)
        row = {"base_id": base_id, "name": names.get(base_id, base_id), "gl": base_id in gls}
        if unit is None:
            missing.append({**row, "owned": False})
            continue
        zetas, omicrons = player_profile._skill_counts(unit, skills)
        owned.append({**row, "owned": True, **guild_units.level_of(unit), "zetas": zetas,
                      "omicrons": len(omicrons), "ultimate": bool(unit.get("purchasedAbilityId"))})
    owned.sort(key=lambda u: (not u["gl"], -(u["relic"] if u["relic"] is not None else -1), -u["gear"],
                              -u["stars"], u["name"].lower()))
    missing.sort(key=lambda u: u["name"].lower())
    return {
        "name": faction["name"],
        "units": owned + missing,
        "owned": len(owned),
        "total": len(ids),
        "seven": sum(1 for u in owned if u["stars"] == 7),
        "relic5": sum(1 for u in owned if (u["relic"] or 0) >= 5),
    }
