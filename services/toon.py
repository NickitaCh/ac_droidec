"""/юнит — персонажи игроков бок о бок, как HotBot `/toon`: статы (с вкладом
модов, лучшее значение среди игроков подсвечено), 6 модов со вторичками и числом
прокачек, способности с зетой/омикроном. Без Discord/веб-импортов: данные здесь,
картинка — services/toon_image.py.

Игрок — сырой get_player; статы считает StatCalc так же, как /юнит_прогноз
(services/unit_forecast.snapshot), моды декодирует services/mod_search.decode_mod."""

import database
from services import mod_search, unit_forecast

# Порядок строк — как у HotBot.
STATS = [
    "Speed", "Health", "Protection", "Physical Critical Chance", "Special Critical Chance",
    "Critical Damage", "Physical Damage", "Special Damage", "Potency", "Tenacity",
    "Armor", "Resistance", "Armor Penetration", "Resistance Penetration",
]

SLOT_ORDER = ["square", "arrow", "diamond", "triangle", "circle", "cross"]

# Короткие подписи статов модов (unitStatId) — проценты видно по «%» в значении.
MOD_STAT_RU = {
    1: "Здоровье", 55: "Здоровье", 28: "Защита", 56: "Защита", 41: "Атака", 48: "Атака",
    42: "Оборона", 49: "Оборона", 5: "Скорость", 17: "Эффективность", 18: "Стойкость",
    53: "Шанс крита", 16: "Крит. урон", 54: "Избег. крита", 52: "Точность",
}

# Способности в карточке: сначала лидерка, потом особые, уникальные, базовая.
_SKILL_ORDER = ("leaderskill", "specialskill", "uniqueskill", "basicskill")


def base_id_of(unit: dict) -> str:
    return (unit.get("definitionId") or "").split(":")[0]


def find_unit(player: dict, base_id: str) -> dict | None:
    return next((u for u in player.get("rosterUnit") or [] if base_id_of(u) == base_id), None)


def _mods(unit: dict) -> tuple[list, int]:
    """([мод по слоту SLOT_ORDER или None], скорость со вторичек)."""
    by_slot, speed = {}, 0
    for raw in unit.get("equippedStatMod") or []:
        mod = mod_search.decode_mod(raw)
        if mod is None:
            continue
        rolls = [s.get("statRolls") or 1 for s in raw.get("secondaryStat") or [] if (s or {}).get("stat")]
        for sec, n in zip(mod["secondaries"], rolls):
            sec["rolls"] = n
            if sec["stat_id"] == 5:
                speed += sec["value"]
        by_slot[mod["slot_key"]] = mod
    return [by_slot.get(k) for k in SLOT_ORDER], int(round(speed))


def _skill_sort_key(skill_id: str, name: str):
    prefix = next((i for i, p in enumerate(_SKILL_ORDER) if skill_id.startswith(p)), len(_SKILL_ORDER))
    return prefix, skill_id, name


def abilities(units: list[dict | None]) -> list[dict]:
    """Способности с зетой/омикроном у этого персонажа — объединение навыков всех
    сравниваемых игроков (у игрока без навыка он просто не выдан). [{skill_id, name,
    zeta_tier, omicron_tier, mode}] в порядке _SKILL_ORDER."""
    skill_ids = list(dict.fromkeys(sk.get("id") for u in units if u for sk in u.get("skill") or [] if sk.get("id")))
    tiers = database.get_all_skill_tier_info()
    names = database.get_skill_display_info(skill_ids)
    out = []
    for skill_id in skill_ids:
        info = tiers.get(skill_id)
        if not info:
            continue
        zeta_tier, omicron_tier, mode = info
        name = (names.get(skill_id) or (skill_id,))[0] or skill_id
        out.append({"skill_id": skill_id, "name": name, "zeta_tier": zeta_tier,
                    "omicron_tier": omicron_tier, "mode": mode or ""})
    out.sort(key=lambda a: _skill_sort_key(a["skill_id"], a["name"]))
    return out


def _ability_status(unit: dict, ability_list: list[dict]) -> list[dict]:
    tiers = {sk.get("id"): sk.get("tier", -1) for sk in unit.get("skill") or []}
    out = []
    for a in ability_list:
        tier = tiers.get(a["skill_id"])
        out.append(dict(
            a,
            zeta=a["zeta_tier"] is not None and tier is not None and tier >= a["zeta_tier"],
            omicron=a["omicron_tier"] is not None and tier is not None and tier >= a["omicron_tier"],
        ))
    return out


def card(stat_calc, player_name: str, unit: dict | None, ability_list: list[dict]) -> dict:
    """Карточка одного игрока по одному персонажу; unit=None — персонажа нет."""
    if unit is None:
        return {"player_name": player_name, "missing": True}
    snap = unit_forecast.snapshot(stat_calc, unit)
    mods, speed_secondary = _mods(unit)
    status = _ability_status(unit, ability_list)
    return {
        "player_name": player_name,
        "missing": False,
        "level": snap["level"],
        "unit_level": unit.get("currentLevel") or 1,
        "gp": snap["gp"],
        "stats": {name: snap["stats"][name] for name in STATS},
        "speed_secondary": speed_secondary,
        "mods": mods,
        "abilities": status,
        "zetas": sum(1 for a in status if a["zeta"]),
        "omicrons": sum(1 for a in status if a["omicron"]),
    }


def best_marks(cards: list[dict]) -> dict:
    """{stat: индекс карточки с лучшим значением} — только если игроков больше одного и
    лучший строго выше остальных."""
    present = [(i, c) for i, c in enumerate(cards) if not c["missing"]]
    if len(present) < 2:
        return {}
    marks = {}
    for name in STATS:
        values = sorted(((c["stats"][name]["value"], i) for i, c in present), reverse=True)
        if values[0][0] - values[1][0] > 1e-6:
            marks[name] = values[0][1]
    return marks


def build(stat_calc, players: list[dict], base_ids: list[str]) -> list[dict]:
    """players — сырые get_player; [{base_id, name, cards: [...], best: {...}}] по персонажам."""
    names = database.get_game_unit_names(base_ids)
    rows = []
    for base_id in base_ids:
        units = [find_unit(p, base_id) for p in players]
        ability_list = abilities(units)
        cards = [card(stat_calc, p.get("name") or "?", u, ability_list) for p, u in zip(players, units)]
        rows.append({"base_id": base_id, "name": names.get(base_id, base_id), "cards": cards, "best": best_marks(cards)})
    return rows
