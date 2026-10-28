"""Эффекты способностей — какие юниты накладывают положительный/отрицательный эффект X
(идея swgoh.ru «Эффекты способностей», 2026-10-28). Веб /effects, фильтр «только мои».

Источник — игровые данные, без разбора текста описаний: способность юнита (последний
уровень) → effectReference → эффект (коллекция effect, флаг 16) и вложенные в него
эффекты. У эффекта, который считается в подвигах, есть тег `featcounter_<тег>` — по нему
и группируем; русское имя — ключ локализации FEATCOUNTER_* (для тегов с другим написанием
— FEATCOUNTER_KEYS), иначе имя из persistentLocKey самого эффекта. Положительный или
отрицательный — по тегам clearable/countable_buff|debuff у эффектов с этим тегом.

Считается раз в сутки в units_sync, лежит в bot_state STATE_KEY."""

import asyncio
import json
import re
from collections import Counter, defaultdict

import database

STATE_KEY = "unit_effects"
EFFECT_DEFINITIONS_FLAG = 16
KIND_LABELS = {"buff": "Положительные", "debuff": "Отрицательные", "other": "Прочие"}
# Тег эффекта → ключ FEATCOUNTER_* там, где имена не совпадают (проверено на живых данных
# 2026-10-28: эффект «Оглушение» несёт тег paralysis_debuff, ключ — FEATCOUNTER_STUN).
FEATCOUNTER_KEYS = {
    "paralysis_debuff": "STUN", "taunt_buff": "TAUNT", "attack_buff": "OFFENSE_UP", "attack_debuff": "OFFENSE_DOWN",
    "stealth_buff": "STEALTH", "shield_buff": "PROTECTION_UP", "speed_buff": "SPEED_UP", "speed_debuff": "SPEED_DOWN",
    "crit_damage_buff": "CRITICAL_DAMAGE_UP", "crit_damage_debuff": "CRITICAL_DAMAGE_DOWN",
    "attack_crit_buff": "CRITICAL_CHANCE_UP", "attack_crit_debuff": "CRITICAL_CHANCE_DOWN",
    "armor_buff": "DEFENSE_UP", "armor_debuff": "DEFENSE_DOWN", "tenacity_buff": "TENACITY_UP",
    "tenacity_debuff": "TENACITY_DOWN", "potency_buff": "POTENCY_UP", "potency_debuff": "POTENCY_DOWN",
    "stifle_debuff": "ABILITY_BLOCK", "health_recovery_immunity": "HEALING_IMMUNITY", "immunity_debuff": "BUFF_IMMUNITY",
    "advantage_buff": "ADVANTAGE", "dot_debuff": "DOT", "dodge_buff": "EVASION_UP", "dodge_debuff": "EVASION_DOWN",
    "bomb_debuff": "THERMAL_DETONATOR", "loyalhand_buff": "LOYAL_HAND", "decay_debuff": "DECAY",
    "invincibility": "DAMAGE_IMMUNITY", "vulnerable_debuff": "VULNERABLE", "stagger": "STAGGER",
    "armor_penetration_buff": "DEFENSE_PENETRATION_UP", "attack_crit_avoidance_buff": "CRITICAL_AVOIDANCE_UP",
    "armorshred_effect": "ARMOR_SHRED", "healthsteal_buff": "HEALTH_STEAL_UP", "shock_debuff": "SHOCK_DEBUFF",
    "accuracy_buff": "ACCURACY_UP", "accuracy_debuff": "ACCURACY_DOWN", "max_health_debuff": "HEALTH_DOWN",
    "masterstraining": "MASTERS_TRAINING", "targetpractice": "TARGET_PRACTICE", "a_plague": "PLAGUE",
}
_MARKUP_RE = re.compile(r"\[[0-9A-Fa-f]{6}\]|\[-\]|\[/?[a-z]\]")


def _clean(text: str) -> str:
    return _MARKUP_RE.sub("", text or "").split(":")[0].strip().strip('"«»')


