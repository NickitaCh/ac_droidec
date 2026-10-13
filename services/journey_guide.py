"""Требования «Руководства» (Journey Guide) из Comlink — для постановки задач сразу на
всех «чайлдов» ГЛ / капшипа / путешествия (/задачи руководство, веб /tasks).

Где это лежит в игровых данных (проверено на живом Comlink 2026-10-13):
unitGuideDefinition[].additionalActivationRequirementId → requirement[].requirementItem[]
с type 105 → id челленджа в коллекции challenge (квест открытия «1 из 3» и т.п.) →
challenge.task[]: юнит в actionLinkDef.link (`base_id=CAPTAINREX`), уровень в descKey
(`GLEVENT_PREREQ_RELIC_07`, `GLEVENT_PREREQ_STAR_04`, `EVENT_PREREQ_GEAR_12`).
У старых событий (Малак, GAS, Ревансы, легендарки) квестов нет — их тут нет.

Логика проверки целей stars/relic/gear здесь же (unit_progress/unit_meets) — ей
пользуются и cogs/tasks.py, и web/routes/tasks.py."""

import asyncio
import re

import database

UNIT_GUIDE_DEFINITION_FLAG = 17179869184
REQUIREMENT_FLAG = 512
CHALLENGE_FLAG = 8388608
CHALLENGE_REQUIREMENT_TYPE = 105

KIND_LABELS = {"gl": "Легенды галактики", "capital": "Флагманы", "journey": "Путешествия"}

_DESC_RE = re.compile(r"PREREQ_(RELIC|STAR|GEAR)_(\d+)$")
_DESC_TYPES = {"RELIC": "relic", "STAR": "stars", "GEAR": "gear"}
_BASE_ID_RE = re.compile(r"base_id=([A-Z0-9_]+)")


def _guide_kind(entry: dict) -> str:
    if entry.get("galacticLegend"):
        return "gl"
    if entry["unitBaseId"].startswith("CAPITAL"):
        return "capital"
    return "journey"


def parse_guide_requirements(guides: list, requirements: list, challenges: list) -> list[tuple]:
    """Строки для database.replace_journey_guide_requirements. Юнит, встречающийся в
    нескольких квестах одной цели, берётся один раз — с максимальным уровнем."""
    req_by_id = {r["id"]: r for r in requirements}
    ch_by_id = {c["id"]: c for c in challenges}
    rows = []
    for entry in guides:
        req = req_by_id.get(entry.get("additionalActivationRequirementId") or "")
        if not req:
            continue
        units = {}
        for item in req.get("requirementItem", []):
            if item.get("type") != CHALLENGE_REQUIREMENT_TYPE:
                continue
            challenge = ch_by_id.get(item.get("id"))
            if not challenge:
                continue
            for task in challenge.get("task", []):
                link = (task.get("actionLinkDef") or {}).get("link", "")
                unit_m = _BASE_ID_RE.search(link)
                desc_m = _DESC_RE.search(task.get("descKey", ""))
                if not unit_m or not desc_m:
                    continue  # скрытые задания ещё не вышедшей цели
                target_type, value = _DESC_TYPES[desc_m.group(1)], int(desc_m.group(2))
                key = (unit_m.group(1), target_type)
                if key not in units or units[key] < value:
                    units[key] = value
        if not units:
            continue
        kind = _guide_kind(entry)
        guide_id = entry["unitBaseId"]
        if guide_id == "TBA":  # анонс ещё не вышедшей цели — юнита в справочнике нет
            guide_id = req["id"].replace("_JOURNEY_GUIDE_REQUIREMENT", "")
        seen_units = set()
        for order, ((unit_id, target_type), value) in enumerate(units.items()):
            if unit_id in seen_units:
                continue
            seen_units.add(unit_id)
            rows.append((guide_id, kind, order, unit_id, target_type, str(value)))
    return rows


