"""/юнит прогноз — юнит игрока в его текущих модах сейчас и на заданных звёздах/гире/
релике (как C3PO `p unit`). Без Discord/веб-импортов: расчёт здесь, картинка —
services/unit_forecast_image.py.

Подмена прокачки — на копии юнита: currentRarity (звёзды), currentTier (гир),
relic.currentTier (релик + 2). Если меняется гир или ставится релик, надетые детали
шмота (equipment) снимаются: их набор на другом тире неизвестен, считается только
табличный бонус тира (как и в stat_engine.project_unit_relic). Моды, уровень и
навыки — реальные. Вклад модов = итог − тот же юнит без модов.

Корабли (forecast_ship): прогноз только по звёздам корабля — гир/релик, если заданы,
молча игнорируются (у кораблей их нет, как и модов), статы зависят от звёзд и прокачки экипажа (StatCalc.calc_ship_stats).
Скорость корабля растёт со звёздами (Палач: 167 на 1★ → 192 на 7★)."""

import copy

import stat_engine

# Порядок и группы строк — как у C3PO; подписи — короткие русские из /статы.
STAT_GROUPS = [
    ["Health", "Protection", "Speed", "Critical Damage"],
    ["Potency", "Tenacity", "Health Steal"],
    ["Physical Damage", "Physical Critical Chance", "Armor Penetration", "Armor"],
    ["Special Damage", "Special Critical Chance", "Resistance Penetration", "Resistance"],
    ["Physical Accuracy"],
]

STAT_LABELS = {
    "Health": "Здоровье",
    "Protection": "Защита",
    "Speed": "Скорость",
    "Critical Damage": "Крит. урон",
    "Potency": "Эффективность",
    "Tenacity": "Стойкость",
    "Health Steal": "Кража здоровья",
    "Physical Damage": "Физ. урон",
    "Physical Critical Chance": "Шанс физ. крита",
    "Armor Penetration": "Пробитие брони",
    "Armor": "Броня",
    "Special Damage": "Особый урон",
    "Special Critical Chance": "Шанс особ. крита",
    "Resistance Penetration": "Особое пробитие",
    "Resistance": "Сопротивление",
    "Physical Accuracy": "Точность",
}

# Вклад модов в скобках показываем только там, где моды что-то дают (как C3PO).
MOD_STATS = {
    "Health", "Protection", "Speed", "Physical Damage", "Special Damage",
    "Physical Critical Chance", "Special Critical Chance", "Armor", "Resistance",
    "Potency", "Tenacity", "Critical Damage", "Physical Accuracy",
}


# Финальные статы корабля StatCalc отдаёт по номерам UnitStat, проценты — долями (0–1).
SHIP_STAT_GROUPS = [
    ["Health", "Protection", "Speed", "Critical Damage"],
    ["Potency", "Tenacity", "Health Steal"],
    ["Physical Damage", "Physical Critical Chance", "Armor Penetration", "Armor"],
    ["Special Damage", "Special Critical Chance", "Resistance Penetration", "Resistance"],
]
SHIP_STAT_IDS = {
    "Health": "1", "Protection": "28", "Speed": "5", "Critical Damage": "16",
    "Potency": "17", "Tenacity": "18", "Health Steal": "27",
    "Physical Damage": "6", "Physical Critical Chance": "14", "Armor Penetration": "10", "Armor": "8",
    "Special Damage": "7", "Special Critical Chance": "15", "Resistance Penetration": "11", "Resistance": "9",
}


def current_level(unit: dict) -> dict:
    gear = unit.get("currentTier") or 1
    relic_tier = (unit.get("relic") or {}).get("currentTier") or 0
    return {
        "stars": unit.get("currentRarity") or 1,
        "gear": gear,
        "relic": relic_tier - 2 if gear >= 13 and relic_tier >= 2 else None,
    }


def validate(stars: int | None, gear: int | None, relic: int | None) -> str | None:
    if stars is not None and not 1 <= stars <= 7:
        return "Звёзды — от 1 до 7."
    if gear is not None and not 1 <= gear <= 13:
        return "Гир — от 1 до 13."
    if relic is not None and not 0 <= relic <= 10:
        return "Реликвия — от 0 до 10."
    if relic is not None and gear is not None and gear < 13:
        return "Реликвия бывает только на G13 — укажите либо гир ниже 13, либо релик."
    return None


