"""«Итоги» ВИ (веб /tb): разрез одной завершённой битвы по дням и по планетам, как
Completed Battles в HotUtils TB v2.

Всё из сохранённой расшифровки (tb_player_detail): round_totals — по игроку за каждый день
(этап), zone_data — по игроку за каждую планету целиком (за все дни, что она была открыта).
Звёзды планеты Comlink не отдаёт, считаем их сами: очки игроков на планете + очки закрытых
операций взводов (15 юнитов = операция, очки за операцию из игровых данных) против порогов
звёзд — на шести прошлых ВИ AC и AP сумма сошлась с итогом битвы до звезды (2026-10-27).
Операция засчитывается, только если в ней стоят все 15 юнитов, а по юнитам видно лишь
общее число на планете, поэтому неполные операции могут дать ошибку в одну звезду —
на практике гильдии закрывают операции целиком."""

import json

import database
from services import tb_forecast, tb_missions

FULL_DEPLOY = 0.85        # день «залил полностью»: залив ≥ 85 % самого большого залива игрока за битву (как в HotUtils)
OPERATIONS = 6
UNITS_PER_OPERATION = 15


def _num(acts: dict, action: str) -> int:
    return int(sum((acts.get(action) or {}).values()))


def _stars(points: int, thresholds: list) -> tuple[int, int | None]:
    """(звёзд, сколько не хватило до следующей звезды или None)."""
    stars = sum(1 for t in thresholds if points >= t)
    nxt = next((t for t in thresholds if points < t), None)
    return stars, (nxt - points if nxt is not None else None)


def event_breakdown(event_id: int) -> dict | None:
    rows = database.get_tb_details_for_event(event_id)
    if not rows:
        return None
    zones = (tb_forecast.zones() or {}).get("planets") or {}
    waves_by_zone = tb_missions.strike_zones()

    days: dict[int, dict] = {}
    planets: dict[tuple, dict] = {}
    players: dict[str, dict] = {}
    for member_id, name, zone_raw, round_raw, global_raw in rows:
        try:
            zone = json.loads(zone_raw or "{}") or {}
            rounds = json.loads(round_raw or "{}") or {}
            totals = json.loads(global_raw or "{}") or {}
        except (TypeError, ValueError):
            continue
        powers = {int(d): int(v.get("power") or 0) for d, v in rounds.items()}
        top = max(powers.values(), default=0)
        full_days = 0
        for d, v in rounds.items():
            day = days.setdefault(int(d), {"day": int(d), "points": 0, "deploy": 0, "waves": 0, "attempts": 0,
                                           "covert": 0, "units": 0, "disobey": 0, "scored": 0, "full": 0})
            summary = int(v.get("summary") or 0)
            day["points"] += summary
            day["deploy"] += int(v.get("power") or 0)
            day["waves"] += int(v.get("strike_encounter") or 0)
            day["attempts"] += int(v.get("strike_attempt") or 0)
            day["covert"] += int(v.get("covert_attempt") or 0)
            day["units"] += int(v.get("unit_donated") or 0)
            day["disobey"] += int(v.get("disobey") or 0)
            day["scored"] += summary > 0
            if top and powers[int(d)] >= FULL_DEPLOY * top:
                day["full"] += 1
                full_days += 1
        players[member_id] = {
            "full_days": full_days, "days": len(rounds),
            "deploy": int(totals.get("power") or 0),
            "battle": int(totals.get("summary") or 0) - int(totals.get("power") or 0),
            "disobey": int(totals.get("disobey") or 0),
            "covert_wins": int(totals.get("covert_complete") or 0),
        }
        for phase, confs in zone.items():
            for conflict, acts in confs.items():
                planet, _label = tb_missions.zone_planet(phase, conflict)
                key = (int(phase), conflict)
                p = planets.setdefault(key, {"phase": int(phase), "conflict": conflict, "planet": planet,
                                             "bonus": conflict.endswith("_bonus"), "player_points": 0,
                                             "deploy": 0, "waves": 0, "attempts": 0, "covert_wins": 0,
                                             "units": 0, "players": []})
                pts, units = _num(acts, "summary"), _num(acts, "unit_donated")
                waves, attempts = _num(acts, "strike_encounter"), _num(acts, "strike_attempt")
                p["player_points"] += pts
                p["deploy"] += _num(acts, "power")
                p["waves"] += waves
                p["attempts"] += attempts
                p["covert_wins"] += _num(acts, "covert_complete")
                p["units"] += units
                if pts or units:
                    p["players"].append({"name": name, "points": pts, "deploy": _num(acts, "power"),
                                         "waves": waves, "attempts": attempts, "units": units})

    member_count = len(players)
    planet_list = []
    for key in sorted(planets, key=lambda k: (k[0], k[1].endswith("_bonus"),
                                              tb_missions._SIDE_ORDER.index(tb_missions._SIDE_BY_CONFLICT.get(k[1][:2], "light")))):
        p = planets[key]
        info = zones.get(p["planet"]) or {}
        ops = min(OPERATIONS, p["units"] // UNITS_PER_OPERATION)
        p["operations"] = ops
        p["platoon_points"] = ops * int(info.get("op_points") or 0)
        p["points"] = p["player_points"] + p["platoon_points"]
        thresholds = info.get("stars") or []
        p["max_stars"] = len(thresholds)
        p["stars"], p["to_next"] = _stars(p["points"], thresholds)
        p["thresholds"] = thresholds
        wz = waves_by_zone.get(f"{p['phase']}/{p['conflict']}") or {}
        p["waves_max"] = (wz.get("waves") or 0) * member_count
        p["battles"] = wz.get("battles") or 0
        p["players"].sort(key=lambda x: -x["points"])
        planet_list.append(p)

    day_list = [days[d] for d in sorted(days)]
    for d in day_list:
        d["battle"] = d["points"] - d["deploy"]
        d["members"] = member_count
        d["planets"] = [p for p in planet_list if p["phase"] == d["day"]]
    totals = {
        "stars": sum(p["stars"] for p in planet_list),
        "points": sum(d["points"] for d in day_list),
        "deploy": sum(d["deploy"] for d in day_list),
        "battle": sum(d["battle"] for d in day_list),
        "waves": sum(d["waves"] for d in day_list),
        "attempts": sum(d["attempts"] for d in day_list),
        "covert": sum(d["covert"] for d in day_list),
        "covert_wins": sum(p["covert_wins"] for p in planet_list),
        "units": sum(d["units"] for d in day_list),
        "disobey": sum(d["disobey"] for d in day_list),
        "members": member_count,
    }
    return {"days": day_list, "planets": planet_list, "players": players, "totals": totals}
