"""Спецмиссии (ОЗ) «Восхода Империи»: кто в гильдии проходит их по требованиям игры —
/тб оз_готовность и раздел на веб-странице /tb/projection.

Требования — из игровых данных, без входа в аккаунт (проверено 2026-10-21):
territoryBattleDefinition t05D → covertZoneDefinition[] (zoneId + campaignElementIdentifier)
→ миссия кампании t05D → entryCategoryAllowed: mandatoryRosterUnit (обязательные), categoryId
(юнит подходит, если у него есть любая из категорий), minimumRequiredUnitQuantity (сколько
юнитов), minimumUnitRarity, minimumRelicTier (сырой тир: 9 = R7). Ветка в zoneId:
conflict01 — светлая, 02 — тёмная, 03 — смешанная (сверено по составам: Сери Джанда —
Бракка, Меррин — Датомир, Кира и Хан — Кореллия). bonusZoneDefinition говорит, какая
зона открывает бонусную планету: Бракка — Зеффо, Татуин — Мандалор. Награда за одно
прохождение — covertZoneDefinition[].victoryReward.

Решение пользователя 2026-10-21: если у миссии есть обязательные персонажи, отслеживаем
только их — добрать остальной отряд («подходят 3 из 5») не считается требованием.
Число подходящих юнитов проверяем лишь у миссий без обязательных (Зеффо)."""

import json
import re

import database
import tb_plan_reader
from services import unit_forecast
from services.tb_projection import BRANCHES

STATE_KEY = "tb_covert_missions"  # bot_state guild 0: [mission, ...] — пишет units_sync
TB_ID = "t05D"
GUILD_DATA_FLAG = 536870912   # territoryBattleDefinition (swgoh_comlink DataItems.GUILD)
CAMPAIGN_FLAG = 274877906944
_ZONE_RE = re.compile(r"phase0?(\d)_conflict0?(\d)(_bonus)?_covert0?(\d)")
_SIDE_BY_CONFLICT = {1: "light", 2: "dark", 3: "mixed"}
_BONUS_PLANET = {"light": "Zeffo", "mixed": "Mandalore"}
ANY_UNIT_LIMIT = 200  # «любой персонаж» (все стороны Силы) — не храним список юнитов
# Валюты наград (локализация SHARED_CURRENCY_*_TERRITORY_BATTLE*).
CURRENCY_NAMES = {
    "TERRITORY_BATTLE_CURRENCY_01": "Mk I",
    "TERRITORY_BATTLE_CURRENCY_02": "Mk II",
    "TERRITORY_BATTLE_CURRENCY_03": "Mk III",
}


def parse(tb_defs: list, campaigns: list, units: list) -> list[dict]:
    """[{zone_id, phase, planet, side, bonus, mandatory, units|None, count, min_relic,
    min_stars, unlocks, reward: [(id, кол-во)]}] — units None значит «любой персонаж»."""
    tb = next((t for t in tb_defs if t.get("id") == TB_ID), None)
    camp = next((c for c in campaigns if c.get("id") == TB_ID), None)
    if not tb or not camp:
        return []
    missions_by_id = {}
    for cmap in camp.get("campaignMap") or []:
        for group in cmap.get("campaignNodeDifficultyGroup") or []:
            for node in group.get("campaignNode") or []:
                for mission in node.get("campaignNodeMission") or []:
                    missions_by_id[mission.get("id")] = mission
    by_category: dict[str, set] = {}
    for u in units:
        if u.get("combatType") != 1:
            continue
        for cat in u.get("categoryId") or []:
            by_category.setdefault(cat, set()).add(u["baseId"])
    # Зона спецмиссии, рядом с которой лежит бонусная зона, открывает бонусную планету.
    unlock_conflicts = {(b.get("zoneDefinition") or {}).get("linkedConflictId") for b in tb.get("bonusZoneDefinition") or []}

    # В одной зоне бывает две спецмиссии (Татуин: инквизиторы и мандалорцы), бонусную
    # планету открывает та, у которой миссия кампании названа SPECIALMISSION (у второй —
    # COMBATMISSION_02); мандалорцы → Мандалор, Сери Джанда → Зеффо, как в игре.
    special_conflicts = {}
    for zone in tb.get("covertZoneDefinition") or []:
        conflict_id = (zone.get("zoneDefinition") or {}).get("linkedConflictId")
        mission_id = (zone.get("campaignElementIdentifier") or {}).get("campaignMissionId") or ""
        if "SPECIALMISSION" in mission_id:
            special_conflicts.setdefault(conflict_id, mission_id)

    result = []
    for zone in tb.get("covertZoneDefinition") or []:
        zone_def = zone.get("zoneDefinition") or {}
        zone_id = zone_def.get("zoneId") or ""
        m = _ZONE_RE.search(zone_id)
        mission = missions_by_id.get((zone.get("campaignElementIdentifier") or {}).get("campaignMissionId"))
        if not m or not mission:
            continue
        phase, conflict, bonus = int(m.group(1)), int(m.group(2)), bool(m.group(3))
        side = _SIDE_BY_CONFLICT.get(conflict)
        if side is None:
            continue
        planet = _BONUS_PLANET.get(side) if bonus else BRANCHES[side][phase - 1]
        entry = mission.get("entryCategoryAllowed") or {}
        mandatory = [r["id"] for r in entry.get("mandatoryRosterUnit") or [] if r.get("id")]
        pool = set(mandatory)
        for cat in entry.get("categoryId") or []:
            pool |= by_category.get(cat, set())
        relic_tier = entry.get("minimumRelicTier") or 0
        result.append({
            "zone_id": zone_id, "phase": phase, "planet": planet, "side": side, "bonus": bonus,
            "mandatory": mandatory,
            "units": sorted(pool) if len(pool) < ANY_UNIT_LIMIT else None,
            "count": entry.get("minimumRequiredUnitQuantity") or len(mandatory) or 1,
            "min_relic": max(0, relic_tier - 2) if relic_tier else None,
            "min_stars": min(entry.get("minimumUnitRarity") or 7, 7),
            "unlocks": _BONUS_PLANET.get(side) if (
                not bonus and zone_def.get("linkedConflictId") in unlock_conflicts
                and special_conflicts.get(zone_def.get("linkedConflictId")) == (zone.get("campaignElementIdentifier") or {}).get("campaignMissionId")
            ) else None,
            "reward": [(r["id"], r.get("minQuantity") or 1) for r in zone.get("victoryReward") or [] if r.get("id")],
        })
    result.sort(key=lambda x: (x["phase"], x["bonus"], ["dark", "mixed", "light"].index(x["side"]), x["zone_id"]))
    return result