async def sync_journey_guide(comlink) -> int:
    """Обновляет справочник. Возвращает число строк; при пустом разборе (изменился
    формат данных) старый справочник не трогает."""
    async def fetch(flag: int, key: str) -> list:
        data = await asyncio.to_thread(comlink.get_game_data, items=str(flag), include_pve_units=False)
        return data.get(key) or []

    guides = await fetch(UNIT_GUIDE_DEFINITION_FLAG, "unitGuideDefinition")
    requirements = await fetch(REQUIREMENT_FLAG, "requirement")
    challenges = await fetch(CHALLENGE_FLAG, "challenge")
    rows = parse_guide_requirements(guides, requirements, challenges)
    if rows:
        database.replace_journey_guide_requirements(rows)
    return len(rows)


def guide_options() -> list[dict]:
    """Цели «Руководства» для выбора: [{base_id, name, kind, kind_label, count}]."""
    guides = database.get_journey_guides()
    names = database.get_game_unit_names([g[0] for g in guides])
    order = list(KIND_LABELS)
    result = [
        {"base_id": base_id, "name": names.get(base_id) or f"{base_id} (анонс, часть требований скрыта)", "kind": kind,
         "kind_label": KIND_LABELS.get(kind, kind), "count": count}
        for base_id, kind, count in guides
    ]
    result.sort(key=lambda g: (order.index(g["kind"]) if g["kind"] in order else 99, g["name"].lower()))
    return result


# ---------------------------------------------------------------------------
# Проверка целей stars / relic / gear по записи юнита из get_player
# ---------------------------------------------------------------------------

def unit_progress(unit: dict, target_type: str) -> str:
    """Текущее значение в тех же единицах, что target_value (реликвия — как в игре)."""
    if target_type == "stars":
        return str(unit.get("currentRarity", 0))
    if target_type == "relic":
        # Comlink хранит currentTier реликвии со смещением +2 (R7 = 9).
        raw_tier = (unit.get("relic") or {}).get("currentTier", 0)
        return str(raw_tier - 2) if raw_tier > 2 else "0"
    if target_type == "gear":
        return str(unit.get("currentTier", 0))
    return ""


def unit_meets(unit: dict, target_type: str, target_value: str) -> bool:
    target = int(target_value)
    if target_type == "stars":
        return unit.get("currentRarity", 0) >= target
    if target_type == "relic":
        raw_tier = (unit.get("relic") or {}).get("currentTier", 0)
        return raw_tier >= (target + 2 if target > 0 else 0)
    if target_type == "gear":
        return unit.get("currentTier", 0) >= target
    return False


def roster_index(player_data: dict) -> dict:
    """{base_id: unit} из ответа get_player."""
    roster = player_data.get("rosterUnit") or player_data.get("roster") or []
    return {(u.get("baseId") or u.get("definitionId", "").split(":")[0]): u for u in roster}


def plan_player(guild_id: int, ally_code: str, requirements: list[tuple], roster: dict | None) -> dict:
    """Что поставить игроку по требованиям цели. roster=None — Comlink недоступен:
    ставим всё, кроме дублей, без снимка прогресса.
    → {"create": [(unit, type, value, initial)], "done": [unit], "duplicate": [unit]}"""
    plan = {"create": [], "done": [], "duplicate": []}
    for unit_id, target_type, target_value in requirements:
        if database.get_active_task_for_unit(guild_id, ally_code, unit_id):
            plan["duplicate"].append(unit_id)
            continue
        initial = None
        if roster is not None:
            unit = roster.get(unit_id)
            if unit is not None:  # не открыт — без снимка, как в /задачи добавить
                if unit_meets(unit, target_type, target_value):
                    plan["done"].append(unit_id)
                    continue
                initial = unit_progress(unit, target_type)
        plan["create"].append((unit_id, target_type, target_value, initial))
    return plan


def target_label(target_type: str, target_value: str) -> str:
    if target_type == "stars":
        return f"⭐ Звёзды {target_value}"
    if target_type == "relic":
        return f"♦️ Реликвия {target_value}"
    if target_type == "gear":
        return f"⚙️ Снаряжение {target_value}"
    return target_value


def short_label(target_type: str, target_value: str) -> str:
    return {"stars": f"{target_value}★", "relic": f"R{target_value}", "gear": f"G{target_value}"}.get(target_type, target_value)
