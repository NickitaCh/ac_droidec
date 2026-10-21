"""ВИ-проекция гильдии (веб /tb/projection): что гильдия закроет во взводах «Восхода
Империи» с нынешними складами — покрытие слотов по этапам и планетам, сколько операций
закрывается 15/15, и узкие места: каких юнитов не хватает, у кого юнит уже проходит и
кто «в шаге» (не хватает одной-двух реликвий или звезды у корабля).

Раскладка взводов — стандартная, из tb_platoon_data.py (та же, что у конструктора
/tb/platoons). Этап планеты — её место в своей ветке (Мустафар 1, Джеонозис 2 …), Зеффо
открывается на 3 этапе, Мандалор — на 4. Юнит игрока встаёт во взвод один раз за этап,
на какой бы планете ни был слот, поэтому слоты одного юнита на всех планетах этапа
считаются вместе. Лимит «не больше 10 юнитов игрока на планету» не учитывается: у
гильдии из 50 человек он почти никогда не мешает."""

import database
import tb_platoon_data
from services import unit_forecast
from tb_platoon_engine import SHIP_MIN_STARS

BRANCHES = {
    "dark": ["Mustafar", "Geonosis", "Dathomir", "Haven-class Medical Station", "Malachor", "Death Star"],
    "mixed": ["Corellia", "Felucia", "Tatooine", "Kessel", "Vandor", "Hoth"],
    "light": ["Coruscant", "Bracca", "Kashyyyk", "Lothal", "Ring of Kafrene", "Scarif"],
}
BONUS_PHASE = {"Zeffo": 3, "Mandalore": 4}
OPERATIONS = range(1, 7)
STEP_RELICS = 2  # «в шаге» — до двух реликвий ниже порога


def phase_planets(phase: int, with_bonus: bool = True) -> list[str]:
    planets = [branch[phase - 1] for branch in BRANCHES.values()]
    if with_bonus:
        planets += [p for p, ph in BONUS_PHASE.items() if ph == phase]
    return planets


def _level(unit: dict) -> dict:
    lv = unit_forecast.current_level(unit)
    return {"stars": lv["stars"], "relic": lv["relic"]}


def _meets(level: dict, min_relic: int, is_ship: bool) -> bool:
    if is_ship:
        return level["stars"] >= SHIP_MIN_STARS
    return level["relic"] is not None and level["relic"] >= min_relic


def _in_step(level: dict, min_relic: int, is_ship: bool) -> bool:
    if is_ship:
        return level["stars"] == SHIP_MIN_STARS - 1
    return level["relic"] is not None and min_relic - STEP_RELICS <= level["relic"] < min_relic


def build(guild_id: int, with_bonus: bool = True) -> dict:
    """{phases: [{phase, slots, filled, pct, planets: [{planet, min_relic, slots, filled,
    ops_closed, ops: [{operation, slots, filled}]}], bottlenecks: [...]}], players, unresolved}."""
    members = [(code, name) for _discord_id, code, name in database.get_all_user_mappings(guild_id)]
    names = database.get_player_names(guild_id)
    codes = [c for c, _ in members]

    all_names = {n for slots in tb_platoon_data.ROTE_PLATOON_SUGGESTIONS.values() for n in slots}
    name_to_base_id = database.resolve_unit_display_names(sorted(all_names))
    base_ids = sorted({b for b in name_to_base_id.values() if b})
    types = database.get_unit_types(base_ids)
    unit_names = database.get_game_unit_names(base_ids)

    # base_id -> [(ally_code, level)] по всей гильдии, один запрос.
    owners: dict[str, list] = {}
    for row in database.get_player_unit_owners_bulk(codes, base_ids):
        owners.setdefault(row["base_id"], []).append((row["ally_code"], _level(row["unit"])))
    players_with_roster = {code for rows in owners.values() for code, _ in rows}

    phases = []
    for phase in range(1, 7):
        planets = phase_planets(phase, with_bonus)
        # Слоты этапа по юниту: [(planet, operation, min_relic)], строгие первыми — тогда
        # жадная раздача игроков с высокой реликвией оптимальна (пороги вложены).
        slots_by_unit: dict[str, list] = {}
        planet_rows = {}
        for planet in planets:
            min_relic = tb_platoon_data.ROTE_MIN_RELIC_BY_PLANET.get(planet, 5)
            ops = []
            for op in OPERATIONS:
                unit_list = tb_platoon_data.ROTE_PLATOON_SUGGESTIONS.get((planet, op)) or []
                ops.append({"operation": op, "slots": len(unit_list), "filled": 0})
                for display in unit_list:
                    bid = name_to_base_id.get(display)
                    slots_by_unit.setdefault(bid or f"?{display}", []).append((planet, op, min_relic))
            planet_rows[planet] = {"planet": planet, "min_relic": min_relic, "ops": ops}

        bottlenecks = []
        for bid, slots in slots_by_unit.items():
            is_ship = types.get(bid) == "ship"
            # Операции по порядку: нехватка уходит в последние операции планеты, а не
            # размазывается по всем — так видно, сколько операций реально закрыть.
            slots.sort(key=lambda s: (-s[2], s[1]))
            pool = sorted(owners.get(bid, []), key=lambda o: -((o[1]["stars"] if is_ship else (o[1]["relic"] or -1))))
            used = set()
            short = 0
            for planet, op, min_relic in slots:
                taker = next((code for code, lv in pool if code not in used and _meets(lv, min_relic, is_ship)), None)
                if taker is None:
                    short += 1
                    continue
                used.add(taker)
                planet_rows[planet]["ops"][op - 1]["filled"] += 1
            if short and not bid.startswith("?"):
                strict = max(s[2] for s in slots)
                ready = sorted(names.get(c, c) for c, lv in pool if _meets(lv, strict, is_ship))
                step = sorted(names.get(c, c) for c, lv in pool if _in_step(lv, strict, is_ship))
                bottlenecks.append({
                    "base_id": bid, "name": unit_names.get(bid, bid), "is_ship": is_ship,
                    "need": len(slots), "short": short, "min_relic": strict,
                    "ready": ready, "step": step,
                    "planets": sorted({s[0] for s in slots}, key=planets.index),
                })
        bottlenecks.sort(key=lambda b: (-b["short"], b["name"].lower()))

        rows = []
        for planet in planets:
            p = planet_rows[planet]
            p["slots"] = sum(o["slots"] for o in p["ops"])
            p["filled"] = sum(o["filled"] for o in p["ops"])
            p["ops_closed"] = sum(1 for o in p["ops"] if o["slots"] and o["filled"] == o["slots"])
            p["bonus"] = planet in BONUS_PHASE
            rows.append(p)
        slots_total = sum(p["slots"] for p in rows)
        filled = sum(p["filled"] for p in rows)
        phases.append({
            "phase": phase, "planets": rows, "slots": slots_total, "filled": filled,
            "pct": round(filled / slots_total * 100) if slots_total else 0,
            "ops_closed": sum(p["ops_closed"] for p in rows), "ops_total": len(rows) * len(OPERATIONS),
            "bottlenecks": bottlenecks,
        })
    return {
        "phases": phases,
        "players": len(players_with_roster),
        "members": len(members),
        "unresolved": sorted(n for n in all_names if not name_to_base_id.get(n)),
    }
