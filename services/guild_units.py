"""Юнит по всей гильдии и готовность гильдии к цели «Руководства» — по кэшу ежечасного
синка ростеров (player_unit_cache), без запросов в игру.

- unit_report — у кого есть юнит и на какой прокачке (как C3PO `g unit`): звёзды, гир/
  реликвия, ГМ, зеты и омикроны, ульта; у кого нет.
- readiness — готовность к цели «Руководства» (как WookieeBot `guild ready journey`):
  по каждому игроку уровень каждого требуемого юнита и общий процент.

Логика без Discord: команды — cogs/guild_units.py, картинка готовности —
services/guild_units_image.py."""

import copy

import database
from services import journey_guide, unit_forecast
from services.player_profile import _skill_counts


def unit_base_id(unit: dict) -> str:
    return unit.get("baseId") or (unit.get("definitionId") or "").split(":")[0]


def _members(guild_id: int) -> list[tuple[str, str]]:
    """[(ally_code, имя)] текущего состава."""
    return [(code, name) for code, name in database.get_roster_by_player_id(guild_id).values() if code]


def level_of(unit: dict) -> dict:
    lv = unit_forecast.current_level(unit)
    return {"stars": lv["stars"], "gear": lv["gear"], "relic": lv["relic"]}


def level_text(level: dict, is_ship: bool = False, with_stars: bool = False) -> str:
    if is_ship:
        return f"{level['stars']}★"
    core = f"R{level['relic']}" if level["relic"] is not None else f"G{level['gear']}"
    if with_stars or level["stars"] < 7:
        return f"{level['stars']}★ {core}"
    return core


def unit_report(guild_id: int, base_id: str, stat_calc) -> dict:
    """{name, is_ship, members, owners: [{name, ally_code, stars, gear, relic, gp, zetas,
    omicrons, ultimate}], missing: [имя]} — владельцы по убыванию прокачки и ГМ."""
    members = _members(guild_id)
    units = database.get_player_units_bulk([c for c, _ in members])
    is_ship = database.get_unit_types([base_id]).get(base_id) == "ship"
    skills = database.get_all_skill_tier_info()
    owners, missing = [], []
    for code, name in members:
        roster = units.get(code) or {}
        unit = roster.get(base_id)
        if unit is None:
            missing.append(name or code)
            continue
        try:
            if is_ship:
                crew = unit_forecast.ship_crew(stat_calc, list(roster.values()), base_id)
                gp = unit_forecast._ship_snapshot(stat_calc, unit, crew)["gp"]
            else:
                gp = stat_calc.calc_char_gp(copy.deepcopy(unit))
        except Exception:
            gp = None
        zetas, omicrons = _skill_counts(unit, skills)
        owners.append({"name": name or code, "ally_code": code, **level_of(unit), "gp": gp,
                       "zetas": zetas, "omicrons": len(omicrons), "ultimate": bool(unit.get("purchasedAbilityId"))})
    owners.sort(key=lambda o: (-o["stars"], -(o["relic"] if o["relic"] is not None else -1), -o["gear"], -(o["gp"] or 0)))
    missing.sort(key=str.lower)
    return {"name": database.get_game_unit_name(base_id) or base_id, "is_ship": is_ship,
            "members": len(members), "owners": owners, "missing": missing}


# ---------------------------------------------------------------- готовность к «Руководству»

def _fraction(unit: dict | None, target_type: str, target_value: str) -> float:
    """Насколько юнит дошёл до цели, 0..1. Реликвия: гир + реликвия против 13 + N, звёзды
    ниже 7 — пропорционально (реликвия открывается только на 7★)."""
    if unit is None:
        return 0.0
    if journey_guide.unit_meets(unit, target_type, target_value):
        return 1.0
    target = int(target_value)
    stars = unit.get("currentRarity", 0)
    gear = unit.get("currentTier", 0)
    relic = int(journey_guide.unit_progress(unit, "relic") or 0) if gear >= 13 else 0
    if target_type == "stars":
        return min(1.0, stars / target) if target else 1.0
    if target_type == "gear":
        return min(1.0, gear / target) if target else 1.0
    if target_type == "relic":
        return min(1.0, (gear + relic) / (13 + target)) * min(1.0, stars / 7)
    return 0.0


