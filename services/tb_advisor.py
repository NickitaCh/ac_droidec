"""«Докачать для взводов» — что игроку докачать и открыть, чтобы закрыть ДЫРЫ гильдии во
взводах «Восхода Империи». Виджет карточки игрока /player/<code>.

Первая версия (2026-10-28, копия «ВИ-советника» swgoh.ru) смотрела только на склад игрока и
выдавала ~200 юнитов, хотя у гильдии их и так хватает (жалоба пользователя 28.10). Теперь
по каждому юниту и этапу считаем, сколько копий нужно во взводах и сколько игроков гильдии
могут их поставить, и советуем только там, где гильдии не хватает (или запас ≤ TIGHT_SPARE).

Раскладка — стандартная из tb_platoon_data.py (как у /tb/plan и конструктора взводов), не
активный ордер: советник про прокачку вперёд. Этап планеты — её место в ветке
(services/tb_projection.py). Юнит игрока встаёт во взвод один раз за этап, на какой бы
планете ни был слот. Персонажу нужна реликвия планеты, кораблю — 7★. Слоты этапа
раздаются жадно: самые сильные игроки — на самые трудные слоты, бонусные планеты —
в последнюю очередь (при пороговых требованиях так получается максимум закрытых слотов)."""

import time
from collections import Counter

import database
import tb_platoon_data
from services import tb_covert, tb_projection, unit_forecast
from tb_platoon_engine import SHIP_MIN_STARS

TIGHT_SPARE = 1  # свободных игроков сверх нужного не больше этого — «впритык»
CACHE_SECONDS = 900
_guild_cache: dict[int, tuple[float, dict]] = {}


def _demand() -> tuple[dict, dict]:
    """({base_id: {этап: [(реликвия, бонус, планета), …] — по слоту на копию}}, {base_id: тип})."""
    by_name: dict[str, dict[int, list]] = {}
    for phase in range(1, 7):
        for planet in tb_projection.phase_planets(phase, with_bonus=True):
            relic = tb_platoon_data.ROTE_MIN_RELIC_BY_PLANET.get(planet, 5)
            bonus = planet in tb_projection.BONUS_PHASE
            for op in tb_projection.OPERATIONS:
                for display in tb_platoon_data.ROTE_PLATOON_SUGGESTIONS.get((planet, op)) or []:
                    by_name.setdefault(display, {}).setdefault(phase, []).append((relic, bonus, planet))
    name_to_base_id = database.resolve_unit_display_names(sorted(by_name))
    demand: dict[str, dict[int, list]] = {}
    for display, phases in by_name.items():
        bid = name_to_base_id.get(display)
        if not bid:
            continue
        for phase, slots in phases.items():
            demand.setdefault(bid, {}).setdefault(phase, []).extend(slots)
    types = database.get_unit_types(list(demand))
    for bid, phases in demand.items():
        if types.get(bid) == "ship":  # кораблю нужны только 7★ — уровень 0, см. _level_value
            for phase in phases:
                phases[phase] = [(0, bonus, planet) for _r, bonus, planet in phases[phase]]
    return demand, types


def _level_value(unit: dict | None, is_ship: bool) -> int:
    """Число для сравнения со слотом: реликвия персонажа (−1 — не 7★/без реликвии);
    у корабля 0 — 7★, −1 — ниже. Слот корабля требует 0."""
    if unit is None:
        return -1
    level = unit_forecast.current_level(unit)
    if is_ship:
        return 0 if level["stars"] >= SHIP_MIN_STARS else -1
    return level["relic"] if level["stars"] >= 7 and level["relic"] is not None else -1


def _guild_levels(guild_id: int, demand: dict, types: dict) -> dict:
    """{ally_code: {base_id: уровень}} по юнитам взводов для текущего состава (кэш 15 мин)."""
    cached = _guild_cache.get(guild_id)
    if cached and time.time() - cached[0] < CACHE_SECONDS:
        return cached[1]
    codes = [code for code, _name in database.get_roster_by_player_id(guild_id).values() if code]
    levels: dict[str, dict[str, int]] = {}
    for i in range(0, len(codes), 20):
        for code, roster in database.get_player_units_bulk(codes[i:i + 20]).items():
            levels[code] = {bid: _level_value(roster.get(bid), types.get(bid) == "ship")
                            for bid in demand if bid in roster}
    _guild_cache[guild_id] = (time.time(), levels)
    return levels


def _fill(slots: list, players: list) -> tuple[list, int]:
    """Жадная раздача: слоты (реликвия, бонус, планета) — обычные раньше бонусных, трудные
    раньше лёгких; игроки — уровни по убыванию. (незакрытые слоты, свободные игроки, которые
    подошли бы к самому лёгкому слоту)."""
    order = sorted(slots, key=lambda s: (s[1], -s[0]))
    pool = sorted(players, reverse=True)
    unfilled = []
    for slot in order:
        idx = next((i for i in range(len(pool) - 1, -1, -1) if pool[i] >= slot[0]), None)
        # берём самого слабого из подходящих — сильные остаются для трудных слотов
        if idx is None:
            unfilled.append(slot)
        else:
            pool.pop(idx)
    easiest = min(s[0] for s in slots)
    return unfilled, sum(1 for lvl in pool if lvl >= easiest)


