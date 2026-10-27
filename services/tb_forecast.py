"""ВИ-проекция «Восхода Империи», часть 2 (веб /tb/plan): боевые миссии (кто
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


STRIKE_STEP_RELICS = 2  # «в шаге» — как у спецмиссий: до двух реликвий (у кораблей — одной звезды)


def _level_label(level: tuple, fleet: bool) -> str:
    stars, relic = level
    if fleet:
        return f"{stars}★"
    return f"R{relic}" if relic is not None else (f"{stars}★ без реликвии" if stars >= 7 else f"{stars}★")


def strike_gap(m: dict, levels: dict, ships: set) -> tuple[list, bool]:
    """Для игрока со статусом short: что докачать, чтобы зайти — [(base_id, уровень)] — и
    «в шаге» ли он. Сначала отстающие обязательные, затем, если подходящих юнитов на пороге
    всё равно не хватит, — ближайшие к порогу из остальных подходящих."""
    pool = m["units"] if m["units"] is not None else [b for b in levels if (b in ships) == m["fleet"]]
    owned = {b for b in pool if b in levels} | set(m["mandatory"])
    ok = {b for b in owned if _ok(levels[b], m)}
    lagging = [b for b in m["mandatory"] if b not in ok]
    extra = max(0, m["count"] - len(ok) - len(lagging))
    rest = sorted((b for b in owned - ok - set(m["mandatory"])),
                  key=lambda b: (-levels[b][0], -(levels[b][1] if levels[b][1] is not None else -9)))
    to_level = lagging + rest[:extra]

    def close(b):
        stars, relic = levels[b]
        if m["fleet"] or m["min_relic"] is None:
            return stars >= m["min_stars"] - 1
        return stars >= m["min_stars"] and relic is not None and relic >= m["min_relic"] - STRIKE_STEP_RELICS
    return [(b, _level_label(levels[b], m["fleet"])) for b in to_level], all(close(b) for b in to_level)


def qualifying_units(m: dict, levels: dict, ships: set) -> list:
    """Юниты игрока, которые годятся для боя (на пороге) — только у боёв с конкретным
    списком юнитов или обязательными: «любой персонаж R5» взводами не сломать."""
    if m["units"] is None and not m["mandatory"]:
        return []
    pool = set(m["units"] or ()) | set(m["mandatory"])
    return sorted(b for b in pool if b in levels and _ok(levels[b], m))


def platoon_conflict(st: dict, code: str, donated: set) -> list | None:
    """Если игрок из «могут зайти» отдаёт во взводы этого этапа юниты, без которых бой ему уже
    не пройти, — эти юниты, иначе None. st — строка strikes_by_phase."""
    ok = (st.get("qualifying") or {}).get(code)
    if not ok or not donated & set(ok):
        return None
    left = set(ok) - donated
    if all(b in left for b in st["mandatory"]) and len(left) >= st["count"]:
        return None
    return sorted(donated & set(ok))


def strikes_by_phase(rosters: dict, names: dict, with_bonus: bool) -> dict:
    """{этап: [{planet, n, fleet, label, requirement, ready: [ники], step/short: [(ник, что
    докачать)], missing: сколько без нужных юнитов, qualifying: {код: [годные юниты]} для
    проверки взводов (platoon_conflict)}]}."""
    ms = strike_missions()
    all_ids = {b for r in rosters.values() for b in r}
    ships = {b for b, t in database.get_unit_types(list(all_ids)).items() if t == "ship"}
    unit_names = database.get_game_unit_names(sorted(all_ids))
    levels = {code: roster_levels(roster) for code, roster in rosters.items()}
    out: dict[int, list] = {}
    for m in ms:
        if m["planet"] in _BONUS_PLANET.values() and not with_bonus:
            continue
        ready, step, short, missing = [], [], [], 0
        qualifying = {}
        for code, lv in levels.items():
            st = strike_status(m, lv, ships)
            if st == "ready":
                ready.append(names.get(code, code))
                q = qualifying_units(m, lv, ships)
                if q:
                    qualifying[code] = q
            elif st == "short":
                gap, is_close = strike_gap(m, lv, ships)
                text = ", ".join(f"{unit_names.get(b, b)} {label}" for b, label in gap)
                (step if is_close else short).append((names.get(code, code), text))
            else:
                missing += 1
        level = f"{m['min_stars']}★" if m["fleet"] else f"R{m['min_relic']}"
        must = ", ".join(unit_names.get(b, b) for b in m["mandatory"])
        if m["mandatory"] and m["count"] <= len(m["mandatory"]):
            req = f"{must} {level}"
        else:
            req = f"{'флот' if m['fleet'] else m['label']} {level} ×{m['count']}" + (f" · {must}" if must else "")
        out.setdefault(m["phase"], []).append({
            "planet": m["planet"], "n": m["n"], "fleet": m["fleet"], "requirement": req,
            "ready": sorted(ready, key=str.lower),
            "step": sorted(step, key=lambda x: x[0].lower()), "short": sorted(short, key=lambda x: x[0].lower()),
            "missing": missing,
            "phase": m["phase"], "mandatory": m["mandatory"], "count": m["count"], "qualifying": qualifying,
        })
    return out


# ------------------------------------------------------------------ прогноз звёзд

# Очки за все боевые миссии планеты, если их пройдёт один игрок за день (все волны). В
# игровых данных очков за бой нет, числа сняты с HotUtils TB v2 (карточки Strategy, «waves up
# to N M · k missions × 50», делим на 50) 2026-10-26. 6 этап у AC ещё не открывался — его
# планеты оценены как 5 этап × 1,45 (столько же растёт каждый этап с 3 по 5: Haven→Malachor
# 1,46, Kessel→Vandor 1,46, Kashyyyk→Lothal 1,44).
BATTLE_MAX_PER_MEMBER = {
    "Mustafar": 1_200_000, "Corellia": 1_000_000, "Coruscant": 1_200_000,
    "Geonosis": 1_500_000, "Felucia": 1_500_000, "Bracca": 1_250_000,
    "Dathomir": 1_366_000, "Tatooine": 1_706_000, "Kashyyyk": 1_706_000, "Zeffo": 2_380_000,
    "Haven-class Medical Station": 1_974_000, "Kessel": 2_460_000, "Lothal": 2_460_000, "Mandalore": 3_460_000,
    "Malachor": 2_880_000, "Vandor": 3_600_000, "Ring of Kafrene": 4_340_000,
    "Death Star": 4_180_000, "Hoth": 5_220_000, "Scarif": 6_290_000,
}
ESTIMATED_BATTLE_MAX = {"Death Star", "Hoth", "Scarif"}
NO_HISTORY_SHARE = 0.3   # доля боёв на планете, где гильдия ещё не воевала
DEFAULT_DEPLOY_SHARE = 0.95


def battle_shares(guild_id: int) -> dict:
    """{планета: доля волн боёв, которые гильдия отбивает за день} — медиана последних ВИ:
    отбито волн / (игроков × волн на планете). Волны, а не очки: очки по планете в
    расшифровке Comlink для планет, открытых два этапа, не делятся на залив и бои. Доли от ВИ
    к ВИ стабильны (±5 %) и сходятся с HotUtils («your last ROTEs filled the zone»)."""
    from services import tb_missions
    waves = tb_missions.strike_zones()
    events = database.get_recent_tb_events(HISTORY_EVENTS, guild_id=guild_id)
    per_planet: dict = {}
    for eid, _at in events:
        rows = database.get_tb_zone_data_for_events([eid])
        players = len(rows)
        if not players:
            continue
        enc: dict = {}
        opened: set = set()
        for _e, _m, _n, raw in rows:
            try:
                zone = json.loads(raw) or {}
            except (TypeError, ValueError):
                continue
            for ph, confs in zone.items():
                for ck, acts in confs.items():
                    key = f"{ph}/{ck}"
                    opened.add(key)
                    enc[key] = enc.get(key, 0) + sum((acts.get("strike_encounter") or {}).values())
        for key, w in waves.items():
            if not w.get("waves") or key not in opened:  # планета в этой ВИ не открывалась
                continue
            ph, ck = key.split("/", 1)
            planet, _label = tb_missions.zone_planet(ph, ck)
            per_planet.setdefault(planet, []).append(min(1.0, enc.get(key, 0) / (players * w["waves"])))
    out = {}
    for planet, vals in per_planet.items():
        vals = sorted(vals)
        out[planet] = vals[len(vals) // 2]
    return out


def history(guild_id: int) -> dict:
    """По последним ВИ: phases — {этап: {"battle": очки боёв гильдии за этап, "share": доля ГМ,
    которую гильдия залила}}, planets — доля боёв по планетам (battle_shares), last_stars —
    звёзды последней ВИ."""
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
        "planets": battle_shares(guild_id),
        "events": len(events), "last_stars": stars.get(events[-1][0]) if events else None,
    }


def guild_gp(ally_codes: list) -> int:
    profiles = database.get_player_profiles_bulk(ally_codes)
    return sum(int((p.get("stats") or {}).get(GP_STAT) or 0) for p in profiles.values())


def planet_battles(planet: str, hist: dict, members: int, expect: dict = None) -> dict:
    """{max, share, expected, estimated, history}: сколько очков боёв планета даёт за день, если
    все пройдут всё (max), и сколько ждём (expected = max × share). expect — {планета: доля}
    от офицера поверх истории."""
    top = BATTLE_MAX_PER_MEMBER.get(planet, 0) * members
    hist_share = (hist.get("planets") or {}).get(planet)
    share = (expect or {}).get(planet)
    if share is None:
        share = hist_share if hist_share is not None else NO_HISTORY_SHARE
    return {"max": top, "share": share, "expected": top * share, "history": hist_share,
            "estimated": planet in ESTIMATED_BATTLE_MAX}


def history_deploy_share(hist: dict) -> float:
    """Доля ГМ, которую гильдия заливает, когда это нужно: максимум по этапам прошлых ВИ (на
    первых этапах заливают меньше не потому, что не могут, а потому, что звёзды уже взяты)."""
    shares = [v.get("share") or 0 for v in (hist.get("phases") or {}).values()]
    return min(1.0, max(shares)) if shares and max(shares) > 0 else DEFAULT_DEPLOY_SHARE


def deploy_budget(phase: int, hist: dict, gp: int, deploy_share: float = None) -> float:
    h = (hist.get("phases") or {}).get(phase) or {}
    share = deploy_share if deploy_share is not None else (h.get("deploy_share") or history_deploy_share(hist))
    return gp * min(1.0, share) + (h.get("extra") or 0)


def _cost(cell: dict, k: int) -> float:
    """Сколько очков залива и боёв нужно планете сегодня на k звёзд (взводы платят при закрытии)."""
    if not k:
        return 0
    return max(0, cell["stars"][k - 1] - cell["v"] - cell["ops"])


def _deploy_need(cell: dict, k: int) -> float:
    return max(0, _cost(cell, k) - cell["battle"])


def _choices(cells: list, budget: float) -> list:
    """Варианты одного этапа: [(звёзд по планетам, остаток залива)]. cells — [{v: набрано,
    ops: очки закрытых операций, stars: пороги звёзд, cap: первый порог награды, battle:
    ожидаемые очки боёв}]. Бои идут только в свою планету, залив — в любую. 0 звёзд = планета
    копит на следующий этап. Заведомо худшие варианты отброшены: недобранную звезду, на
    которую хватает остатка, берём; планету, которую можно сразу закрыть на максимум, не копим."""
    out = []

    def walk(i, ks, spent):
        if i == len(cells):
            left = budget - spent
            for j, k in enumerate(ks):
                top = len(cells[j]["stars"])
                if k < top and _deploy_need(cells[j], k + 1 if k else top) - _deploy_need(cells[j], k) <= left:
                    return
            out.append((tuple(ks), left))
            return
        for k in range(len(cells[i]["stars"]) + 1):
            c = _deploy_need(cells[i], k)
            if spent + c <= budget:
                walk(i + 1, ks + [k], spent + c)

    walk(0, [], 0)
    return out


def _platoon_priority(planets: dict):
    """Порядок планет при раздаче юнитов во взводы: бонусные — в последнюю очередь (в планах
    офицеров «взводы только по боту»), остальные — по этапу планеты."""
    return lambda p: (planets[p]["bonus"], planets[p]["phase"])


def _fill_platoons(state: tuple, slots: dict, levels: dict, priority, full: frozenset = frozenset(),
                   skip: frozenset = frozenset()) -> tuple:
    """Взводы одного этапа. state — ((планета, очки, маска заполненных слотов), …). Юнит
    игрока встаёт один раз за этап, на какой бы планете ни был слот; заполненное остаётся,
    поэтому планета, открытая несколько этапов, добирает дефицитные слоты на следующих
    (3 из 6 нужных юнитов — на одном этапе, 3 — на другом). Строгие слоты первыми, затем
    планеты по priority — тогда игроки с высокой реликвией не уходят на слабые слоты.
    full — планеты, взводы которых считаем закрытыми без юнитов (сценарий «добрать взводы»),
    skip — планеты, где взводы не ставим. Возвращает state с новыми масками."""
    demand: dict = {}
    masks = [(1 << len(slots.get(p) or [])) - 1 if p in full else mask for p, _v, mask in state]
    for i, (p, _v, mask) in enumerate(state):
        if p in full or p in skip:
            continue
        for j, (_op, bid, need) in enumerate(slots.get(p) or []):
            if bid and not mask >> j & 1:
                demand.setdefault(bid, []).append((-need, priority(p), i, j))
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


def _preload(cells: list, ks, left: float) -> tuple[list, list, float]:
    """Копящие планеты (0 звёзд) набирают до первого порога награды: сначала своими боями,
    потом остатком залива — в ту, которой дальше всего до максимума. → (очки к концу дня,
    очки боёв на планете, остаток залива)."""
    after, used = [], []
    for cell, k in zip(cells, ks):
        if k:
            cost = _cost(cell, k)
            after.append(cell["stars"][k - 1])
            used.append(min(cell["battle"], cost))
        else:
            room = max(0, cell["cap"] - 1 - cell["v"])
            b = min(cell["battle"], room)
            after.append(cell["v"] + b)
            used.append(b)
    for i in sorted((i for i, k in enumerate(ks) if not k), key=lambda i: after[i] - cells[i]["stars"][-1]):
        if left <= 0:
            break
        put = min(max(0, cells[i]["cap"] - 1 - after[i]), left)
        after[i] += put
        left -= put
    return after, used, left


def _branch_of() -> dict:
    return {p: b for b in BRANCHES.values() for p in b}


def _next_open(state: tuple, ks, after, zone_data: dict, bonus_open) -> tuple:
    """Планеты следующего этапа: со звездой — закрылась, открывает следующую в ветке; без —
    копит дальше (если не перевалила первый порог награды). bonus_open(бонусная, правило) —
    открывать ли бонусную планету после звезды на её «предшественнице»."""
    planets = zone_data.get("planets") or {}
    branch_of = _branch_of()
    nxt = []
    for (p, _v, mask), k, v in zip(state, ks, after):
        if not k:
            if v < (planets[p].get("tiers") or planets[p]["stars"])[0]:
                nxt.append((p, v, mask))
            continue
        branch = branch_of.get(p)
        if branch and branch.index(p) + 1 < len(branch):
            nxt.append((branch[branch.index(p) + 1], 0, 0))
        for b_planet, rule in (zone_data.get("bonus") or {}).items():
            if rule["after"] == p and b_planet in planets and bonus_open(b_planet, rule):
                nxt.append((b_planet, 0, 0))
    return tuple(sorted(nxt))


def _phase_cells(state: tuple, zone_data: dict, slots: dict, hist: dict, members: int, expect: dict) -> list:
    planets = zone_data.get("planets") or {}
    cells = []
    for p, v, m in state:
        info = planets[p]
        b = planet_battles(p, hist, members, expect)
        cells.append({"planet": p, "v": v, "mask": m, "stars": info["stars"],
                      "cap": (info.get("tiers") or info["stars"])[0],
                      "ops_closed": _ops_closed(slots.get(p) or [], m),
                      "ops": _ops_closed(slots.get(p) or [], m) * info["op_points"],
                      "battle": b["expected"], "battle_info": b})
    return cells


def _phase_options(cells: list, budget: float, fix: dict) -> list:
    """Варианты этапа, где часть планет офицер уже задал (fix — {планета: звёзд}): заданные
    берём как есть, даже если залива на них не хватает (это покажет simulate), остальные —
    перебором на оставшийся залив."""
    if not fix:
        return _choices(cells, budget)
    fixed = {i: max(0, min(int(fix[c["planet"]]), len(c["stars"]))) for i, c in enumerate(cells) if c["planet"] in fix}
    free = [i for i in range(len(cells)) if i not in fixed]
    rest = budget - sum(_deploy_need(cells[i], k) for i, k in fixed.items())
    sub = _choices([cells[i] for i in free], rest) if rest >= 0 else []
    if not sub:
        sub = [((0,) * len(free), rest)]
    out = []
    for ks_free, left in sub:
        ks = [0] * len(cells)
        for i, k in fixed.items():
            ks[i] = k
        for j, i in enumerate(free):
            ks[i] = ks_free[j]
        out.append((tuple(ks), left))
    return out


def forecast(zone_data: dict, platoons: dict, covert_ready: dict, hist: dict, gp: int, members: int = 50,
             expect: dict = None, fixed: dict = None, bonus: set = None, deploy_share: float = None,
             no_platoons: dict = None, preload: dict = None) -> dict:
    """Звёзды по этапам. platoons — {"slots": {планета: [(операция, base_id|None, порог)]},
    "levels": {base_id: [уровни игроков по убыванию]}} (порог и уровень — реликвия, у кораблей
    звёзды), covert_ready — {zone_id спецмиссии: сколько игроков её проходят}. Модель: на
    каждом этапе гильдия заливает долю ГМ, как в прошлых ВИ (залив идёт в любую планету), и
    бьёт бои каждой открытой планеты так же, как в прошлых ВИ (очки боёв — только своей
    планете, planet_battles); операции взводов платят, когда планета берёт звезду. Планета со
    звездой закрывается и открывает следующую в ветке; без звезды — копит на следующий этап
    (до первого порога награды). Перебираем все 6 этапов сразу и берём план с наибольшим
    числом звёзд — как офицеры: «0★ сейчас, 3★ на следующем этапе» часто лучше, чем 1★ сразу.
    fixed — {этап: {планета: звёзд}}, что офицер уже решил (калькулятор плана): эти планеты
    берём как заданы, остальное подбираем. Остальные параметры — как у simulate."""
    planets = zone_data.get("planets") or {}
    slots, levels = platoons.get("slots") or {}, platoons.get("levels") or {}
    full = frozenset(platoons.get("full") or ())
    memo: dict = {}
    priority = _platoon_priority(planets)
    fixed = {int(k): v for k, v in (fixed or {}).items()}
    no_platoons = {int(k): frozenset(v) for k, v in (no_platoons or {}).items()}
    bonus = set((zone_data.get("bonus") or {}).keys()) if bonus is None else set(bonus)

    def bonus_open(planet, rule):
        return planet in bonus and covert_ready.get(rule["covert_zone"], 0) >= rule["wins"]

    def solve(phase: int, state: tuple) -> tuple:
        """state — ((планета, очки, маска взводов), …) открытых планет → (звёзд, −недобранных
        планет, −дней копления, −потеряно очков, [ks этапов]). При равных звёздах — как в планах
        офицеров: обычную планету закрываем на 3★ или копим, а не берём 1–2★, и не копим
        дольше, чем нужно."""
        if phase > 6 or not state:
            return 0, 0, 0, 0, []
        key = (phase, tuple((p, int(v // 1e6), m) for p, v, m in state))
        if key in memo:
            return memo[key]
        state = _fill_platoons(state, slots, levels, priority, full, no_platoons.get(phase, frozenset()))
        cells = _phase_cells(state, zone_data, slots, hist, members, expect)
        best = None
        for ks, left in _phase_options(cells, deploy_budget(phase, hist, gp, deploy_share), fixed.get(phase)):
            after, _used, left = _preload(cells, ks, left)
            stars, neg_partial, neg_carry, neg_waste, rest = solve(phase + 1, _next_open(state, ks, after, zone_data, bonus_open))
            partial = sum(1 for c, k in zip(cells, ks) if 0 < k < len(c["stars"]))
            cand = (stars + sum(ks), neg_partial - partial, neg_carry - ks.count(0), neg_waste - left,
                    [dict(zip((c["planet"] for c in cells), ks))] + rest)
            if best is None or cand[:4] > best[:4]:
                best = cand
        memo[key] = best
        return best

    start = tuple(sorted((b[0], 0, 0) for b in BRANCHES.values() if b[0] in planets))
    _total, _p, _c, _w, steps = solve(1, start)
    choices = {phase: ks for phase, ks in enumerate(steps, start=1)}
    result = simulate(zone_data, platoons, covert_ready, hist, gp, members, choices, bonus=bonus, expect=expect,
                      deploy_share=deploy_share, no_platoons=no_platoons, preload=preload, strict_bonus=True)
    starred = {r["planet"] for ph in result["phases"] for r in ph["planets"] if r["stars"]}
    for ph in result["phases"]:
        for r in ph["planets"]:
            r["carry"] = not r["stars"] and r["planet"] in starred
            r["skip"] = not r["stars"] and r["planet"] not in starred
    result["choices"] = choices
    return result


def simulate(zone_data: dict, platoons: dict, covert_ready: dict, hist: dict, gp: int, members: int,
             choices: dict, bonus: set = None, preload: dict = None, expect: dict = None,
             deploy_share: float = None, no_platoons: dict = None, strict_bonus: bool = False) -> dict:
    """Прогнать план: choices — {этап: {планета: звёзд к концу этапа}} (нет в плане — максимум),
    preload — {этап: {планета: до скольких очков копить}} (нет — «сколько останется»), bonus —
    какие бонусные планеты открываем, expect — {планета: доля боёв} поверх истории,
    deploy_share — доля ГМ в заливе (None — как в прошлых ВИ), no_platoons — {этап: {планеты}},
    где взводы не ставим. strict_bonus — бонусную планету открываем, только если спецмиссию
    проходит достаточно игроков (иначе открываем и предупреждаем).
    → {phases: [{phase, planets: [...], deploy, deploy_need, battle_expected, battle_max,
    battle_need, ok, short, warnings, stars}], total, max, gp}."""
    planets = zone_data.get("planets") or {}
    slots, levels = platoons.get("slots") or {}, platoons.get("levels") or {}
    full = frozenset(platoons.get("full") or ())
    priority = _platoon_priority(planets)
    bonus = set((zone_data.get("bonus") or {}).keys()) if bonus is None else set(bonus)
    preload = preload or {}
    no_platoons = no_platoons or {}
    warnings_next: list = []

    def bonus_open(planet, rule):
        if planet not in bonus:
            return False
        ready = covert_ready.get(rule["covert_zone"], 0)
        if ready < rule["wins"]:
            if strict_bonus:
                return False
            warnings_next.append(f"{planet}: для открытия нужно {rule['wins']} побед в спецмиссии, проходят {ready}")
        return True

    state = tuple(sorted((b[0], 0, 0) for b in BRANCHES.values() if b[0] in planets))
    phases, total = [], 0
    for phase in range(1, 7):
        if not state:
            break
        warnings, warnings_next[:] = list(warnings_next), []
        skip = frozenset(no_platoons.get(phase) or ())
        state = _fill_platoons(state, slots, levels, priority, full, skip)
        cells = _phase_cells(state, zone_data, slots, hist, members, expect)
        day = choices.get(phase) or choices.get(str(phase)) or {}
        day_preload = preload.get(phase) or preload.get(str(phase)) or {}
        ks = []
        for c in cells:
            k = day.get(c["planet"])
            ks.append(len(c["stars"]) if k is None else max(0, min(int(k), len(c["stars"]))))
        budget = deploy_budget(phase, hist, gp, deploy_share)
        need = 0.0
        targets = []
        for c, k in zip(cells, ks):
            target = day_preload.get(c["planet"]) if not k else None
            if target is not None:
                target = float(target)
                if target >= c["cap"]:
                    warnings.append(f"{c['planet']}: копить {target / 1e6:.0f} млн — это уже первый порог "
                                    f"({c['cap'] / 1e6:.0f} млн), планета закроется")
                cost = max(0, target - c["v"])
                need += max(0, cost - c["battle"])
            else:
                need += _deploy_need(c, k)
            targets.append(target)
        left = budget - need
        after, used = [], []
        for c, k, target in zip(cells, ks, targets):
            if k:
                after.append(c["stars"][k - 1])
                used.append(min(c["battle"], _cost(c, k)))
            elif target is not None:
                after.append(max(c["v"], target))
                used.append(min(c["battle"], max(0, target - c["v"])))
            else:
                after.append(None)
                used.append(0)
        auto = [i for i, (k, t) in enumerate(zip(ks, targets)) if not k and t is None]
        if auto:
            sub_after, sub_used, rest = _preload([cells[i] for i in auto], [0] * len(auto), max(0, left))
            for i, a, u in zip(auto, sub_after, sub_used):
                after[i], used[i] = a, u
            if left > 0:
                left = rest
        ok = left >= 0
        if phase == 6:
            for c, k in zip(cells, ks):
                if not k:
                    warnings.append(f"{c['planet']}: 6 этап последний — копить некуда, звезда пропадёт")
        battle_max = sum(c["battle_info"]["max"] for c in cells)
        costs = sum(_cost(c, k) for c, k in zip(cells, ks)) + sum(
            max(0, t - c["v"]) for c, t in zip(cells, targets) if t is not None)
        battle_need = max(0.0, costs - budget)
        rows = []
        for c, k, a, u, target in zip(cells, ks, after, used, targets):
            info = planets[c["planet"]]
            rows.append({
                "planet": c["planet"], "stars": k, "max": len(c["stars"]),
                "bonus": info["bonus"], "phase": info["phase"], "start": c["v"], "points": a,
                "thresholds": c["stars"], "cap": c["cap"], "target": target,
                "ops_closed": c["ops_closed"], "ops_points": c["ops"] if k else 0,
                "ops_pending": c["ops"] if not k else 0, "op_points": info["op_points"],
                "battle": c["battle_info"], "battle_used": u,
                "deploy_need": (_deploy_need(c, k) if k else max(0, (a or 0) - c["v"] - u)),
                "need": _cost(c, k) if k else max(0, (a or 0) - c["v"]),
                "no_platoons": c["planet"] in skip,
                "short_slots": [] if c["planet"] in skip else [
                    (bid, need) for j, (_op, bid, need) in enumerate(slots.get(c["planet"]) or [])
                    if not c["mask"] >> j & 1],
            })
        stars = sum(ks)
        total += stars
        phases.append({
            "phase": phase, "planets": rows, "stars": stars, "ok": ok, "short": max(0.0, -left),
            "deploy": budget, "deploy_need": need, "deploy_left": max(0.0, left),
            "battle": sum(used), "battle_expected": sum(c["battle"] for c in cells), "battle_max": battle_max,
            "battle_need": battle_need,
            "battle_need_pct": battle_need / battle_max if battle_max else 0,
            "battle_expected_pct": sum(c["battle"] for c in cells) / battle_max if battle_max else 0,
            "ops": sum(r["ops_points"] for r in rows), "warnings": warnings,
        })
        state = _next_open(state, ks, [a if a is not None else 0 for a in after], zone_data, bonus_open)
    return {"phases": phases, "total": total,
            "max": sum(len(v["stars"]) for v in planets.values()), "gp": gp}


def platoon_shortfall(zone_data: dict, platoons: dict, result: dict, only: set) -> list[dict]:
    """Каких юнитов не хватает, чтобы закрыть взводы планет only по плану result (forecast,
    посчитанный с platoons["full"]): прогоняем этапы плана с реальными складами.
    [{base_id, need, short, planets, ship}] — need это реликвия (у кораблей звёзды)."""
    planets = zone_data.get("planets") or {}
    slots, levels = platoons.get("slots") or {}, platoons.get("levels") or {}
    priority = _platoon_priority(planets)
    masks: dict = {}
    for ph in result["phases"]:
        state = tuple((r["planet"], 0, masks.get(r["planet"], 0)) for r in ph["planets"])
        for p, _v, m in _fill_platoons(state, slots, levels, priority):
            masks[p] = m
    short: dict = {}
    for p in only:
        for j, (_op, bid, need) in enumerate(slots.get(p) or []):
            if not masks.get(p, 0) >> j & 1:
                row = short.setdefault((bid, need), {"base_id": bid, "need": need, "short": 0, "planets": [],
                                                     "ship": bid in (platoons.get("ships") or ())})
                row["short"] += 1
                if p not in row["planets"]:
                    row["planets"].append(p)
    return sorted(short.values(), key=lambda r: (-r["short"], str(r["base_id"])))
