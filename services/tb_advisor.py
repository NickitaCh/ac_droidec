"""ВИ-советник игрока — что докачать и открыть, чтобы закрывать взводы «Восхода Империи»
(идея swgoh.ru «ВИ-советник», 2026-10-28). Виджет карточки игрока /player/<code>.

Раскладка — стандартная из tb_platoon_data.py (как у /tb/plan и конструктора взводов),
не активный ордер: советник про прокачку вперёд, а не про текущую ВИ. Этап планеты —
её место в ветке (services/tb_projection.py). Юнит игрока встаёт во взвод один раз за
этап, поэтому этап закрыт, если уровня хватает хотя бы на одну планету этапа, где юнит
нужен (на самую лёгкую). Персонажу нужна реликвия планеты, кораблю — 7★."""

import database
import tb_platoon_data
from services import tb_covert, tb_projection, unit_forecast
from tb_platoon_engine import SHIP_MIN_STARS


def _unit_phases() -> dict[str, dict[int, list[tuple[str, int, bool]]]]:
    """{отображаемое имя: {этап: [(планета, мин. реликвия, бонус)]}} по стандартной раскладке."""
    result: dict[str, dict[int, list]] = {}
    for phase in range(1, 7):
        for planet in tb_projection.phase_planets(phase, with_bonus=True):
            min_relic = tb_platoon_data.ROTE_MIN_RELIC_BY_PLANET.get(planet, 5)
            bonus = planet in tb_projection.BONUS_PHASE
            for op in tb_projection.OPERATIONS:
                for display in tb_platoon_data.ROTE_PLATOON_SUGGESTIONS.get((planet, op)) or []:
                    rows = result.setdefault(display, {}).setdefault(phase, [])
                    if not any(r[0] == planet for r in rows):
                        rows.append((planet, min_relic, bonus))
    return result


def _meets(level: dict, min_relic: int, is_ship: bool) -> bool:
    if is_ship:
        return level["stars"] >= SHIP_MIN_STARS
    return level["stars"] >= 7 and level["relic"] is not None and level["relic"] >= min_relic


def _level_text(level: dict, is_ship: bool) -> str:
    if is_ship or level["stars"] < 7:
        return f"{level['stars']}★" + ("" if is_ship else f" G{level['gear']}")
    return f"R{level['relic']}" if level["relic"] is not None else f"G{level['gear']}"


def build(ally_code: str, roster: dict) -> dict:
    """{total, covered, upgrade: [...], unlock: [...]}; элемент — {base_id, name, is_ship,
    now, target, phases: [{phase, need, planets, bonus, met}], open}. roster — {base_id: rosterUnit}."""
    by_name = _unit_phases()
    name_to_base_id = database.resolve_unit_display_names(sorted(by_name))
    by_unit: dict[str, dict[int, list]] = {}
    for display, phases in by_name.items():
        bid = name_to_base_id.get(display)
        if not bid:
            continue
        merged = by_unit.setdefault(bid, {})
        for phase, rows in phases.items():
            known = merged.setdefault(phase, [])
            known += [r for r in rows if r not in known]
    types = database.get_unit_types(list(by_unit))
    names = database.get_game_unit_names(list(by_unit))

    covered, upgrade, unlock = 0, [], []
    for bid, phases in by_unit.items():
        is_ship = types.get(bid) == "ship"
        unit = roster.get(bid)
        level = unit_forecast.current_level(unit) if unit is not None else None
        rows = []
        for phase in sorted(phases):
            planets = phases[phase]
            need = min(r[1] for r in planets)
            easiest = [r for r in planets if r[1] == need]
            rows.append({
                "phase": phase, "need": need,
                "planets": [tb_covert.planet_ru(r[0]) for r in easiest],
                "bonus": all(r[2] for r in easiest),
                "met": level is not None and _meets(level, need, is_ship),
            })
        open_rows = [r for r in rows if not r["met"]]
        if not open_rows:
            covered += 1
            continue
        item = {"base_id": bid, "name": names.get(bid, bid), "is_ship": is_ship, "phases": rows,
                "open": len(open_rows), "only_bonus": all(r["bonus"] for r in open_rows)}
        if level is None:
            item["target"] = "7★" if is_ship else f"R{min(r['need'] for r in open_rows)}"
            unlock.append(item)
            continue
        item["now"] = _level_text(level, is_ship)
        if is_ship:
            item["target"] = f"{SHIP_MIN_STARS}★"
        else:
            target = min(r["need"] for r in open_rows)
            item["target"] = f"R{target}"
            item["closes"] = sum(1 for r in open_rows if r["need"] <= target)
            item["relics"] = target - level["relic"] if level["relic"] is not None else None
        upgrade.append(item)
    upgrade.sort(key=lambda x: (x["only_bonus"], -x["open"], x.get("relics") or 0, x["name"].lower()))
    unlock.sort(key=lambda x: (x["only_bonus"], -x["open"], x["name"].lower()))
    return {"total": len(by_unit), "covered": covered, "upgrade": upgrade, "unlock": unlock}
