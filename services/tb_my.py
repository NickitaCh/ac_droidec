"""«Мой ВИ» в карточке игрока (как My TB в HotUtils, решение пользователя 2026-10-27): по
каждому этапу активного ордера — каких юнитов игрок ставит во взводы и может ли он пройти
боевые миссии и спецмиссии планет этапа без этих юнитов.

Планеты этапов и взводы — из активного ордера (guilds.tb_active_plan_id → тред ордера,
tb_plan_reader.fetch_plan_planets; назначения — tb_platoon_assignments, этап = round_num,
как в рассылке взводов tb_platoon_notify). Юниты во взводах заняты до конца этапа, поэтому
готовность к боям считаем по ростеру без них. Отрядов под бой в игровых данных нет, поэтому
«кем» — годные юниты игрока из списка боя (у боёв «любой персонаж» — без списка)."""

import database
import tb_plan_reader
import tb_platoon_data
from services import tb_covert, tb_forecast


async def load_order(guild_id: int) -> tuple[dict | None, list, str | None]:
    """(ордер, планеты этапов, ошибка) — сетевую часть делаем в async, расчёт — в build()."""
    cfg = database.get_guild_config(guild_id) or {}
    active_id = cfg.get("tb_active_plan_id")
    if not active_id:
        return None, [], "Активного ордера ВИ нет — офицеры публикуют его на вкладке «План»."
    order = database.get_tb_saved_plan(int(active_id))
    if not order or order["guild_id"] != guild_id:
        return None, [], "Активный ордер ВИ не найден."
    entries, error = await tb_plan_reader.fetch_plan_planets(order)
    return order, entries, error


def _rounds(entries: list) -> dict:
    """{этап: [(планета, без взводов)]} в порядке ордера, без повторов."""
    out: dict = {}
    for e in entries:
        if not e.get("planet"):
            continue
        planets = out.setdefault(e["round"], [])
        if all(p != e["planet"] for p, _np in planets):
            planets.append((e["planet"], bool(e.get("no_platoons"))))
    return dict(sorted(out.items()))


def _platoons(guild_id: int, order_id: int, ally_code: str) -> dict:
    """{этап: [{planet, operation, base_id}]} — что игрок ставит во взводы этапа."""
    assignments = database.get_tb_platoon_assignments(guild_id, order_id)
    mine = [(k, a) for k, a in assignments.items() if a["ally_code"] == ally_code and a.get("round_num")]
    names = {n for (planet, op, _i), _a in mine for n in tb_platoon_data.ROTE_PLATOON_SUGGESTIONS.get((planet, op)) or []}
    to_base = database.resolve_unit_display_names(sorted(names)) if names else {}
    out: dict = {}
    for (planet, op, idx), a in mine:
        units = tb_platoon_data.ROTE_PLATOON_SUGGESTIONS.get((planet, op)) or []
        if idx >= len(units):
            continue
        out.setdefault(a["round_num"], []).append({"planet": planet, "operation": op,
                                                   "base_id": to_base.get(units[idx]), "en": units[idx]})
    for rows in out.values():
        rows.sort(key=lambda r: (r["planet"], r["operation"]))
    return out


