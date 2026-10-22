"""ВИ-проекция «Восхода Империи», часть 2 (веб /tb/projection): боевые миссии (кто
может зайти в каждый бой) и прогноз звёзд на всю битву.

Всё из игровых данных t05D, без входа в аккаунт (проверено 2026-10-22), пишет units_sync:
- пороги звёзд планеты — conflictZoneDefinition[].victoryPointRewards[].galacticScoreRequirement;
- очки за операцию взвода (15/15) — reconZoneDefinition[].platoonDefinition[].reward.value;
- бонусные планеты — conflictZoneDefinition (isBonus).zoneDefinition.unlockRequirement:
  тип 61 — звезда на планете перед ней, тип 116 — сколько побед в спецмиссии (Зеффо — 30 на
  Бракке, Мандалор — 25 мандалорской на Татуине);
- боевые миссии — strikeZoneDefinition → миссия кампании t05D → entryCategoryAllowed (как у
  спецмиссий: категории «любой из», сколько юнитов, сырой тир реликвии −2, обязательные).
Очков за бой в данных нет (maxPointsPerAttempt = 0) — их берём из истории наших ВИ:
у игрока за этап summary − power = очки боёв (power — залитая ГМ)."""

import json
import re

import database
from services import tb_covert
from services.tb_projection import BRANCHES

ZONES_STATE = "tb_zone_data"          # bot_state guild 0: {"planets": {...}, "bonus": {...}}
STRIKES_STATE = "tb_strike_missions"  # bot_state guild 0: [mission, ...]
HISTORY_EVENTS = 3                    # сколько последних ВИ берём для очков боёв и доли залива
GP_STAT = "STAT_GALACTIC_POWER_ACQUIRED_NAME"
_ZONE_RE = re.compile(r"phase0?(\d)_conflict0?(\d)(_bonus)?")
_SIDE_BY_CONFLICT = {1: "light", 2: "dark", 3: "mixed"}
_BONUS_PLANET = {"light": "Zeffo", "mixed": "Mandalore"}
_ALIGNMENTS = {"alignment_light": "светлые", "alignment_dark": "тёмные", "alignment_neutral": "нейтральные"}


def planet_of(zone_id: str) -> tuple[int, str, bool] | None:
    """(этап, планета, бонусная) по zoneId вида tb3_mixed_phase03_conflict01_bonus…"""
    m = _ZONE_RE.search(zone_id or "")
    if not m:
        return None
    phase, side, bonus = int(m.group(1)), _SIDE_BY_CONFLICT.get(int(m.group(2))), bool(m.group(3))
    if side is None:
        return None
    return phase, (_BONUS_PLANET[side] if bonus else BRANCHES[side][phase - 1]), bonus


# ------------------------------------------------------------------ разбор игровых данных

def _missions_by_id(campaigns: list) -> dict:
    camp = next((c for c in campaigns if c.get("id") == tb_covert.TB_ID), None) or {}
    out = {}
    for cmap in camp.get("campaignMap") or []:
        for group in cmap.get("campaignNodeDifficultyGroup") or []:
            for node in group.get("campaignNode") or []:
                for mission in node.get("campaignNodeMission") or []:
                    out[mission.get("id")] = mission
    return out


