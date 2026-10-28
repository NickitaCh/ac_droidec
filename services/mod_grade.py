"""Оценка модов персонажа A–Е (идея swgoh.ru «Анализатор модулей», 2026-10-28). Виджет
карточки игрока /player/<code>.

Эталон — как персонажа модят игроки ВСЕХ подключённых гильдий (player_unit_cache): берутся
юниты R6+ с шестью модами 15 уровня (services/mod_analysis.py::_relevant_unit — тот же
фильтр, что у /mod-analysis). По каждому персонажу: доли основ на четырёх гибких слотах,
доли комбинаций наборов (mod_analysis._significant_sets) и квантили скорости с модов.
Строится раз в сутки ботом после часового обновления ростеров (cogs/stat_requirements.py),
лежит в bot_state REFERENCE_KEY: чтение ~330 ростеров — около минуты.

Оценка персонажа игрока — взвешенная сумма четырёх частей по 0–100:
- основы: на каждом гибком слоте доля основы игрока / доля самой популярной;
- наборы: то же для комбинации наборов;
- скорость: перцентиль скорости с модов (вторички + основа стрелы, без бонуса набора);
- качество: 6 точек и 15 уровень у всех шести модов.
Буквы как у swgoh.ru: А 85+, Б 70+, В 55+, Г 40+, Д 25+, Е — ниже."""

import json
import sqlite3
import time
from collections import Counter, defaultdict

import database
import stat_engine
from services import mod_analysis
from services.mod_search import decode_mod

REFERENCE_KEY = "mod_grade_reference"
REFRESH_HOURS = 24
MIN_SAMPLES = 10  # меньше — эталона нет, персонажа не оцениваем
FLEX_SLOTS = ("arrow", "triangle", "circle", "cross")
SLOT_RU = {"arrow": "Стрела", "triangle": "Треугольник", "circle": "Круг", "cross": "Крест"}
SPEED_STAT = 5
QUANTILES = 20  # 21 точка: 0, 5, …, 100 %
WEIGHTS = {"primary": 0.3, "sets": 0.2, "speed": 0.35, "quality": 0.15}
GRADES = ((85, "А"), (70, "Б"), (55, "В"), (40, "Г"), (25, "Д"), (0, "Е"))
PAUSE_SECONDS = 0.3  # между пачками ростеров — отпустить GIL для цикла событий бота


def _combo_key(combo: tuple) -> str:
    return ",".join(f"{sid}x{mult}" for sid, mult in combo)


def combo_label(key: str) -> str:
    if not key:
        return "без полного набора"
    parts = []
    for item in key.split(","):
        sid, mult = item.split("x")
        name = stat_engine.MOD_SET_NAMES_RU.get(int(sid), f"#{sid}")
        parts.append(f"{name} ×{mult}" if mult != "1" else name)
    return " + ".join(parts)


def _mod_speed(decoded: list) -> float:
    speed = 0.0
    for d in decoded:
        if d["primary"] and d["primary"]["stat_id"] == SPEED_STAT:
            speed += d["primary"]["value"]
        speed += sum(s["value"] for s in d["secondaries"] if s["stat_id"] == SPEED_STAT)
    return speed


def _decoded(unit: dict) -> list:
    return [d for d in (decode_mod(m) for m in unit.get("equippedStatMod") or []) if d]


def _quantiles(values: list) -> list:
    values = sorted(values)
    n = len(values)
    return [round(values[min(n - 1, round(i * (n - 1) / QUANTILES))], 1) for i in range(QUANTILES + 1)]


def build_reference() -> dict:
    """{base_id: {n, prim: {slot: {stat_id: count}}, sets: {combo: count}, speed: [квантили]}}."""
    conn = sqlite3.connect(database.DB_NAME)
    codes = [r[0] for r in conn.execute("SELECT DISTINCT ally_code FROM player_unit_cache")]
    conn.close()
    prim: dict = defaultdict(lambda: defaultdict(Counter))
    sets: dict = defaultdict(Counter)
    speeds: dict = defaultdict(list)
    for i in range(0, len(codes), 20):
        for roster in database.get_player_units_bulk(codes[i:i + 20]).values():
            for base_id, unit in roster.items():
                if not mod_analysis._relevant_unit(unit):
                    continue
                decoded = _decoded(unit)
                for d in decoded:
                    if d["slot_key"] in FLEX_SLOTS and d["primary"]:
                        prim[base_id][d["slot_key"]][d["primary"]["stat_id"]] += 1
                sets[base_id][_combo_key(mod_analysis._significant_sets(decoded))] += 1
                speeds[base_id].append(_mod_speed(decoded))
        time.sleep(PAUSE_SECONDS)
    return {
        b: {"n": len(v), "prim": {s: dict(c) for s, c in prim[b].items()}, "sets": dict(sets[b]),
            "speed": _quantiles(v)}
        for b, v in speeds.items() if len(v) >= MIN_SAMPLES
    }