def planet_ru(planet: str) -> str:
    return (tb_plan_reader.PLANET_NAME_TO_RU.get(planet) or planet).capitalize()


def missions(phase: int | None = None, planet: str | None = None) -> list[dict]:
    raw = database.get_bot_state(STATE_KEY, guild_id=0)
    data = json.loads(raw) if raw else []
    return [m for m in data if (phase is None or m["phase"] == phase) and (planet is None or m["planet"] == planet)]


def planets() -> list[tuple[str, str, int]]:
    """[(planet, русское название, этап)] планет со спецмиссиями — в порядке этапов."""
    out = []
    for m in missions():
        if all(p != m["planet"] for p, _ru, _ph in out):
            out.append((m["planet"], planet_ru(m["planet"]), m["phase"]))
    return out


def resolve_planet(text: str) -> str | None:
    """Планета из подсказки (английское название) или вписанная руками по-русски."""
    needle = (text or "").strip().lower()
    for planet, ru, _phase in planets():
        if needle in (planet.lower(), ru.lower()):
            return planet
    hits = [planet for planet, ru, _phase in planets() if needle and (needle in ru.lower() or needle in planet.lower())]
    return hits[0] if len(hits) == 1 else None


def reward_text(m: dict, short: bool = False) -> str:
    """«50 жетонов событий гильдии Mk II» / short: «🪙 50 Mk II», «🧩 Третья сестра ×1»;
    пусто — неизвестно (миссии из bot_state, записанные до 2026-10-21, награды не хранят)."""
    parts = []
    for item_id, qty in m.get("reward") or []:
        if item_id.startswith("unitshard_"):
            bid = item_id[len("unitshard_"):]
            name = database.get_game_unit_names([bid]).get(bid, bid)
            parts.append(f"🧩 {name} ×{qty}" if short else f"осколки: {name} ×{qty}")
        else:
            mk = CURRENCY_NAMES.get(item_id, item_id)
            parts.append(f"🪙 {qty} {mk}" if short else f"{qty} жетонов событий гильдии {mk}")
    return ", ".join(parts)


def mission_label(m: dict) -> str:
    """Порог над колонкой: «R7» — у миссии с обязательными, «R7 ×5» — без них."""
    relic = f"R{m['min_relic']}" if m["min_relic"] is not None else f"{m['min_stars']}★"
    return relic if m["mandatory"] else f"{relic} ×{m['count']}"


def _unit_ok(unit: dict, mission: dict) -> bool:
    lv = unit_forecast.current_level(unit)
    if lv["stars"] < mission["min_stars"]:
        return False
    return mission["min_relic"] is None or (lv["relic"] is not None and lv["relic"] >= mission["min_relic"])


