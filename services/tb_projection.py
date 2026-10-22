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

    # Склады гильдии читаем один раз (разбор JSON — самое дорогое на странице): из них и
    # взводы, и спецмиссии, и боевые миссии. base_id -> [(ally_code, level)].
    rosters = database.get_player_units_bulk(codes)
    wanted = set(base_ids)
    owners: dict[str, list] = {}
    for code, roster in rosters.items():
        for bid in wanted & roster.keys():
            owners.setdefault(bid, []).append((code, _level(roster[bid])))
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
    # Спецмиссии, боевые миссии и прогноз звёзд (импорт здесь — tb_covert и tb_forecast
    # сами берут BRANCHES отсюда).
    from services import tb_covert, tb_forecast
    member_names = {code: names.get(code) or name or code for code, name in members}
    covert_ready = {}
    for m in tb_covert.missions():
        if m["bonus"] and not with_bonus:
            continue
        row = covert_row(m, rosters, member_names)
        covert_ready[m["zone_id"]] = len(row["ready"])
        phases[m["phase"] - 1].setdefault("covert", []).append(row)
    zone_data = tb_forecast.zones()
    for rule in (zone_data.get("bonus") or {}).values():
        for p in phases:
            for c in p.get("covert") or []:
                if c["zone_id"] == rule["covert_zone"]:
                    c["unlock_wins"] = rule["wins"]
    strikes = tb_forecast.strikes_by_phase(rosters, member_names, with_bonus)
    for p in phases:
        p["strikes"] = strikes.get(p["phase"], [])
    forecast = None
    gp = tb_forecast.guild_gp(codes)
    if zone_data.get("planets") and gp:
        op_points = {pl: v["op_points"] for pl, v in zone_data["planets"].items()}
        platoon_points = {pl["planet"]: pl["ops_closed"] * op_points.get(pl["planet"], 0) for p in phases for pl in p["planets"]}
        hist = tb_forecast.history(guild_id)
        if not with_bonus:
            zone_data = {**zone_data, "bonus": {}}
        forecast = tb_forecast.forecast(zone_data, platoon_points, covert_ready, hist, gp)
        forecast["history"] = hist
    return {
        "phases": phases,
        "forecast": forecast,
        "players": len(players_with_roster),
        "members": len(members),
        "unresolved": sorted(n for n in all_names if not name_to_base_id.get(n)),
    }


def covert_row(m: dict, rosters: dict, member_names: dict) -> dict:
    """Спецмиссия по гильдии: ready — проходят, step — «в шаге» (все отстающие обязательные
    в 1–2 реликвиях от порога), short — не дотягивают сильнее; у двух последних — что
    докачать. Без обязательных (Зеффо) — сколько подходящих юнитов из нужных."""
    from services import tb_covert
    all_ids = {b for r in rosters.values() for b in r}
    ships = {b for b, t in database.get_unit_types(list(all_ids)).items() if t == "ship"}
    unit_names = database.get_game_unit_names(m["mandatory"])
    ready, step, short = [], [], []
    for code, roster in rosters.items():
        s = tb_covert.player_status(m, roster, ships)
        name = member_names.get(code, code)
        if s["status"] == "ready":
            ready.append(name)
            continue
        if s["status"] != "short":
            continue
        if not m["mandatory"]:
            short.append((name, s["text"]))
            continue
        lagging = [b for b in m["mandatory"] if not tb_covert._unit_ok(roster[b], m)]
        text = ", ".join(f"{unit_names.get(b, b)} {tb_covert._level(roster[b])}" for b in lagging)
        close = m["min_relic"] is not None and all(
            _level(roster[b])["stars"] >= m["min_stars"] and (_level(roster[b])["relic"] or -9) >= m["min_relic"] - STEP_RELICS
            for b in lagging)
        (step if close else short).append((name, text))
    lead = m["mandatory"][0] if m["mandatory"] else (m["units"] or [None])[0]
    return {
        "zone_id": m["zone_id"], "planet": m["planet"], "base_id": lead,
        "name": " + ".join(unit_names.get(b, b) for b in m["mandatory"]) or "любые подходящие",
        "label": tb_covert.mission_label(m), "unlocks": m["unlocks"], "bonus": m["bonus"],
        "ready": sorted(ready, key=str.lower),
        "step": sorted(step, key=lambda x: x[0].lower()),
        "short": sorted(short, key=lambda x: x[0].lower()),
    }