def _counters(effect: dict) -> list[str]:
    return [t["tag"][len("featcounter_"):] for t in effect.get("descriptiveTag") or []
            if t.get("tag", "").startswith("featcounter_")]


def parse(effects: list, abilities: list, skills: list, units: list, loc: dict) -> dict:
    """{tag: {name, kind, units: [base_id]}} — по всем персонажам и кораблям units."""
    by_id = {e["id"]: e for e in effects if e.get("id")}
    ability_by_id = {a["id"]: a for a in abilities if a.get("id")}
    ability_of_skill = {s["id"]: s.get("abilityReference") for s in skills if s.get("id")}

    loc_names: dict[str, Counter] = defaultdict(Counter)
    kinds: dict[str, Counter] = defaultdict(Counter)
    for e in by_id.values():
        tags = {t.get("tag", "") for t in e.get("descriptiveTag") or []}
        kind = "debuff" if tags & {"clearable_debuff", "countable_debuff"} else (
            "buff" if tags & {"clearable_buff", "countable_buff"} else "other")
        for tag in _counters(e):
            kinds[tag][kind] += 1
            if e.get("persistentLocKey"):
                name = _clean(loc.get(e["persistentLocKey"], ""))
                if name:
                    loc_names[tag][name] += 1

    def walk(effect_id: str, seen: set, out: set) -> None:
        if effect_id in seen or effect_id not in by_id:
            return
        seen.add(effect_id)
        effect = by_id[effect_id]
        out.update(_counters(effect))
        for ref in effect.get("effectReference") or []:
            walk(ref.get("id"), seen, out)

    owners: dict[str, set] = defaultdict(set)
    for unit in units:
        found: set = set()
        for ref in unit.get("skillReference") or []:
            ability = ability_by_id.get(ability_of_skill.get(ref.get("skillId")))
            if not ability:
                continue
            tiers = ability.get("tier") or []
            for ref_e in (tiers[-1] if tiers else ability).get("effectReference") or []:
                walk(ref_e.get("id"), set(), found)
        for tag in found:
            owners[tag].add(unit["baseId"])

    result = {}
    for tag, base_ids in owners.items():
        name = (loc.get(f"FEATCOUNTER_{FEATCOUNTER_KEYS[tag]}") if tag in FEATCOUNTER_KEYS else None) \
            or loc.get(f"FEATCOUNTER_{tag.upper()}")
        name = _clean(name) if name else (loc_names[tag].most_common(1)[0][0] if loc_names[tag] else None)
        if not name:
            continue
        kind = kinds[tag].most_common(1)[0][0] if kinds[tag] else "other"
        if kind == "other":
            kind = "debuff" if tag.endswith("_debuff") else ("buff" if tag.endswith("_buff") else "other")
        name = name[0].upper() + name[1:]
        # Два тега с одним именем (обычное и «50%» избегание крита) — один пункт списка.
        same = next((k for k, v in result.items() if v["name"] == name and v["kind"] == kind), None)
        if same:
            result[same]["units"] = sorted(set(result[same]["units"]) | base_ids)
            continue
        result[tag] = {"name": name, "kind": kind, "units": sorted(base_ids)}
    return result


async def sync(comlink, abilities: list, skills: list, units: list, loc: dict) -> int:
    data = await asyncio.to_thread(comlink.get_game_data, items=str(EFFECT_DEFINITIONS_FLAG), include_pve_units=False)
    # ~63 тыс. эффектов — разбор в потоке, иначе стоит цикл событий бота (сторож ловил 6 с).
    result = await asyncio.to_thread(parse, data.get("effect") or [], abilities, skills, units, loc)
    if result:
        database.set_bot_state(STATE_KEY, json.dumps(result, ensure_ascii=False), guild_id=0)
    return len(result)


def load() -> dict:
    raw = database.get_bot_state(STATE_KEY, guild_id=0)
    return json.loads(raw) if raw else {}