def _level_text(unit: dict, is_ship: bool) -> str:
    level = unit_forecast.current_level(unit)
    if is_ship or level["stars"] < 7:
        return f"{level['stars']}★" + ("" if is_ship else f" G{level['gear']}")
    return f"R{level['relic']}" if level["relic"] is not None else f"G{level['gear']}"


def build(guild_id: int, ally_code: str, roster: dict) -> dict:
    """{members, short_total, phases, phase_counts: {вкладка: {этап: юнитов}}, upgrade: [...], unlock: [...],
    tight: [...]}. Элемент — {base_id, name, is_ship, now, target, relics, cost, phases: [{phase, need,
    planets, demand, have, short, bonus, main}]}; main=False — этап «впритык» у юнита из «докачать/открыть». В «докачать/открыть» — юниты, где гильдии не хватает копий и игрок
    ещё не подходит; в «впритык» — где хватает с запасом ≤ TIGHT_SPARE и игрок не подходит."""
    demand, types = _demand()
    guild = _guild_levels(guild_id, demand, types)
    others = {code: lv for code, lv in guild.items() if code != ally_code}
    names = database.get_game_unit_names(list(demand))

    upgrade, unlock, tight = [], [], []
    short_units: set = set()
    for bid, phases in demand.items():
        is_ship = types.get(bid) == "ship"
        unit = roster.get(bid)
        mine = _level_value(unit, is_ship)
        short_rows, tight_rows = [], []
        for phase in sorted(phases):
            slots = phases[phase]
            players = [lv[bid] for lv in others.values() if bid in lv]
            if mine >= 0:
                players.append(mine)
            unfilled, spare = _fill(slots, players)
            if unfilled:
                short_units.add(bid)
            need = min(s[0] for s in (unfilled or slots))
            if mine >= need:
                continue  # игрок уже закрывает этот этап (или его уровень уже учтён)
            rows_planets = Counter(s[2] for s in (unfilled or slots) if s[0] == need)
            row = {"phase": phase, "need": need, "demand": len(slots),
                   "have": len(slots) - len(unfilled), "short": len(unfilled),
                   "planets": [tb_covert.planet_ru(p) for p in rows_planets],
                   "bonus": all(s[1] for s in (unfilled or slots))}
            if unfilled:
                short_rows.append(row)
            elif spare <= TIGHT_SPARE:
                row["spare"] = spare
                tight_rows.append(row)
        if not short_rows and not tight_rows:
            continue
        # в карточке — все этапы юнита (нехватка и впритык), цель и вкладка — по нехватке, если она есть
        main_rows = short_rows or tight_rows
        rows = sorted(short_rows + tight_rows, key=lambda r: r["phase"])
        for r in rows:
            r["main"] = r in main_rows
        target = min(r["need"] for r in main_rows)
        item = {"base_id": bid, "name": names.get(bid, bid), "is_ship": is_ship, "phases": rows,
                "only_bonus": all(r["bonus"] for r in main_rows),
                "short": sum(r["short"] for r in rows),
                "target": f"{SHIP_MIN_STARS}★" if is_ship else f"R{target}",
                "closes": sum(1 for r in main_rows if r["need"] <= target)}
        # cost — для сортировки: реликвий до цели (без реликвии — ещё +1 за гир), не открыт — в конец
        item["cost"] = 99
        if unit is not None:
            item["now"] = _level_text(unit, is_ship)
            relic = unit_forecast.current_level(unit)["relic"]
            item["relics"] = target - relic if not is_ship and relic is not None and relic < target else None
            item["cost"] = 0 if is_ship else item["relics"] or (target + 1 if relic is None else 0)
        if not short_rows:
            tight.append(item)
        elif unit is None:
            unlock.append(item)
        else:
            upgrade.append(item)
    # сверху — ближе всего докачать, потом ранние этапы (их гильдия проходит каждую ВИ)
    key = lambda x: (x["only_bonus"], x["cost"], min(r["phase"] for r in x["phases"] if r["main"]), -x["short"], x["name"].lower())
    for lst in (upgrade, unlock, tight):
        lst.sort(key=key)
    # сколько юнитов на каждом этапе — отдельно для каждой вкладки (кнопки этапов показывают счёт открытой)
    phase_counts = {kind: Counter(r["phase"] for x in lst for r in x["phases"] if r["main"])
                    for kind, lst in (("upgrade", upgrade), ("unlock", unlock), ("tight", tight))}
    phases = sorted(set().union(*phase_counts.values()))
    return {"members": len(guild), "short_total": len(short_units), "phases": phases, "phase_counts": phase_counts,
            "upgrade": upgrade, "unlock": unlock, "tight": tight}