def refresh_reference(force: bool = False) -> bool:
    """Пересобирает эталон раз в REFRESH_HOURS (зовёт бот после часового синка)."""
    raw = database.get_bot_state(REFERENCE_KEY, guild_id=0)
    if raw and not force:
        try:
            if time.time() - json.loads(raw).get("built", 0) < REFRESH_HOURS * 3600:
                return False
        except ValueError:
            pass
    units = build_reference()
    if not units:
        return False
    database.set_bot_state(REFERENCE_KEY, json.dumps({"built": time.time(), "units": units}), guild_id=0)
    return True


def load_reference() -> dict:
    raw = database.get_bot_state(REFERENCE_KEY, guild_id=0)
    return json.loads(raw).get("units", {}) if raw else {}


def _percentile(value: float, quantiles: list) -> float:
    below = sum(1 for q in quantiles if q < value)
    equal = sum(1 for q in quantiles if q == value)
    return min(100.0, (below + equal / 2) / (len(quantiles) - 1) * 100) if len(quantiles) > 1 else 50.0


def letter(score: float) -> str:
    return next(g for threshold, g in GRADES if score >= threshold)


def grade_unit(unit: dict, ref: dict) -> dict:
    """{score, letter, parts: {primary, sets, speed, quality}, hints: [str], speed}."""
    decoded = _decoded(unit)
    by_slot = {d["slot_key"]: d for d in decoded}
    hints = []

    slot_scores = []
    for slot in FLEX_SLOTS:
        counts = {int(k): v for k, v in (ref["prim"].get(slot) or {}).items()}
        if not counts:
            continue
        top_id, top = max(counts.items(), key=lambda kv: kv[1])
        mod = by_slot.get(slot)
        mine = mod["primary"]["stat_id"] if mod and mod["primary"] else None
        slot_scores.append(counts.get(mine, 0) / top if mine is not None else 0.0)
        if mine != top_id and counts.get(mine, 0) / top < 0.5:
            total = sum(counts.values())
            have = stat_engine.UNIT_STAT_RU.get(mine, "нет мода") if mine is not None else "нет мода"
            hints.append(f"{SLOT_RU[slot]}: у вас {have}, обычно {stat_engine.UNIT_STAT_RU.get(top_id, top_id)} "
                         f"({round(top / total * 100)}%)")
    primary = sum(slot_scores) / len(slot_scores) * 100 if slot_scores else 100.0

    combo = _combo_key(mod_analysis._significant_sets(decoded))
    top_combo, top_n = max(ref["sets"].items(), key=lambda kv: kv[1])
    sets = min(1.0, ref["sets"].get(combo, 0) / top_n) * 100
    if sets < 50:
        hints.append(f"Наборы: у вас {combo_label(combo)}, обычно {combo_label(top_combo)} "
                     f"({round(top_n / ref['n'] * 100)}%)")

    speed_value = _mod_speed(decoded)
    speed = _percentile(speed_value, ref["speed"])
    if speed < 40:
        hints.append(f"Скорость с модов +{speed_value:.0f}, у половины — от +{ref['speed'][QUANTILES // 2]:.0f}, "
                     f"у лучших 20% — от +{ref['speed'][QUANTILES * 4 // 5]:.0f}")

    quality_points = [(1.0 if (d["rarity"] or 0) >= 6 else 0.6 if d["rarity"] == 5 else 0.3)
                      * (1.0 if d["level"] == 15 else 0.6) for d in decoded]
    quality = sum(quality_points) / 6 * 100
    if len(decoded) < 6:
        hints.append(f"Надето модов: {len(decoded)} из 6")
    elif quality < 70:
        hints.append("Моды не 6 точек или не 15 уровня")

    parts = {"primary": primary, "sets": sets, "speed": speed, "quality": quality}
    score = sum(parts[k] * w for k, w in WEIGHTS.items())
    return {"score": round(score), "letter": letter(score), "parts": {k: round(v) for k, v in parts.items()},
            "hints": hints, "speed": round(speed_value)}


def player_grades(roster: dict, min_relic: int = 1) -> dict:
    """Оценки персонажей игрока на R{min_relic}+ с эталоном: {units: [...], counts: {буква: n},
    average}. Слабые — первыми, при равенстве — с большей реликвией (их важнее перемодить)."""
    ref_all = load_reference()
    names = database.get_game_unit_names(list(roster))
    units = []
    for base_id, unit in roster.items():
        ref = ref_all.get(base_id)
        relic = stat_engine.get_current_relic_level(unit)
        if not ref or relic < min_relic or not unit.get("equippedStatMod"):
            continue
        g = grade_unit(unit, ref)
        units.append({"base_id": base_id, "name": names.get(base_id, base_id), "relic": relic, "n": ref["n"], **g})
    units.sort(key=lambda u: (u["score"], -u["relic"], u["name"].lower()))
    counts = Counter(u["letter"] for u in units)
    return {"units": units, "counts": [(g, counts.get(g, 0)) for _t, g in GRADES],
            "average": round(sum(u["score"] for u in units) / len(units)) if units else None,
            "built": bool(ref_all)}
