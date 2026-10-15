"""Требования «Руководства» (Journey Guide) из Comlink — для постановки задач сразу на
всех «чайлдов» ГЛ / капшипа / путешествия / старого события (/задачи руководство, веб /tasks).

Где это лежит в игровых данных (проверено на живом Comlink 2026-10-13):
1. Квесты открытия (source='quest', реальные требования игры):
   unitGuideDefinition[].additionalActivationRequirementId → requirement[].requirementItem[]
   с type 105 → id челленджа в коллекции challenge («1 из 3» и т.п.) → challenge.task[]:
   юнит в actionLinkDef.link (`base_id=CAPTAINREX`), уровень в descKey
   (`GLEVENT_PREREQ_RELIC_07`, `GLEVENT_PREREQ_STAR_04`, `EVENT_PREREQ_GEAR_12`).
   У Енота и Дарта Джа-Джа ссылки нет — их квесты `Event_Unlock_<Событие>_TierNN`
   находятся по имени узла кампании. `EVENT_PREREQ_ERA_LEVEL_N` — «уровень эпохи» героя
   эпохи (один уровень вместо уровня/снаряжения/способностей); по таблице
   eraDefinition[].eraLevel он переводится в реликвию или снаряжение (125 = R4). У такого
   героя две цели — звёзды и реликвия; ставится первая невыполненная (plan_player).
2. Старые события без квестов (source='entry'): условия входа в бои —
   campaign[EVENTS] → узел из guide.campaignElementIdentifier → campaignNodeMission[]
   .entryCategoryAllowed: mandatoryRosterUnit + categoryId `selftag_*` (конкретный юнит,
   соответствие selftag → base_id сохраняет services/units_sync.py) и минимумы
   звёзд/снаряжения/реликвии. Ярусы «любые N из фракции» пропускаются. Это минимум для
   входа, не для победы — гильдия может поднять уровни у себя (journey_guide_overrides).

Логика проверки целей stars/relic/gear здесь же (unit_progress/unit_meets) — ей
пользуются и cogs/tasks.py, и web/routes/tasks.py."""

import asyncio
import json
import re

import database

UNIT_GUIDE_DEFINITION_FLAG = 17179869184
REQUIREMENT_FLAG = 512
CHALLENGE_FLAG = 8388608
CAMPAIGN_FLAG = 274877906944
ERA_DEFINITION_FLAG = 2251799813685248
CHALLENGE_REQUIREMENT_TYPE = 105

KIND_LABELS = {"gl": "Легенды галактики", "capital": "Флагманы", "journey": "Путешествия",
               "event": "Старые события (минимум для входа)"}

_DESC_RE = re.compile(r"PREREQ_(RELIC|STAR|GEAR|ERA_LEVEL)_(\d+)$")
_DESC_TYPES = {"RELIC": "relic", "STAR": "stars", "GEAR": "gear"}
_BASE_ID_RE = re.compile(r"base_id=([A-Z0-9_]+)")
_UNLOCK_QUEST_RE = re.compile(r"^Event_Unlock_(.+?)(?:_Tier\d+|_\d+)?$")


def _guide_kind(entry: dict) -> str:
    if entry.get("galacticLegend"):
        return "gl"
    if entry["unitBaseId"].startswith("CAPITAL"):
        return "capital"
    return "journey"


