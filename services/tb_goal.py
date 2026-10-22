"""Вкладка «Цель» ВИ-проекции (веб /tb/goal, флаг tb_star_goal — сейчас только АС): офицер
задаёт, сколько звёзд нужно, а мы считаем несколько вариантов, как их набрать, и что для
каждого надо сделать. Все варианты — тот же прогноз (services/tb_forecast.py) с изменёнными
входами, поэтому план по этапам у каждого свой:
- полный залив — гильдия заливает весь ГМ на каждом этапе (в прошлых ВИ местами меньше);
- бои — сколько очков боёв добавить за этап (минимум по сумме: на этапах k…6 поровну);
- взводы — какие планеты закрыть во взводах целиком и каких юнитов для этого не хватает;
- взводы + бои — если одних взводов мало;
- рост ГМ — сколько ГМ гильдии добавить.
Входы берутся из уже посчитанной проекции (forecast["inputs"]), склады заново не читаются."""

import copy

import database
from services import tb_forecast

STEP = 5_000_000            # точность поиска добавки очков / ГМ
MAX_EXTRA = 600_000_000     # больше добавки за этап не ищем — нереально
MAX_GP_EXTRA = 3_000_000_000


def _mln(x: float, digits: int = 0) -> str:
    return f"{x / 1e6:.{digits}f}".replace(".", ",")


def _run(inp: dict, hist: dict = None, gp: int = None, full=()) -> dict:
    platoons = {**inp["platoons"], "full": frozenset(full)} if full else inp["platoons"]
    return tb_forecast.forecast(inp["zone_data"], platoons, inp["covert_ready"], hist or inp["hist"],
                                inp["gp"] if gp is None else gp)


def _min_extra(reach, limit: int):
    """Наименьшая добавка (кратная STEP), при которой reach(добавка) даёт результат, и сам
    результат; None — не хватает даже limit. Звёзд от добавки не становится меньше, поэтому
    двоичный поиск."""
    top = reach(limit)
    if top is None:
        return None
    lo, hi, best = 0, limit // STEP, top
    while lo < hi:
        mid = (lo + hi) // 2
        r = reach(mid * STEP)
        if r is None:
            lo = mid + 1
        else:
            hi, best = mid, r
    return hi * STEP, best


def _battles(hist: dict, first: int, extra: float) -> dict:
    h = copy.deepcopy(hist)
    for ph in range(first, 7):
        row = h.setdefault("phases", {}).setdefault(ph, {"battle": 0, "share": 0.9})
        row["battle"] += extra
    return h


def _cheapest_battles(inp: dict, target: int, full=()):
    """(первый этап, добавка за этап, результат) с наименьшей суммарной добавкой."""
    best = None
    for first in range(1, 7):
        found = _min_extra(lambda x: (lambda r: r if r["total"] >= target else None)(
            _run(inp, hist=_battles(inp["hist"], first, x), full=full)), MAX_EXTRA)
        if found and (best is None or found[0] * (7 - first) < best[1] * (7 - best[0])):
            best = (first, found[0], found[1])
    return best


def _shortfall_conditions(inp: dict, result: dict, planets: set) -> list[dict]:
    rows = tb_forecast.platoon_shortfall(inp["zone_data"], inp["platoons"], result, planets)
    names = database.get_game_unit_names([r["base_id"] for r in rows if r["base_id"]])
    by_planet: dict = {}
    for r in rows:
        for p in r["planets"]:
            by_planet.setdefault(p, []).append({**r, "name": names.get(r["base_id"]) or r["base_id"] or "неизвестный юнит"})
    return [{"planet": p, "units": units} for p, units in sorted(by_planet.items(), key=lambda x: -sum(u["short"] for u in x[1]))]


def _fewest_full_planets(inp: dict, target: int, candidates: list, extra_hist: dict = None):
    """Убираем из «закрыть взводы целиком» планеты, без которых цель всё равно берётся."""
    keep = list(candidates)
    for p in sorted(candidates, key=lambda x: -inp["zone_data"]["planets"][x]["phase"]):
        trial = [x for x in keep if x != p]
        if _run(inp, hist=extra_hist, full=trial)["total"] >= target:
            keep = trial
    return keep