def _strike_row(m: dict, levels: dict, free: dict, ships: set, donated: set, ru: dict) -> dict:
    status = tb_forecast.strike_status(m, free, ships)
    level = f"{m['min_stars']}★" if m["fleet"] else f"R{m['min_relic']}"
    must = ", ".join(ru.get(b, b) for b in m["mandatory"])
    if m["mandatory"] and m["count"] <= len(m["mandatory"]):
        label = f"{must} {level}"
    else:
        label = f"{'флот' if m['fleet'] else m['label']} {level} ×{m['count']}" + (f" · {must}" if must else "")
    row = {"kind": "strike", "n": m["n"], "fleet": m["fleet"], "status": status, "label": label,
           "units": [], "blocked": [], "gap": ""}
    if status == "ready":
        # Отряд — обязательные + сильнейшие из годных (реликвия, звёзды), остальные — числом
        ok = tb_forecast.qualifying_units(m, free, ships)
        ok.sort(key=lambda b: (b not in m["mandatory"], -(free[b][1] or -1), -free[b][0]))
        team = ok[:max(m["count"], len(m["mandatory"]))]
        row["units"] = [ru.get(b, b) for b in team]
        row["more"] = len(ok) - len(team)
    elif tb_forecast.strike_status(m, levels, ships) == "ready":
        # без взводов прошёл бы: бой ломают юниты, отданные во взводы этого этапа
        row["status"] = "blocked"
        row["blocked"] = [ru.get(b, b) for b in sorted(donated & set(tb_forecast.qualifying_units(m, levels, ships)))]
    elif status == "short":
        gap, close = tb_forecast.strike_gap(m, free, ships)
        row["status"] = "step" if close else "short"
        row["gap"] = ", ".join(f"{ru.get(b, b)} {label}" for b, label in gap)
    return row


def build(guild_id: int, ally_code: str, order: dict, entries: list) -> dict:
    """{order_name, rounds: [{round, planets: [{planet, ru, no_platoons, platoons, missions}],
    donated_total}]} — блокирующая (sqlite), звать через to_thread."""
    roster = database.get_player_units(ally_code)
    if not roster:
        return {"error": "Ростер игрока ещё не синхронизирован."}
    rounds = _rounds(entries)
    platoons = _platoons(guild_id, order["id"], ally_code)
    ships = {b for b, t in database.get_unit_types(list(roster)).items() if t == "ship"}
    levels = tb_forecast.roster_levels(roster)
    strikes = tb_forecast.strike_missions()
    ids = set(roster) | {p["base_id"] for rows in platoons.values() for p in rows if p["base_id"]}
    ids |= {b for m in strikes for b in m["mandatory"]}
    ru = database.get_game_unit_names(sorted(ids))
    out = []
    for rnd, planets in rounds.items():
        mine = platoons.get(rnd) or []
        donated = {p["base_id"] for p in mine if p["base_id"]}
        free = {b: lv for b, lv in levels.items() if b not in donated}
        free_roster = {b: u for b, u in roster.items() if b not in donated}
        rows = []
        for planet, no_platoons in planets:
            missions = [_strike_row(m, levels, free, ships, donated, ru)
                        for m in strikes if m["planet"] == planet]
            for n, m in enumerate(tb_covert.missions(planet=planet), 1):
                st = tb_covert.player_status(m, free_roster, ships)
                blocked = []
                if st["status"] != "ready" and tb_covert.player_status(m, roster, ships)["status"] == "ready":
                    st = {**st, "status": "blocked"}
                    blocked = [ru.get(b, b) for b in sorted(donated & set(m["units"] or m["mandatory"] or ()))]
                who = ", ".join(ru.get(b, b) for b in m["mandatory"])
                missions.append({"kind": "covert", "n": n, "status": st["status"], "text": st["text"],
                                 "label": f"{who} {tb_covert.mission_label(m)}".strip(), "blocked": blocked,
                                 "reward": tb_covert.reward_compact(m)})
            rows.append({
                "planet": planet, "ru": tb_covert.planet_ru(planet), "no_platoons": no_platoons,
                "platoons": [{"operation": p["operation"], "name": ru.get(p["base_id"], p["en"]) if p["base_id"] else p["en"]}
                             for p in mine if p["planet"] == planet],
                "missions": missions,
            })
        out.append({"round": rnd, "planets": rows, "platoons_total": len(mine),
                    "ready": sum(m["status"] == "ready" for r in rows for m in r["missions"]),
                    "blocked": sum(m["status"] == "blocked" for r in rows for m in r["missions"]),
                    "missions": sum(len(r["missions"]) for r in rows)})
    return {"order_name": order["name"], "rounds": out, "error": None}