def parse_zones(tb_defs: list) -> dict:
    """{"planets": {планета: {phase, side, bonus, stars: [3 порога], op_points}},
    "bonus": {бонусная планета: {after: планета, covert_zone, wins}}}."""
    tb = next((t for t in tb_defs if t.get("id") == tb_covert.TB_ID), None)
    if not tb:
        return {}
    planets, bonus = {}, {}
    zone_planet = {}
    for z in tb.get("conflictZoneDefinition") or []:
        zone_id = (z.get("zoneDefinition") or {}).get("zoneId") or ""
        where = planet_of(zone_id)
        if not where:
            continue
        phase, planet, is_bonus = where
        zone_planet[zone_id] = planet
        planets[planet] = {"phase": phase, "bonus": is_bonus,
                           "stars": [int(v.get("galacticScoreRequirement") or 0) for v in z.get("victoryPointRewards") or []],
                           "op_points": 0}
    for z in tb.get("conflictZoneDefinition") or []:
        zone_def = z.get("zoneDefinition") or {}
        if not z.get("isBonus"):
            continue
        planet = zone_planet.get(zone_def.get("zoneId"))
        items = ((zone_def.get("unlockRequirement") or {}).get("requirementItem")) or []
        after = next((zone_planet.get(i.get("id")) for i in items if i.get("type") == 61), None)
        covert = next((i for i in items if i.get("type") == 116), None)
        if planet and after:
            bonus[planet] = {"after": after, "covert_zone": covert.get("id") if covert else None,
                             "wins": int(covert.get("value") or 0) if covert else 0}
    for r in tb.get("reconZoneDefinition") or []:
        where = planet_of((r.get("zoneDefinition") or {}).get("zoneId") or "")
        platoons = r.get("platoonDefinition") or []
        if where and where[1] in planets and platoons:
            planets[where[1]]["op_points"] = int((platoons[0].get("reward") or {}).get("value") or 0)
    return {"planets": planets, "bonus": bonus}


def parse_strike_missions(tb_defs: list, campaigns: list, units: list, category_names: dict) -> list[dict]:
    """[{zone_id, phase, planet, n, fleet, mandatory, units|None, count, min_relic, label}] —
    units None значит «любой персонаж» (или любой корабль у флота)."""
    tb = next((t for t in tb_defs if t.get("id") == tb_covert.TB_ID), None)
    if not tb:
        return []
    missions = _missions_by_id(campaigns)
    by_category: dict[tuple, set] = {}
    count_by_type: dict[int, int] = {}
    for u in units:
        ct = u.get("combatType")
        count_by_type[ct] = count_by_type.get(ct, 0) + 1
        for cat in u.get("categoryId") or []:
            by_category.setdefault((ct, cat), set()).add(u["baseId"])
    out = []
    numbers: dict[str, int] = {}
    for zone in tb.get("strikeZoneDefinition") or []:
        zone_id = (zone.get("zoneDefinition") or {}).get("zoneId") or ""
        where = planet_of(zone_id)
        mission = missions.get((zone.get("campaignElementIdentifier") or {}).get("campaignMissionId"))
        if not where or not mission:
            continue
        phase, planet, _bonus = where
        fleet = zone.get("combatType") == 2
        ct = 2 if fleet else 1
        entry = mission.get("entryCategoryAllowed") or {}
        cats = entry.get("categoryId") or []
        mandatory = [r["id"] for r in entry.get("mandatoryRosterUnit") or [] if r.get("id")]
        pool = set()
        for cat in cats:
            pool |= by_category.get((ct, cat), set())
        relic_tier = entry.get("minimumRelicTier") or 0
        numbers[planet] = numbers.get(planet, 0) + 1
        any_unit = len(pool) >= 0.9 * count_by_type.get(ct, 1)
        names = [category_names.get(c) or _ALIGNMENTS.get(c) or c for c in cats]
        if set(cats) >= set(_ALIGNMENTS) or (fleet and set(cats) >= {"alignment_light", "alignment_dark"}):
            names = ["любые"]
        out.append({
            "zone_id": zone_id, "phase": phase, "planet": planet, "n": numbers[planet], "fleet": fleet,
            "mandatory": mandatory, "units": None if any_unit else sorted(pool),
            "count": entry.get("minimumRequiredUnitQuantity") or 1,
            "min_relic": None if fleet or relic_tier < 3 else relic_tier - 2,
            "min_stars": min(entry.get("minimumUnitRarity") or 7, 7),
            "label": " / ".join(names),
        })
    return out


def zones() -> dict:
    raw = database.get_bot_state(ZONES_STATE, guild_id=0)
    return json.loads(raw) if raw else {}