def readiness(guild_id: int, guide_base_id: str) -> dict:
    """Готовность гильдии к цели «Руководства» — см. readiness_grid."""
    grouped = journey_guide.group_by_unit(database.get_journey_guide_requirements(guide_base_id, guild_id))
    return readiness_grid(guild_id, grouped, guide_base_id)


def readiness_grid(guild_id: int, grouped: dict, guide_base_id: str | None = None) -> dict:
    """{guide: {base_id, name} | None, units: [{base_id, name, label}], players: [{name, pct,
    ready, guide_level, cells: [{text, met}]}]} — игроки по убыванию готовности. grouped —
    {base_id: [(target_type, value), ...]}; без guide_base_id — без колонки «Цель» (рейд)."""
    unit_ids = list(grouped)
    extra = [guide_base_id] if guide_base_id else []
    names = database.get_game_unit_names(unit_ids + extra)
    types = database.get_unit_types(unit_ids + extra)
    members = _members(guild_id)
    rosters = database.get_player_units_bulk([c for c, _ in members])

    units = []
    for unit_id, targets in grouped.items():
        label = " ".join(journey_guide.short_label(t, v) for t, v in targets)
        units.append({"base_id": unit_id, "name": names.get(unit_id, unit_id), "label": label})

    players = []
    for code, name in members:
        roster = rosters.get(code)
        if not roster:
            continue
        cells, fractions = [], []
        for unit_id, targets in grouped.items():
            unit = roster.get(unit_id)
            met = unit is not None and all(journey_guide.unit_meets(unit, t, v) for t, v in targets)
            fractions.append(min(_fraction(unit, t, v) for t, v in targets))
            text = "—" if unit is None else level_text(level_of(unit), types.get(unit_id) == "ship")
            cells.append({"text": text, "met": met})
        guide_unit = roster.get(guide_base_id) if guide_base_id else None
        guide_level = level_text(level_of(guide_unit), types.get(guide_base_id) == "ship") if guide_unit else None
        pct = sum(fractions) / len(fractions) * 100 if fractions else 0
        players.append({"name": name or code, "ally_code": code, "pct": pct,
                        "ready": all(c["met"] for c in cells), "guide_level": guide_level, "cells": cells})
    players.sort(key=lambda p: (-int(p["pct"] + 1e-9), p["guide_level"] is None, p["name"].lower()))
    guide = ({"base_id": guide_base_id, "name": names.get(guide_base_id, guide_base_id),
              "is_ship": types.get(guide_base_id) == "ship"} if guide_base_id else None)
    return {"guide": guide, "units": units, "players": players}


def player_readiness(guild_id: int, guide_base_id: str, roster: dict) -> dict:
    """Готовность одного игрока к цели «Руководства» (как C3PO `panic`): по каждому требуемому
    юниту — текущая прокачка и статус: met (выполнено), short (звёзды есть, не хватает
    реликвии/гира) или missing (юнита нет или не хватает звёзд). roster — {base_id: rosterUnit}."""
    grouped = journey_guide.group_by_unit(database.get_journey_guide_requirements(guide_base_id, guild_id))
    names = database.get_game_unit_names(list(grouped) + [guide_base_id])
    types = database.get_unit_types(list(grouped) + [guide_base_id])
    units, fractions = [], []
    for unit_id, targets in grouped.items():
        unit = roster.get(unit_id)
        is_ship = types.get(unit_id) == "ship"
        need_stars = max([int(v) for t, v in targets if t == "stars"] + [7 if any(t == "relic" for t, _ in targets) else 0])
        if unit is not None and all(journey_guide.unit_meets(unit, t, v) for t, v in targets):
            status = "met"
        elif unit is None or (unit.get("currentRarity") or 0) < need_stars:
            status = "missing"
        else:
            status = "short"
        fractions.append(min(_fraction(unit, t, v) for t, v in targets))
        level = level_of(unit) if unit is not None else None
        units.append({
            "base_id": unit_id, "name": names.get(unit_id, unit_id), "is_ship": is_ship,
            "label": " ".join(journey_guide.short_label(t, v) for t, v in sorted(targets, key=lambda tv: tv[0] != "stars")),
            "status": status, "level": level,
        })
    guide_unit = roster.get(guide_base_id)
    return {
        "guide": {"base_id": guide_base_id, "name": names.get(guide_base_id, guide_base_id)},
        "guide_level": level_of(guide_unit) if guide_unit is not None else None,
        "units": units,
        "pct": sum(fractions) / len(fractions) * 100 if fractions else 0,
        "ready": all(u["status"] == "met" for u in units),
    }


