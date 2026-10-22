"""ВИ-проекция «Восхода Империи», часть 2 (веб /tb/projection): боевые миссии (кто
может зайти в каждый бой) и прогноз звёзд на всю битву.

Всё из игровых данных t05D, без входа в аккаунт (проверено 2026-10-22), пишет units_sync:
- пороги звёзд планеты — conflictZoneDefinition[].victoryPointRewards[].galacticScoreRequirement,
  звезду даёт только порог с reward.type 2 (у Зеффо и Мандалора первые два порога — ящики,
  type 3, звезда одна и лишь за полное заполнение — сходится с 44★ в наших ВИ);
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
STAR_REWARD = 2                       # reward.type порога, который даёт звезду (3 — ящик)
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
    """{"planets": {планета: {phase, side, bonus, tiers: [все пороги наград], stars: [пороги звёзд],
    op_points}},
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
        rewards = z.get("victoryPointRewards") or []
        planets[planet] = {"phase": phase, "bonus": is_bonus,
                           "tiers": [int(v.get("galacticScoreRequirement") or 0) for v in rewards],
                           "stars": [int(v.get("galacticScoreRequirement") or 0) for v in rewards
                                     if (v.get("reward") or {}).get("type") == STAR_REWARD],
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


def _choices(cells: list, budget: float) -> list:
    """Варианты одного этапа: [(звёзд по планетам, остаток очков)]. cells — [(очки, пороги
    звёзд, первый порог награды)]. 0 звёзд = планета копит очки на следующий этап — только
    пока ниже первого порога награды: за ним планета закрывается (у Зеффо и Мандалора —
    без звезды, с ящиком). Заведомо худшие варианты отброшены: недобранную звезду, на которую
    хватает остатка, берём; планету, которую можно сразу закрыть на максимум, не копим."""
    out = []

    def cost(i, k):
        progress, thresholds, _cap = cells[i]
        return max(0, thresholds[k - 1] - progress) if k else 0

    def walk(i, ks, spent):
        if i == len(cells):
            left = budget - spent
            for j, k in enumerate(ks):
                top = len(cells[j][1])
                if k < top and cost(j, k + 1 if k else top) - cost(j, k) <= left:
                    return
            out.append((tuple(ks), left))
            return
        for k in range(len(cells[i][1]) + 1):
            c = cost(i, k)
            if spent + c <= budget:
                walk(i + 1, ks + [k], spent + c)

    walk(0, [], 0)
    return out


def _fill_platoons(state: tuple, slots: dict, levels: dict, priority) -> tuple:
    """Взводы одного этапа. state — ((планета, очки, маска заполненных слотов), …). Юнит
    игрока встаёт один раз за этап, на какой бы планете ни был слот; заполненное остаётся,
    поэтому планета, открытая несколько этапов, добирает дефицитные слоты на следующих
    (3 из 6 нужных юнитов — на одном этапе, 3 — на другом). Строгие слоты первыми, затем
    планеты по priority — тогда игроки с высокой реликвией не уходят на слабые слоты.
    Возвращает state с новыми масками."""
    demand: dict = {}
    for i, (p, _v, mask) in enumerate(state):
        for j, (_op, bid, need) in enumerate(slots.get(p) or []):
            if bid and not mask >> j & 1:
                demand.setdefault(bid, []).append((-need, priority(p), i, j))
    masks = [mask for _p, _v, mask in state]
    for bid, wanted in demand.items():
        have = levels.get(bid) or []
        used = 0
        for neg_need, _pr, i, j in sorted(wanted):
            # levels по убыванию: слот закрывается, если подходящих больше, чем уже занято
            if used < len(have) and have[used] >= -neg_need:
                masks[i] |= 1 << j
                used += 1
    return tuple((p, v, m) for (p, v, _old), m in zip(state, masks))


def _ops_closed(slots: list, mask: int) -> int:
    ops: dict = {}
    for j, (op, _bid, _need) in enumerate(slots):
        ops[op] = ops.get(op, True) and bool(mask >> j & 1)
    return sum(ops.values())


def forecast(zone_data: dict, platoons: dict, covert_ready: dict, hist: dict, gp: int) -> dict:
    """Звёзды по этапам. platoons — {"slots": {планета: [(операция, base_id|None, порог)]},
    "levels": {base_id: [уровни игроков по убыванию]}} (порог и уровень — реликвия, у кораблей
    звёзды), covert_ready — {zone_id спецмиссии: сколько игроков её проходят}. Модель: на
    каждом этапе гильдия закрывает взводы открытых планет (см. _fill_platoons), заливает долю
    ГМ, как в прошлых ВИ, и набирает столько же очков боёв. Планета со звездой закрывается
    и открывает следующую в ветке; без звезды — копит очки на следующий этап (прелоад, до
    первого порога награды). Перебираем все 6 этапов сразу и берём план с наибольшим числом
    звёзд за битву — как офицеры: «0★ сейчас, 3★ на следующем этапе» часто лучше, чем 1★ сразу."""
    planets = zone_data.get("planets") or {}
    bonus = zone_data.get("bonus") or {}
    slots, levels = platoons.get("slots") or {}, platoons.get("levels") or {}
    budgets = []
    for phase in range(1, 7):
        h = (hist.get("phases") or {}).get(phase) or {"battle": 0, "share": 0.9}
        budgets.append((gp * h["share"], h["battle"]))
    branch_of = {p: b for b in BRANCHES.values() for p in b}
    memo: dict = {}

    def priority(p: str) -> tuple:
        # бонусные — в последнюю очередь (в планах офицеров «взводы только по боту»)
        return planets[p]["bonus"], planets[p]["phase"]

    def cap(p: str) -> float:
        return (planets[p].get("tiers") or planets[p]["stars"])[0]

    def advance(state: tuple, ks: tuple, after: list) -> tuple:
        nxt = []
        for (p, _v, mask), k, v in zip(state, ks, after):
            if not k:
                if v < cap(p):  # копит; за первым порогом награды закрылась без звезды
                    nxt.append((p, v, mask))
                continue
            branch = branch_of.get(p)
            if branch and branch.index(p) + 1 < len(branch):
                nxt.append((branch[branch.index(p) + 1], 0, 0))
            for b_planet, rule in bonus.items():
                if rule["after"] == p and b_planet in planets and covert_ready.get(rule["covert_zone"], 0) >= rule["wins"]:
                    nxt.append((b_planet, 0, 0))
        return tuple(sorted(nxt))

    def solve(phase: int, state: tuple) -> tuple:
        """state — ((планета, очки, маска взводов), …) открытых планет → (звёзд, −недобранных
        планет, −потеряно очков, [(state после взводов, ks, after)]). При равных звёздах — как
        в планах офицеров: обычную планету закрываем на 3★ или копим, а не берём 1–2★."""
        if phase > 6 or not state:
            return 0, 0, 0, []
        key = (phase, tuple((p, int(v // 1e6), m) for p, v, m in state))
        if key in memo:
            return memo[key]
        filled = _fill_platoons(state, slots, levels, priority)
        state = tuple((p, v + (_ops_closed(slots.get(p) or [], m) - _ops_closed(slots.get(p) or [], old))
                       * planets[p]["op_points"], m) for (p, v, m), (_p, _v, old) in zip(filled, state))
        deploy, battle = budgets[phase - 1]
        cells = [(v, planets[p]["stars"], cap(p)) for p, v, _m in state]
        best = None
        for ks, left in _choices(cells, deploy + battle):
            after = [v if not k else max(v, t[k - 1]) for (v, t, _c), k in zip(cells, ks)]
            # остаток — в копящиеся планеты, сначала в ту, которой дальше всего до максимума
            for i in sorted((i for i, k in enumerate(ks) if not k), key=lambda i: after[i] - cells[i][1][-1]):
                put = min(max(0, cells[i][2] - 1 - after[i]), left)
                after[i] += put
                left -= put
            stars, neg_partial, neg_waste, rest = solve(phase + 1, advance(state, ks, after))
            partial = sum(1 for (_v, t, _c), k in zip(cells, ks) if 0 < k < len(t))
            cand = (stars + sum(ks), neg_partial - partial, neg_waste - left, [(state, ks, after)] + rest)
            if best is None or cand[:3] > best[:3]:
                best = cand
        memo[key] = best
        return best

    start = tuple(sorted((b[0], 0, 0) for b in BRANCHES.values() if b[0] in planets))
    total, _neg_partial, _neg_waste, steps = solve(1, start)
    starred = {p for state, ks, _after in steps for (p, _v, _m), k in zip(state, ks) if k}
    phases = []
    for phase, (state, ks, after) in enumerate(steps, start=1):
        deploy, battle = budgets[phase - 1]
        # carry — копим ради звезды на следующих этапах; skip — звезду на ней так и не берём
        rows = [{"planet": p, "stars": k, "max": len(planets[p]["stars"]), "carry": not k and p in starred,
                 "skip": not k and p not in starred, "points": v,
                 "ops_closed": _ops_closed(slots.get(p) or [], m),
                 "thresholds": planets[p]["stars"], "bonus": planets[p]["bonus"]}
                for (p, _v, m), k, v in zip(state, ks, after)]
        phases.append({"phase": phase, "deploy": deploy, "battle": battle, "planets": rows, "stars": sum(ks)})
    return {"phases": phases, "total": total, "max": sum(len(v["stars"]) for v in planets.values()), "gp": gp}