def project(unit: dict, stars: int | None = None, gear: int | None = None, relic: int | None = None) -> dict:
    projected = copy.deepcopy(unit)
    cur = current_level(unit)
    if stars is not None:
        projected["currentRarity"] = stars
    if relic is not None:
        gear = 13
    if gear is not None and gear != cur["gear"]:
        projected["currentTier"] = gear
        projected["equipment"] = []
    if gear is not None and gear < 13:
        projected["relic"] = {"currentTier": 1}  # релик закрыт
    if relic is not None:
        projected.setdefault("relic", {})
        projected["relic"] = dict(projected["relic"] or {}, currentTier=relic + 2)
    return projected


def snapshot(stat_calc, unit: dict) -> dict:
    final = stat_engine.calc_final_stats(stat_calc, unit)
    modless_unit = copy.deepcopy(unit)
    modless_unit["equippedStatMod"] = []
    modless = stat_engine.calc_final_stats(stat_calc, modless_unit)
    stats = {}
    for name in STAT_LABELS:
        value = final.get(name) or 0
        mods = value - (modless.get(name) or 0) if name in MOD_STATS else 0
        stats[name] = {"value": value, "mods": mods}
    try:
        gp = stat_calc.calc_char_gp(copy.deepcopy(unit))
    except Exception:
        gp = None
    return {"level": current_level(unit), "gp": gp, "stats": stats}


def forecast(stat_calc, unit: dict, stars: int | None = None, gear: int | None = None, relic: int | None = None) -> dict:
    """{"current": снимок, "projected": снимок или None, "groups": STAT_GROUPS,
    "labels": STAT_LABELS, "percent": {...}}. projected = None, если ничего не задано
    или задано то же, что сейчас."""
    current = snapshot(stat_calc, unit)
    projected = None
    if stars is None and (gear is not None or relic is not None):
        stars = 7  # звёзды не заданы — тянем до 7★ (реликвии в игре и так только на 7★)
    if any(v is not None for v in (stars, gear, relic)):
        target = project(unit, stars, gear, relic)
        if current_level(target) != current["level"]:
            projected = snapshot(stat_calc, target)
    return {
        "current": current,
        "projected": projected,
        "groups": STAT_GROUPS,
        "labels": STAT_LABELS,
        "percent": {n for n in STAT_LABELS if n in stat_engine.PERCENT_STATS},
    }


def ship_crew(stat_calc, roster: list, base_id: str) -> list:
    """Экипаж корабля из ростера игрока (сырые rosterUnit) — в порядке из игровых данных."""
    meta = (stat_calc._unit_data or {}).get(base_id) or {}
    by_id = {(u.get("definitionId") or "").split(":")[0]: u for u in roster}
    return [by_id[c] for c in meta.get("crew") or [] if c in by_id]


def _ship_snapshot(stat_calc, ship: dict, crew: list) -> dict:
    crew = [copy.deepcopy(c) for c in crew]
    for c in crew:  # calc_ship_stats суммирует ГМ экипажа, сам его не считает
        c["gp"] = stat_calc.calc_char_gp(copy.deepcopy(c))
    result = stat_calc.calc_ship_stats(copy.deepcopy(ship), crew)
    final = (result.get("stats") or {}).get("final") or {}
    stats = {}
    for name, stat_id in SHIP_STAT_IDS.items():
        value = float(final.get(stat_id) or 0)
        if name in stat_engine.PERCENT_STATS:
            value *= 100
        stats[name] = {"value": value, "mods": 0}
    level = {"stars": ship.get("currentRarity") or 1, "gear": None, "relic": None}
    return {"level": level, "gp": result.get("gp"), "stats": stats}


def forecast_ship(stat_calc, ship: dict, crew: list, stars: int | None = None) -> dict:
    """Как forecast, но для корабля: прогноз только по звёздам."""
    current = _ship_snapshot(stat_calc, ship, crew)
    projected = None
    if stars is not None and stars != current["level"]["stars"]:
        target = copy.deepcopy(ship)
        target["currentRarity"] = stars
        projected = _ship_snapshot(stat_calc, target, crew)
    return {
        "current": current,
        "projected": projected,
        "groups": SHIP_STAT_GROUPS,
        "labels": STAT_LABELS,
        "percent": {n for n in SHIP_STAT_IDS if n in stat_engine.PERCENT_STATS},
        "is_ship": True,
    }