def guide_matrix(guild_id: int, kind: str = "gl") -> dict:
    """Матрица «игрок × цель Руководства» одного вида (как HotUtils GL progress):
    {guides: [{base_id, name, owners, ready}], players: [{name, ally_code, owned, cells}]},
    ячейка — {owned, ultimate, level, pct, met, total, lagging: [(имя, сейчас, нужно)]}.
    Ростеры читаются один раз на все цели."""
    guides = [g for g in journey_guide.guide_options() if g["kind"] == kind]
    grouped = {g["base_id"]: journey_guide.group_by_unit(database.get_journey_guide_requirements(g["base_id"], guild_id))
               for g in guides}
    all_ids = {u for units in grouped.values() for u in units} | set(grouped)
    names = database.get_game_unit_names(list(all_ids))
    types = database.get_unit_types(list(all_ids))
    members = _members(guild_id)
    rosters = database.get_player_units_bulk([c for c, _ in members])

    players = []
    for code, name in members:
        roster = rosters.get(code)
        if not roster:
            continue
        cells = []
        for g in guides:
            guide_unit = roster.get(g["base_id"])
            if guide_unit is not None:
                cells.append({"owned": True, "ultimate": bool(guide_unit.get("purchasedAbilityId")),
                              "level": level_text(level_of(guide_unit), types.get(g["base_id"]) == "ship"), "pct": 100})
                continue
            fractions, met, lagging = [], 0, []
            for unit_id, targets in grouped[g["base_id"]].items():
                unit = roster.get(unit_id)
                fractions.append(min(_fraction(unit, t, v) for t, v in targets))
                if unit is not None and all(journey_guide.unit_meets(unit, t, v) for t, v in targets):
                    met += 1
                    continue
                now = "нет" if unit is None else level_text(level_of(unit), types.get(unit_id) == "ship")
                need = " ".join(journey_guide.short_label(t, v) for t, v in targets)
                lagging.append((names.get(unit_id, unit_id), now, need, fractions[-1]))
            lagging.sort(key=lambda x: x[3])
            cells.append({"owned": False, "pct": sum(fractions) / len(fractions) * 100 if fractions else 0,
                          "met": met, "total": len(fractions), "lagging": [x[:3] for x in lagging]})
        players.append({"name": name or code, "ally_code": code, "cells": cells,
                        "owned": sum(1 for c in cells if c["owned"])})
    players.sort(key=lambda p: (-p["owned"], -sum(c["pct"] for c in p["cells"]), p["name"].lower()))
    for i, g in enumerate(guides):
        g["name"] = names.get(g["base_id"], g["name"])
        g["owners"] = sum(1 for p in players if p["cells"][i]["owned"])
        g["ready"] = sum(1 for p in players if not p["cells"][i]["owned"] and p["cells"][i]["met"] == p["cells"][i]["total"])
    return {"guides": guides, "players": players, "kinds": journey_guide.KIND_LABELS}