def strike_missions() -> list[dict]:
    raw = database.get_bot_state(STRIKES_STATE, guild_id=0)
    return json.loads(raw) if raw else []


# ------------------------------------------------------------------ боевые миссии

def _ok(level: tuple, m: dict) -> bool:
    stars, relic = level
    if stars < m["min_stars"]:
        return False
    return m["min_relic"] is None or (relic is not None and relic >= m["min_relic"])


def roster_levels(roster: dict) -> dict:
    """{base_id: (звёзды, реликвия|None)} — уровни считаем один раз на игрока, а не на бой."""
    from services import unit_forecast
    out = {}
    for b, unit in roster.items():
        lv = unit_forecast.current_level(unit)
        out[b] = (lv["stars"], lv["relic"])
    return out


def strike_status(m: dict, levels: dict, ships: set) -> str:
    """levels — roster_levels игрока. ready — все обязательные на пороге и подходящих
    юнитов хватает; short — юниты есть, но не дотягивают; missing — нужных юнитов нет."""
    if any(b not in levels for b in m["mandatory"]):
        return "missing"
    pool = m["units"] if m["units"] is not None else [b for b in levels if (b in ships) == m["fleet"]]
    owned = {b for b in pool if b in levels} | set(m["mandatory"])
    ok = {b for b in owned if _ok(levels[b], m)}
    if all(b in ok for b in m["mandatory"]) and len(ok) >= m["count"]:
        return "ready"
    return "short" if len(owned) >= m["count"] else "missing"


def strikes_by_phase(rosters: dict, names: dict, with_bonus: bool) -> dict:
    """{этап: [{planet, n, fleet, label, requirement, ready: [ники], short: [ники]}]}."""
    ms = strike_missions()
    all_ids = {b for r in rosters.values() for b in r}
    ships = {b for b, t in database.get_unit_types(list(all_ids)).items() if t == "ship"}
    unit_names = database.get_game_unit_names(sorted({b for m in ms for b in m["mandatory"]}))
    levels = {code: roster_levels(roster) for code, roster in rosters.items()}
    out: dict[int, list] = {}
    for m in ms:
        if m["planet"] in _BONUS_PLANET.values() and not with_bonus:
            continue
        ready, short = [], []
        for code, lv in levels.items():
            st = strike_status(m, lv, ships)
            if st == "ready":
                ready.append(names.get(code, code))
            elif st == "short":
                short.append(names.get(code, code))
        level = f"{m['min_stars']}★" if m["fleet"] else f"R{m['min_relic']}"
        must = ", ".join(unit_names.get(b, b) for b in m["mandatory"])
        if m["mandatory"] and m["count"] <= len(m["mandatory"]):
            req = f"{must} {level}"
        else:
            req = f"{'флот' if m['fleet'] else m['label']} {level} ×{m['count']}" + (f" · {must}" if must else "")
        out.setdefault(m["phase"], []).append({
            "planet": m["planet"], "n": m["n"], "fleet": m["fleet"], "requirement": req,
            "ready": sorted(ready, key=str.lower), "short": sorted(short, key=str.lower),
        })
    return out


# ------------------------------------------------------------------ прогноз звёзд

def history(guild_id: int) -> dict:
    """По последним ВИ: {этап: {"battle": средние очки боёв гильдии, "share": доля ГМ, которую
    гильдия залила}} и stars — звёзды последней ВИ."""
    events = database.get_recent_tb_events(HISTORY_EVENTS, guild_id=guild_id)
    stars = database.get_tb_event_stars([eid for eid, _at in events])
    per_phase: dict[str, list] = {}
    for eid, _at in events:
        rows = database.get_tb_round_totals(eid)
        battle, power, full = {}, {}, 0
        for totals in rows:
            full += max((v.get("power") or 0) for v in totals.values()) if totals else 0
            for ph, v in totals.items():
                battle[ph] = battle.get(ph, 0) + max(0, (v.get("summary") or 0) - (v.get("power") or 0))
                power[ph] = power.get(ph, 0) + (v.get("power") or 0)
        for ph in power:
            per_phase.setdefault(ph, []).append((battle.get(ph, 0), power[ph] / full if full else 0))
    return {
        "phases": {int(ph): {"battle": sum(b for b, _s in v) / len(v), "share": sum(s for _b, s in v) / len(v)}
                   for ph, v in per_phase.items()},
        "events": len(events), "last_stars": stars.get(events[-1][0]) if events else None,
    }