def player_status(mission: dict, roster: dict, ships: set = frozenset()) -> dict:
    """{status: ready|short|missing, have, text} для одного игрока. С обязательными: ready —
    все они на пороге, short — есть, но не дотягивают (text — уровень отстающего), missing —
    кого-то нет. Без обязательных — по числу подходящих юнитов: short — юнитов хватает, но не
    дотягивают, missing — юнитов из пула меньше, чем нужно."""
    if mission["mandatory"]:
        failing = [b for b in mission["mandatory"] if b not in roster or not _unit_ok(roster[b], mission)]
        have = len(mission["mandatory"]) - len(failing)
        if not failing:
            return {"status": "ready", "have": have, "text": "✓"}
        if any(b not in roster for b in failing):
            return {"status": "missing", "have": have, "text": "—"}
        return {"status": "short", "have": have, "text": _level(roster[failing[0]])}
    pool = mission["units"]
    candidates = [b for b in (pool if pool is not None else roster) if b in roster]
    if pool is None:  # «любой персонаж» — корабли в спецмиссию не берутся
        candidates = [b for b in candidates if b not in ships]
    ok = {b for b in candidates if _unit_ok(roster[b], mission)}
    need = mission["count"]
    have = len(ok)
    if have >= need:
        return {"status": "ready", "have": have, "text": "✓"}
    return {"status": "short" if len(candidates) >= need else "missing", "have": have, "text": f"{have}/{need}"}


def _level(unit: dict) -> str:
    lv = unit_forecast.current_level(unit)
    core = f"R{lv['relic']}" if lv["relic"] is not None else f"G{lv['gear']}"
    return core if lv["stars"] >= 7 else f"{lv['stars']}★ {core}"


def player_report(ms: list[dict], roster: dict) -> list[dict]:
    """Разбор одного игрока по спецмиссиям: [{mission, status, have, need, mandatory:
    [(имя, уровень|None, ok)], best: [(имя, уровень)] — лучшие из недотянувших, кого
    докачать, ok_names}]."""
    ships = {b for b, t in database.get_unit_types(list(roster)).items() if t == "ship"}
    pool_ids = {b for m in ms for b in (m["units"] or [])} | {b for m in ms for b in m["mandatory"]}
    names = database.get_game_unit_names(list(pool_ids | set(roster)))
    out = []
    for m in ms:
        s = player_status(m, roster, ships)
        pool = m["units"] if m["units"] is not None else [b for b in roster if b not in ships]
        owned = [b for b in pool if b in roster]
        ok = [b for b in owned if _unit_ok(roster[b], m)]
        mandatory = [(names.get(b, b), _level(roster[b]) if b in roster else None, b in ok) for b in m["mandatory"]]
        # Добор отряда считаем только у миссий без обязательных (см. докстринг модуля).
        missing_slots = 0 if m["mandatory"] else max(0, m["count"] - len(ok))

        def strength(b):
            lv = unit_forecast.current_level(roster[b])
            return (lv["stars"], lv["relic"] if lv["relic"] is not None else -1, lv["gear"])
        under = sorted((b for b in owned if b not in ok and b not in m["mandatory"]), key=strength, reverse=True)
        out.append({
            "mission": m, "status": s["status"], "have": min(s["have"], m["count"]), "need": m["count"],
            "mandatory": mandatory,
            "best": [(names.get(b, b), _level(roster[b])) for b in under[:missing_slots]],
            "missing_slots": missing_slots,
            "ok_names": [names.get(b, b) for b in ok if b not in m["mandatory"]],
        })
    return out


def guild_grid(guild_id: int, phase: int | None = None, planet: str | None = None) -> dict:
    """Структура для services/guild_units_image.render: units — спецмиссии (портрет —
    первый обязательный персонаж), players — по убыванию числа пройденных."""
    ms = missions(phase, planet)
    members = [(code, name) for _d, code, name in database.get_all_user_mappings(guild_id)]
    rosters = database.get_player_units_bulk([c for c, _ in members])
    names = database.get_game_unit_names([b for m in ms for b in m["mandatory"]])
    all_ids = {b for r in rosters.values() for b in r}
    ships = {b for b, t in database.get_unit_types(list(all_ids)).items() if t == "ship"}
    units = []
    for m in ms:
        # Без обязательных (Зеффо: любые светлые клоны) — портрет первого подходящего.
        lead = m["mandatory"][0] if m["mandatory"] else (m["units"] or [None])[0]
        units.append({"base_id": lead, "name": " + ".join(names.get(b, b) for b in m["mandatory"]) or "любые подходящие",
                      "label": mission_label(m)})
    players = []
    for code, name in members:
        roster = rosters.get(code)
        if not roster:
            continue
        cells, done = [], 0
        for m in ms:
            s = player_status(m, roster, ships)
            done += s["status"] == "ready"
            cells.append({"text": s["text"], "met": s["status"] == "ready", "status": s["status"]})
        players.append({"name": name or code, "ally_code": code, "pct": done / len(ms) * 100 if ms else 0,
                        "ready": bool(ms) and done == len(ms), "guide_level": None, "cells": cells, "done": done})
    players.sort(key=lambda p: (-p["done"], p["name"].lower()))
    groups, last = [], None
    for m in ms:
        label = f"{m['phase']} · {planet_ru(m['planet'])}"
        if groups and last == label:
            groups[-1] = (label, groups[-1][1] + 1)
        else:
            groups.append((label, 1))
        last = label
    ready_counts = [sum(1 for p in players if p["cells"][i]["met"]) for i in range(len(ms))]
    return {"guide": None, "units": units, "players": players, "groups": groups, "missions": ms,
            "ready_counts": ready_counts}
