"""Требования «Руководства» (Journey Guide) из Comlink — для постановки задач сразу на
всех «чайлдов» ГЛ / капшипа / путешествия / старого события (/задачи руководство, веб /tasks).

Где это лежит в игровых данных (проверено на живом Comlink 2026-10-13):
1. Квесты открытия (source='quest', реальные требования игры):
   unitGuideDefinition[].additionalActivationRequirementId → requirement[].requirementItem[]
   с type 105 → id челленджа в коллекции challenge («1 из 3» и т.п.) → challenge.task[]:
   юнит в actionLinkDef.link (`base_id=CAPTAINREX`), уровень в descKey
   (`GLEVENT_PREREQ_RELIC_07`, `GLEVENT_PREREQ_STAR_04`, `EVENT_PREREQ_GEAR_12`).
   У Енота и Дарта Джа-Джа ссылки нет — их квесты `Event_Unlock_<Событие>_TierNN`
   находятся по имени узла кампании. `EVENT_PREREQ_ERA_LEVEL_*` (уровень эпохи) не берём.
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
CHALLENGE_REQUIREMENT_TYPE = 105

KIND_LABELS = {"gl": "Легенды галактики", "capital": "Флагманы", "journey": "Путешествия",
               "event": "Старые события (минимум для входа)"}

_DESC_RE = re.compile(r"PREREQ_(RELIC|STAR|GEAR)_(\d+)$")
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


def _units_from_challenges(challenges: list) -> dict:
    """{(unit, target_type): max value} по заданиям квестов."""
    units = {}
    for challenge in challenges:
        for task in challenge.get("task", []):
            link = (task.get("actionLinkDef") or {}).get("link", "")
            unit_m = _BASE_ID_RE.search(link)
            desc_m = _DESC_RE.search(task.get("descKey", ""))
            if not unit_m or not desc_m:
                continue  # скрытые задания ещё не вышедшей цели, уровень эпохи
            target_type, value = _DESC_TYPES[desc_m.group(1)], int(desc_m.group(2))
            key = (unit_m.group(1), target_type)
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
                             campaigns: list | None = None, selftags: dict | None = None) -> list[tuple]:
    """Строки для database.replace_journey_guide_requirements. Юнит, встречающийся в
    нескольких квестах одной цели, берётся один раз — с максимальным уровнем."""
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
        units = _units_from_challenges([ch_by_id[q] for q in quest_ids if q in ch_by_id])
        kind, source = _guide_kind(entry), "quest"
        ce = entry.get("campaignElementIdentifier") or {}
        if not units and ce.get("campaignNodeId"):
            found = sorted(unlock_quests.get(_norm(ce["campaignNodeId"]), []), key=lambda c: c["id"])
            units = _units_from_challenges(found)
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
        seen_units = set()
        for order, ((unit_id, target_type), value) in enumerate(units.items()):
            if unit_id in seen_units:
                continue
            seen_units.add(unit_id)
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
    raw_selftags = database.get_bot_state("unit_selftags", guild_id=0)
    selftags = json.loads(raw_selftags) if raw_selftags else {}
    rows = parse_guide_requirements(guides, requirements, challenges, campaigns, selftags)
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