def _norm(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", name.lower())


# Минимальные цели поверх игровых: {guide: {target_type: value}} для всех героев цели.
# Ротта: в игре сейчас уровень Эпохи 125 = R4, но к следующему событию нужно будет R6
# (RicardoOneS, тред «Гайд по АС Боту», 2026-10-15). Берётся максимум с игровым значением.
GUIDE_MIN_TARGETS = {"RACCOON": {"relic": 6}}

# Порядок выбора цели юнита: сначала реликвия (самая тяжёлая), потом снаряжение, звёзды.
TARGET_PRIORITY = {"relic": 0, "gear": 1, "stars": 2}


def era_level_index(eras: list) -> dict:
    """{unit: {era_level: (gear_tier, relic_raw)}} из eraDefinition."""
    index = {}
    for era in eras:
        levels = {e["level"]: (e.get("currentGearTier") or 0, e.get("currentRelicTier") or 0)
                  for e in era.get("eraLevel") or []}
        for unit in era.get("eraUnitId") or []:
            index[unit] = levels
    return index


def _era_level_target(level: int, levels: dict) -> tuple | None:
    """Уровень эпохи → (target_type, value): реликвия, если она уже есть на этом уровне,
    иначе снаряжение. None — уровня нет в таблице."""
    if level not in levels:
        return None
    gear, relic_raw = levels[level]
    if relic_raw >= 3:  # сырой tier: 1 — нет, 2 — R0, 3 — R1
        return "relic", relic_raw - 2
    if gear > 1:
        return "gear", gear
    return None


def _units_from_challenges(challenges: list, era_levels: dict | None = None) -> dict:
    """{(unit, target_type): max value} по заданиям квестов."""
    units = {}
    for challenge in challenges:
        for task in challenge.get("task", []):
            link = (task.get("actionLinkDef") or {}).get("link", "")
            unit_m = _BASE_ID_RE.search(link)
            desc_m = _DESC_RE.search(task.get("descKey", ""))
            if not unit_m or not desc_m:
                continue  # скрытые задания ещё не вышедшей цели, уровень эпохи
            unit_id, value = unit_m.group(1), int(desc_m.group(2))
            if desc_m.group(1) == "ERA_LEVEL":
                target = _era_level_target(value, (era_levels or {}).get(unit_id, {}))
                if target is None:
                    continue
                target_type, value = target
            else:
                target_type = _DESC_TYPES[desc_m.group(1)]
            key = (unit_id, target_type)
            if key not in units or units[key] < value:
                units[key] = value
    return units


def _units_from_entry(node: dict, guide_unit: str, selftags: dict) -> dict:
    """{(unit, target_type): value} — минимум для входа во все ярусы события, где юниты
    названы поимённо. Один уровень на юнит: реликвия > снаряжение > звёзды."""
    levels = {}  # unit -> [stars, gear, relic_raw]
    for mission in node.get("campaignNodeMission") or []:
        allowed = mission.get("entryCategoryAllowed") or {}
        named = [u["id"] for u in allowed.get("mandatoryRosterUnit") or []]
        named += [selftags[c] for c in allowed.get("categoryId") or [] if c in selftags]
        for unit in dict.fromkeys(named):
            if unit == guide_unit:
                continue
            cur = levels.setdefault(unit, [0, 0, 0])
            cur[0] = max(cur[0], min(allowed.get("minimumUnitRarity") or 0, 7))
            cur[1] = max(cur[1], allowed.get("minimumUnitTier") or 0)
            cur[2] = max(cur[2], allowed.get("minimumRelicTier") or 0)
    units = {}
    for unit, (stars, gear, relic_raw) in levels.items():
        if relic_raw >= 3:  # сырой tier реликвии: 1 — нет, 2 — R0 (=G13), 3 — R1
            units[(unit, "relic")] = relic_raw - 2
        elif relic_raw == 2 or gear >= 13:
            units[(unit, "gear")] = 13
        elif gear > 1:
            units[(unit, "gear")] = gear
        elif stars > 0:
            units[(unit, "stars")] = stars
    return units


def parse_guide_requirements(guides: list, requirements: list, challenges: list,
                             campaigns: list | None = None, selftags: dict | None = None,
                             eras: list | None = None) -> list[tuple]:
    """Строки для database.replace_journey_guide_requirements. Цель юнита (тип), встречающаяся
    в нескольких квестах, берётся один раз — с максимальным уровнем."""
    era_levels = era_level_index(eras or [])
    req_by_id = {r["id"]: r for r in requirements}
    ch_by_id = {c["id"]: c for c in challenges}
    unlock_quests = {}
    for cid, c in ch_by_id.items():
        m = _UNLOCK_QUEST_RE.match(cid)
        if m:
            unlock_quests.setdefault(_norm(m.group(1)), []).append(c)
    nodes = {}
    for camp in campaigns or []:
        for cmap in camp.get("campaignMap") or []:
            for group in cmap.get("campaignNodeDifficultyGroup") or []:
                for node in group.get("campaignNode") or []:
                    nodes[(camp["id"], cmap["id"], node["id"], group["campaignNodeDifficulty"])] = node

    rows = []
    for entry in guides:
        req = req_by_id.get(entry.get("additionalActivationRequirementId") or "")
        quest_ids = [i.get("id") for i in (req or {}).get("requirementItem", []) if i.get("type") == CHALLENGE_REQUIREMENT_TYPE]
        units = _units_from_challenges([ch_by_id[q] for q in quest_ids if q in ch_by_id], era_levels)
        kind, source = _guide_kind(entry), "quest"
        ce = entry.get("campaignElementIdentifier") or {}
        if not units and ce.get("campaignNodeId"):
            found = sorted(unlock_quests.get(_norm(ce["campaignNodeId"]), []), key=lambda c: c["id"])
            units = _units_from_challenges(found, era_levels)
        if not units and ce.get("campaignId") == "EVENTS" and selftags:
            node = nodes.get((ce["campaignId"], ce.get("campaignMapId"), ce["campaignNodeId"], ce.get("campaignNodeDifficulty")))
            if node:
                units = _units_from_entry(node, entry["unitBaseId"], selftags)
                kind, source = "event", "entry"
        if not units:
            continue
        guide_id = entry["unitBaseId"]
        if guide_id == "TBA" and req:  # анонс ещё не вышедшей цели — юнита в справочнике нет
            guide_id = req["id"].replace("_JOURNEY_GUIDE_REQUIREMENT", "")
        for target_type, min_value in GUIDE_MIN_TARGETS.get(guide_id, {}).items():
            for unit_id in {u for u, _t in units}:
                key = (unit_id, target_type)
                units[key] = max(units.get(key, 0), min_value)
        # Реликвия уже включает G13 — снаряжение из младших ярусов эпохи лишнее.
        units = {k: v for k, v in units.items() if not (k[1] == "gear" and (k[0], "relic") in units)}
        unit_order = list(dict.fromkeys(unit_id for unit_id, _t in units))
        ordered = sorted(units.items(), key=lambda kv: (unit_order.index(kv[0][0]), TARGET_PRIORITY.get(kv[0][1], 9)))
        for order, ((unit_id, target_type), value) in enumerate(ordered):
            rows.append((guide_id, kind, order, unit_id, target_type, str(value), source))
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
    campaigns = await fetch(CAMPAIGN_FLAG, "campaign")
    eras = await fetch(ERA_DEFINITION_FLAG, "eraDefinition")
    raw_selftags = database.get_bot_state("unit_selftags", guild_id=0)
    selftags = json.loads(raw_selftags) if raw_selftags else {}
    rows = parse_guide_requirements(guides, requirements, challenges, campaigns, selftags, eras)
    # Событийные копии героев (GENERALKENOBI_GLE, IG11_MANDALORIAN_EVENT) — не юниты ростера.
    known = database.get_game_unit_names([r[3] for r in rows])
    rows = [r for r in rows if r[3] in known]
    if rows:
        database.replace_journey_guide_requirements(rows)
    return len(rows)


def guide_options() -> list[dict]:
    """Цели для выбора: [{base_id, name, kind, kind_label, count, entry}]; entry=True —
    уровни из условий входа старого события (гильдия может их поднять)."""
    guides = database.get_journey_guides()
    names = database.get_game_unit_names([g[0] for g in guides])
    order = list(KIND_LABELS)
    result = [
        {"base_id": base_id, "name": names.get(base_id) or f"{base_id} (анонс, часть требований скрыта)", "kind": kind,
         "kind_label": KIND_LABELS.get(kind, kind), "count": count, "entry": source == "entry"}
        for base_id, kind, count, source in guides
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


def group_by_unit(requirements: list[tuple]) -> dict:
    """{unit: [(target_type, target_value), ...]} в порядке игры, цели — по TARGET_PRIORITY."""
    grouped = {}
    for unit_id, target_type, target_value in requirements:
        grouped.setdefault(unit_id, []).append((target_type, target_value))
    for targets in grouped.values():
        targets.sort(key=lambda t: TARGET_PRIORITY.get(t[0], 9))
    return grouped


def plan_player(guild_id: int, ally_code: str, requirements: list[tuple], roster: dict | None) -> dict:
    """Что поставить игроку по требованиям цели. У юнита может быть несколько целей
    (звёзды + реликвия у героя эпохи) — ставится одна, первая невыполненная: реликвия,
    потом снаряжение, потом звёзды. roster=None — Comlink недоступен: ставим главную цель,
    кроме дублей, без снимка прогресса.
    → {"create": [(unit, type, value, initial)], "done": [unit], "duplicate": [unit]}"""
    plan = {"create": [], "done": [], "duplicate": []}
    for unit_id, targets in group_by_unit(requirements).items():
        if database.get_active_task_for_unit(guild_id, ally_code, unit_id):
            plan["duplicate"].append(unit_id)
            continue
        unit = roster.get(unit_id) if roster is not None else None
        if unit is None:  # не открыт или нет данных — без снимка, как в /задачи добавить
            target_type, target_value = targets[0]
            plan["create"].append((unit_id, target_type, target_value, None))
            continue
        pending = [(t, v) for t, v in targets if not unit_meets(unit, t, v)]
        if not pending:
            plan["done"].append(unit_id)
            continue
        target_type, target_value = pending[0]
        plan["create"].append((unit_id, target_type, target_value, unit_progress(unit, target_type)))
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