def variants(inp: dict, target: int, base: dict) -> dict:
    """{target, base_total, reached, variants: [{key, title, ok, conditions, platoons, result}]}.
    platoons — [{planet, units: [{name, need, short}]}]: планеты, где закрыть взводы целиком,
    и каких юнитов не хватает (short — сколько слотов не закрыть нынешними складами)."""
    out = []
    players = max(1, inp.get("players") or 1)
    hist = inp["hist"]
    phases = hist.get("phases") or {}

    # 1. Полный залив ГМ
    low = {ph: v["share"] for ph, v in phases.items() if v["share"] < 0.98}
    if low:
        h = copy.deepcopy(hist)
        for ph in low:
            h["phases"][ph]["share"] = 1.0
        r = _run(inp, hist=h)
        out.append({"key": "deploy", "title": "Залить весь ГМ", "ok": r["total"] >= target, "result": r,
                    "conditions": [f"{ph} этап: заливать весь ГМ — в прошлых ВИ {share:.0%}, это +{_mln((1 - share) * inp['gp'])} млн"
                                   for ph, share in sorted(low.items())]})

    # 2. Больше очков в боях
    found = _cheapest_battles(inp, target)
    if found:
        first, extra, r = found
        span = f"{first}–6 этапы" if first < 6 else "6 этап"
        conds = [f"{span}: +{_mln(extra)} млн очков в боях за этап (≈ {_mln(extra / players, 1)} млн на игрока)"]
        conds += [f"{ph} этап: сейчас в среднем {_mln(phases[ph]['battle'])} млн" for ph in range(first, 7) if ph in phases]
        out.append({"key": "battles", "title": "Больше бить БЗ", "ok": True, "result": r, "conditions": conds})
    else:
        out.append({"key": "battles", "title": "Больше бить БЗ", "ok": False, "result": None,
                    "conditions": [f"не хватает даже +{_mln(MAX_EXTRA)} млн очков боёв на каждом этапе"]})

    # 3. Добрать взводы
    all_planets = [p for p in inp["platoons"]["slots"] if p in inp["zone_data"]["planets"]]
    r_full = _run(inp, full=all_planets)
    if r_full["total"] >= target:
        keep = _fewest_full_planets(inp, target, all_planets)
        r = _run(inp, full=keep)
        short = _shortfall_conditions(inp, r, set(keep))
        out.append({"key": "platoons", "title": "Добрать взводы", "ok": True, "result": r, "platoons": short,
                    "conditions": []})
    else:
        out.append({"key": "platoons", "title": "Добрать взводы", "ok": False, "result": None,
                    "conditions": [f"даже со всеми закрытыми взводами выходит {r_full['total']}★"]})
        # 4. Взводы + бои
        found = _cheapest_battles(inp, target, full=all_planets)
        if found:
            first, extra, _r = found
            h = _battles(hist, first, extra)
            keep = _fewest_full_planets(inp, target, all_planets, extra_hist=h)
            r = _run(inp, hist=h, full=keep)
            short = _shortfall_conditions(inp, r, set(keep))
            span = f"{first}–6 этапы" if first < 6 else "6 этап"
            conds = [f"{span}: +{_mln(extra)} млн очков в боях за этап (≈ {_mln(extra / players, 1)} млн на игрока)"]
            out.append({"key": "mix", "title": "Взводы + бои", "ok": True, "result": r, "platoons": short,
                        "conditions": conds})

    # 5. Рост ГМ
    found = _min_extra(lambda x: (lambda r: r if r["total"] >= target else None)(_run(inp, gp=inp["gp"] + x)), MAX_GP_EXTRA)
    if found:
        extra, r = found
        out.append({"key": "gp", "title": "Вырасти по ГМ", "ok": True, "result": r,
                    "conditions": [f"+{_mln(extra)} млн ГМ гильдии (≈ {_mln(extra / players, 1)} млн на игрока), "
                                   f"всего {_mln((inp['gp'] + extra) / 1e3, 2)} млрд"]})
    return {"target": target, "base_total": base["total"], "reached": base["total"] >= target, "variants": out}