def guild_gp(ally_codes: list) -> int:
    profiles = database.get_player_profiles_bulk(ally_codes)
    return sum(int((p.get("stats") or {}).get(GP_STAT) or 0) for p in profiles.values())


def forecast(zone_data: dict, platoon_points: dict, covert_ready: dict, hist: dict, gp: int) -> dict:
    """Звёзды по этапам. platoon_points — {планета: очки закрытых операций}, covert_ready —
    {zone_id спецмиссии: сколько игроков её проходят}. Модель: на каждом этапе гильдия
    заливает долю ГМ, как в прошлых ВИ, и набирает столько же очков боёв; эти очки жадно
    раскладываются на самые дешёвые звёзды открытых планет. Планета со звездой закрывается
    и открывает следующую в ветке, без звезды — остаётся с набранными очками (прелоад).
    Остаток очков уходит в незакрытую планету, ближе всех к звезде."""
    planets = zone_data.get("planets") or {}
    bonus = zone_data.get("bonus") or {}
    progress = {p: 0 for p in planets}
    stars: dict[str, int] = {}
    open_now = [b[0] for b in BRANCHES.values()]
    platoons_done: set = set()
    phases = []
    for phase in range(1, 7):
        h = (hist.get("phases") or {}).get(phase) or {"battle": 0, "share": 0.9}
        deploy, battle = gp * h["share"], h["battle"]
        budget = deploy + battle
        active = [p for p in open_now if p in planets]
        for p in active:
            if p not in platoons_done:  # взводы планеты закрываются один раз, когда она открыта
                platoons_done.add(p)
                progress[p] += platoon_points.get(p, 0)
        got = {p: 0 for p in active}
        while True:
            options = []
            for p in active:
                k = got[p]
                thresholds = planets[p]["stars"]
                if k < len(thresholds):
                    options.append((max(0, thresholds[k] - progress[p]), p))
            affordable = [o for o in options if o[0] <= budget]
            if not affordable:
                break
            cost, p = min(affordable)
            budget -= cost
            progress[p] += cost
            got[p] += 1
        leftover = [p for p in active if got[p] == 0]
        if leftover and budget > 0:
            p = min(leftover, key=lambda x: planets[x]["stars"][0] - progress[x])
            progress[p] += budget
            budget = 0
        rows = [{"planet": p, "stars": got[p], "points": progress[p], "thresholds": planets[p]["stars"],
                 "bonus": planets[p]["bonus"]} for p in active]
        phases.append({"phase": phase, "deploy": deploy, "battle": battle, "planets": rows,
                       "stars": sum(got.values())})
        nxt = []
        for p in active:
            if got[p]:
                stars[p] = got[p]
                branch = next((b for b in BRANCHES.values() if p in b), None)
                if branch and branch.index(p) + 1 < len(branch):
                    nxt.append(branch[branch.index(p) + 1])
            else:
                nxt.append(p)
        for b_planet, rule in bonus.items():
            if rule["after"] in active and got.get(rule["after"]) and b_planet not in stars and b_planet not in nxt:
                if covert_ready.get(rule["covert_zone"], 0) >= rule["wins"]:
                    nxt.append(b_planet)
        open_now = nxt
    return {"phases": phases, "total": sum(stars.values()), "max": 3 * len(planets), "gp": gp}
