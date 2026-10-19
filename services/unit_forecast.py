"""/юнит прогноз — юнит игрока в его текущих модах сейчас и на заданных звёздах/гире/
релике (как C3PO `p unit`). Без Discord/веб-импортов: расчёт здесь, картинка —
services/unit_forecast_image.py.

Подмена прокачки — на копии юнита: currentRarity (звёзды), currentTier (гир),
relic.currentTier (релик + 2). Если меняется гир или ставится релик, надетые детали
шмота (equipment) снимаются: их набор на другом тире неизвестен, считается только
табличный бонус тира (как и в stat_engine.project_unit_relic). Моды, уровень и
навыки — реальные. Вклад модов = итог − тот же юнит без модов."""

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
    "Armor Penetration": "Пробой брони",
    "Armor": "Броня",
    "Special Damage": "Особый урон",
    "Special Critical Chance": "Шанс особ. крита",
    "Resistance Penetration": "Пробой сопр.",
    "Resistance": "Сопротивление",
    "Physical Accuracy": "Точность",
}

# Вклад модов в скобках показываем только там, где моды что-то дают (как C3PO).
MOD_STATS = {
    "Health", "Protection", "Speed", "Physical Damage", "Special Damage",
    "Physical Critical Chance", "Special Critical Chance", "Armor", "Resistance",
    "Potency", "Tenacity", "Critical Damage", "Physical Accuracy",
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


def _snapshot(stat_calc, unit: dict) -> dict:
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
    current = _snapshot(stat_calc, unit)
    projected = None
    if any(v is not None for v in (stars, gear, relic)):
        target = project(unit, stars, gear, relic)
        if current_level(target) != current["level"]:
            projected = _snapshot(stat_calc, target)
    return {
        "current": current,
        "projected": projected,
        "groups": STAT_GROUPS,
        "labels": STAT_LABELS,
        "percent": {n for n in STAT_LABELS if n in stat_engine.PERCENT_STATS},
    }
