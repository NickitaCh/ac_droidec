"""Модель анализа модинга персонажа по гильдии (веб /mod-analysis) — реализация ТЗ из
"Описание модели анализа модинга персонажа по гильдии.pdf" (получено от пользователя
2026-09-15). Для одного выбранного персонажа и одного целевого уровня реликвии показывает,
как гильдия в среднем модит его: какие сеты/основы популярны, какие характеристики
получают наибольший осознанный вклад от модов (не от реликвии/шмота), и сколько в гильдии
разных "направлений" сборки.

Данные для СВОЕЙ гильдии (build_report) — уже закэшированный player_unit_cache
(database.get_player_units_bulk), без живых обращений к Comlink — тот же приём, что
services/mod_search.py (поиск по 50-90 игрокам не должен бить по Comlink на каждый вызов
страницы). Для ЧУЖОЙ гильдии (build_report_live, добавлено 2026-09-16 — код союзника/ID любой
другой гильдии на /mod-analysis) участников приходится тянуть живым Comlink по одному, как
services/steal_build.py. Обе ветки считают модель по общей паре _build_relevant/
_compute_player_deltas+_aggregate_report, чтобы сама модель анализа не дублировалась;
результат _compute_player_deltas (дорогая часть) дополнительно кэшируется в _REPORT_CACHE
(см. её докстринг) — переключатель Δ/%/Итог (view) бьёт только по дешёвой _aggregate_report.

Модель (см. PDF, раздел "Порядок расчётов"):
1. Фильтр релевантных игроков — см. _relevant_unit.
2. Для каждого релевантного игрока моды проецируются на ОДИН выбранный целевой релик
   (stat_engine.project_unit_relic — тот же приём, что /статы_релик и /steal-build),
   реальные моды остаются как есть. БАЗА — статы БЕЗ единого мода на этом же целевом
   релике (stat_engine.calc_base_stats), ИТОГ — статы С реальными модами игрока на том же
   релике (stat_engine.calc_final_stats). Так дельта = вклад модов, ОТДЕЛЁННЫЙ от разницы
   в реликвии между игроками (все нормированы на один и тот же целевой релик).
3. Сеты/основы/конфигурации — переиспользует services.mod_search.decode_mod (уже проверенное
   декодирование definitionId -> сет/слот/primary, см. её докстринг про эмпирическую сверку
   слотов).
4. Статистика по дельте на характеристику (SF/p50/p70/p90/Spread/CV/NM/GF) — по формулам PDF.
   CV = StdDev(выборочный, N-1) / MeanDelta (ДЕЛЕНИЕ — в самом PDF была опечатка "минус",
   уточнено пользователем в чате 2026-09-15). SF считается по СВОЕМУ порогу на каждый стат
   (STAT_SF_THRESHOLD, PDF v3 раздел 4) — не единым 10%-от-базы правилом, как было раньше
   (см. докстринг STAT_SF_THRESHOLD).
5. "Направления модинга" — уникальные конфигурации (сет-комбо + primary на 4 гибких слотах,
   без квадрата/ромба — там всегда один фиксированный primary, см. stat_engine.
   MOD_PRIMARY_OPTIONS) с долей игроков > 10%.
6. Итоговый ConsensusScore (CS) — по PDF, раздел 4: WeightedCV = Σ(CV·GF) / Σ(GF)
   (среднее CV по статам, взвешенное их GF — статы, в которые гильдия почти не
   вкладывается, почти не влияют на консенсус), CS = 1 − WeightedCV. НЕ простое
   среднее CV по статам — этот баг уже был и исправлен (нашёл пользователь 2026-09-16).

Реализовано по "Описание модели анализа модинга персонажа по гильдии v3.pdf" (получено от
пользователя 2026-09-17, лежал на Desktop рядом с v1/v2 — см. ANALYZED_STATS/STAT_SF_THRESHOLD
ниже про то, что именно изменилось относительно v2).
"""

import asyncio
import math
import statistics
import time

import database
import stat_engine
from services import mod_search

# Кэш дорогой части отчёта (project_unit_relic + calc_final_stats/calc_base_stats на КАЖДОГО
# релевантного игрока — 50-90 игроков x 2 StatCalc-вызова) отдельно от дешёвой агрегации/
# форматирования под view. Раньше переключатель Δ/%/Итог (web/routes/mod_analysis.py,
# _view_urls) делал обычный GET той же страницы — это гоняло ВСЮ модель заново, включая
# этот цикл, только чтобы поменять представление уже посчитанных чисел (жалоба пользователя
# 2026-09-22: "тыкнул на другой режим отображения, и он секунд 30 крутит... по идее всю базу
# на конкретный запрос он и так должен иметь"). Ключ — (scope, base_id, target_relic), НЕ
# включает view. TTL — не бессрочно: player_unit_cache/состав гильдии меняются (часовой
# ресинк роста, см. CLAUDE.md про ViolationsCog), и это веб-процесс, который не перезапускается
# часто, так что без TTL кэш реально бы протух и рос без границ.
_REPORT_CACHE: dict = {}
_CACHE_TTL_SECONDS = 15 * 60
_CACHE_MAX_ENTRIES = 100


def _cache_get(key):
    entry = _REPORT_CACHE.get(key)
    if entry is None:
        return None
    ts, relevant, total_open, extra = entry
    if time.monotonic() - ts > _CACHE_TTL_SECONDS:
        _REPORT_CACHE.pop(key, None)
        return None
    return relevant, total_open, extra


def _cache_put(key, relevant, total_open, extra=None):
    if len(_REPORT_CACHE) >= _CACHE_MAX_ENTRIES:
        _REPORT_CACHE.pop(next(iter(_REPORT_CACHE)), None)  # вытеснить самый старый (dict упорядочен по вставке)
    _REPORT_CACHE[key] = (time.monotonic(), relevant, total_open, extra or {})

# Флекс-слоты — форма, где primary реально варьируется (stat_engine.MOD_PRIMARY_OPTIONS
# даёт больше одного варианта); Квадрат/Ромб всегда несут ровно один фиксированный primary —
# не несут информации для "конфигурации билда" (PDF: "// без учета квадрата и ромба").
CONFIG_SLOTS = ("arrow", "triangle", "circle", "cross")

MIN_RELIC = 5  # "Relic > 5" — фильтр релевантности (исключение низкореловых персонажей)
REQUIRED_MOD_COUNT = 6  # "Количество надетых модулей = 6"
REQUIRED_MOD_LEVEL = 15  # "Средний уровень надетых модулей = 15"
SIGNIFICANT_CF_THRESHOLD = 0.10  # "направление модинга" — конфигурация с частотой > 10%

# Характеристики для анализа — по PDF v3 (раздел 3: "Группировка и пересчёт характеристик").
# Относительно v2 переработано втроём:
# 1. Атака/Крит.шанс больше НЕ распадаются на физ./особую как отдельные строки отчёта — PDF v3
#    явно требует усреднять их (см. _attack_and_crit_chance_delta ниже), чтобы "неродной" для
#    персонажа тип урона не считался отдельным неинвестируемым статом, а просто наполовину
#    гасил среднее (раньше это давало на выходе два ряда, один из которых всегда ~0).
# 2. Броня/Сопротивление больше не сравниваются как отдельные %-статы — вместо этого PDF v3
#    вводит производную "Оборону" (см. _defense_delta), а Сопротивление ("сопротивление не
#    учитываем вообще") выброшено из модели целиком.
# 3. У каждого стата теперь СВОЙ порог "осмысленного упора" (STAT_SF_THRESHOLD) вместо единого
#    10%-от-базы, применявшегося раньше ко всем статам без разбора.
ANALYZED_STATS = [
    ("Critical Damage", "Крит. урон"),
    ("Attack", "Атака (усредн.)"),
    ("Speed", "Скорость"),
    ("Health", "Здоровье"),
    ("Protection", "Защита"),
    ("Critical Chance", "Крит. шанс (усредн.)"),
    ("Defense", "Оборона (от брони)"),
    ("Potency", "Эффективность"),
    ("Tenacity", "Стойкость"),
]
STAT_LABELS = dict(ANALYZED_STATS)

# "Порог осмысленного упора" на каждую характеристику (PDF v3, раздел 4) — ("absolute", X):
# дельта > X в собственных единицах стата (без деления на базу); ("relative", X): дельта/база
# > X. В v2 здесь стояло единое SF_DELTA_THRESHOLD = 0.10 (10% от базы на всё) — та же PDF (v2)
# параллельно уже описывала эту самую по-статовую таблицу на стр. 3, но формула GF на стр. 5
# буквально разворачивала SF как ">10% от базы", что ей прямо противоречило (см.
# [[project_mod_analysis_feature]], "Scope note deliberately NOT taken further" 2026-09-16) —
# implementировать её тогда сознательно не стали. PDF v3 это расхождение убрала (формула GF на
# стр. 5 теперь ссылается общим "> порога", без "10% от базы"), поэтому здесь применяется
# таблица.
STAT_SF_THRESHOLD = {
    "Critical Damage": ("absolute", 30.0),
    "Attack": ("relative", 0.25),
    "Speed": ("absolute", 100.0),
    "Health": ("relative", 0.42),
    "Protection": ("relative", 0.48),
    "Critical Chance": ("absolute", 15.0),
    "Defense": ("relative", 0.40),
    "Potency": ("absolute", 30.0),
    "Tenacity": ("absolute", 35.0),
}

# Статы, показываемые с суффиксом "%" (_fmt_delta) — те же, что раньше входили в
# stat_engine.PERCENT_STATS, но именами после усреднения/переименования в ANALYZED_STATS
# (Attack/Defense — сырые числа, не %; Speed/Health/Protection — тоже).
PERCENT_DISPLAY_STATS = frozenset({"Critical Damage", "Critical Chance", "Potency", "Tenacity"})


def _attack_and_crit_chance_delta(final_values: dict, base_values: dict) -> dict:
    """PDF v3, раздел 3: усредненная атака/крит.шанс — среднее физической и особой дельты (не
    сумма), чтобы не задваивать вклад и не искажать итог "неродным" для персонажа типом урона
    (Attack_Delta = (PhysAttack_Delta + SpecAttack_Delta) / 2, аналогично для крит.шанса). База
    для относительного SF-порога усредняется тем же способом, симметрично дельте."""
    result = {}
    for out_name, phys_name, spec_name in (
        ("Attack", "Physical Damage", "Special Damage"),
        ("Critical Chance", "Physical Critical Chance", "Special Critical Chance"),
    ):
        phys_delta = final_values.get(phys_name, 0.0) - base_values.get(phys_name, 0.0)
        spec_delta = final_values.get(spec_name, 0.0) - base_values.get(spec_name, 0.0)
        result[out_name] = {
            "delta": (phys_delta + spec_delta) / 2,
            "base": (base_values.get(phys_name, 0.0) + base_values.get(spec_name, 0.0)) / 2,
        }
    return result


def _defense_delta(final_values: dict, base_values: dict) -> dict:
    """PDF v3, раздел 3: "пересчитываем броню в оборону... сопротивление не учитываем вообще".
    Defense = Armor*637.5/(100-Armor) — тот же коэффициент 637.5, что уже подтверждён и
    используется в stat_engine._armor_pct_to_defense (сверено с пользователем 2026-08-24, см.
    её докстринг), переиспользуем эту функцию, а не переизобретаем формулу. final_values["Armor"]
    — итоговый % (calc_final_stats), конвертируем в сырой Defense-рейтинг; base_values["Armor"]
    calc_base_stats УЖЕ отдаёт сырым Defense-рейтингом (см. её докстринг) — конвертировать
    повторно не нужно, раньше (v2) было наоборот: базу переводили В %, чтобы сравнить с final%."""
    final_defense = stat_engine._armor_pct_to_defense(final_values.get("Armor", 0.0))
    base_defense = base_values.get("Armor", 0.0)
    return {"delta": final_defense - base_defense, "base": base_defense}


def _relevant_unit(unit: dict) -> bool:
    mods = unit.get("equippedStatMod") or []
    if len(mods) != REQUIRED_MOD_COUNT:
        return False
    if stat_engine.get_current_relic_level(unit) <= MIN_RELIC:
        return False
    levels = [m.get("level") or 0 for m in mods]
    if sum(levels) / len(levels) != REQUIRED_MOD_LEVEL:
        return False
    return True


def _percentile(sorted_values: list, pct: float) -> float:
    """Линейная интерполяция (тот же метод, что numpy.percentile по умолчанию) — своя
    реализация, т.к. numpy не в зависимостях проекта (requirements.txt)."""
    n = len(sorted_values)
    if n == 0:
        return 0.0
    if n == 1:
        return sorted_values[0]
    k = (pct / 100) * (n - 1)
    f = math.floor(k)
    c = math.ceil(k)
    if f == c:
        return sorted_values[int(k)]
    return sorted_values[f] + (sorted_values[c] - sorted_values[f]) * (k - f)


def _significant_sets(decoded_mods: list) -> tuple:
    """Сет считается значимым, если надето хотя бы минимально необходимое для бонуса
    количество модулей этого сета (stat_engine.MOD_SET_PIECE_COUNT). Возвращает (set_id,
    множитель) — множитель = во сколько раз бонус сета активен (6 модов 2-шт. сета = "х3")
    — раньше комбо хранило только id без множителя, из-за чего "Эффективность + Здоровье"
    не показывало, 4 Эфки + 2 ХП или наоборот (запрос пользователя 2026-09-22)."""
    counts = {}
    for d in decoded_mods:
        counts[d["set_id"]] = counts.get(d["set_id"], 0) + 1
    significant = [
        (sid, cnt // stat_engine.MOD_SET_PIECE_COUNT.get(sid, 99))
        for sid, cnt in counts.items()
        if cnt >= stat_engine.MOD_SET_PIECE_COUNT.get(sid, 99)
    ]
    return tuple(sorted(significant))


def _combo_label(set_ids: tuple) -> str:
    if not set_ids:
        return "— без значимого набора —"
    parts = []
    for sid, multiplier in set_ids:
        name = stat_engine.MOD_SET_IDS.get(str(sid), f"#{sid}")
        parts.append(f"{name} x{multiplier}" if multiplier > 1 else name)
    return " + ".join(parts)


def _config_key(decoded_mods: list) -> tuple:
    by_slot = {d["slot_key"]: d for d in decoded_mods}
    combo = _significant_sets(decoded_mods)
    primaries = tuple(
        (by_slot[k]["primary"]["stat_id"] if k in by_slot and by_slot[k].get("primary") else None)
        for k in CONFIG_SLOTS
    )
    return combo, primaries


def _config_label_parts(config_key: tuple) -> tuple:
    """Сеты и основы отдельно — на странице сеты идут первой строкой заголовка, основы
    второй (см. web/templates/mod_analysis.html, блок "Направления модинга")."""
    combo, primaries = config_key
    primary_parts = []
    for slot_key, stat_id in zip(CONFIG_SLOTS, primaries):
        if stat_id is None:
            continue
        name, _is_pct = mod_search.STAT_ID_TO_INFO.get(stat_id, (f"#{stat_id}", False))
        primary_parts.append(f"{mod_search.SLOT_KEY_TO_LABEL[slot_key]}: {name}")
    return _combo_label(combo), ", ".join(primary_parts)


def _fmt_delta(value: float, stat_name: str) -> str:
    suffix = "%" if stat_name in PERCENT_DISPLAY_STATS else ""
    return mod_search.fmt_value(value) + suffix


def _gf_level(gf) -> str | None:
    if gf is None:
        return None
    if gf > 0.8:
        return "Сильный согласованный фокус"
    if gf >= 0.6:
        return "Хороший фокус"
    if gf >= 0.4:
        return "Средний фокус"
    return "Слабый или вариативный фокус"


def _cv_level(cv) -> str | None:
    if cv is None:
        return None
    cv = abs(cv)
    if cv < 0.15:
        return "Очень высокая согласованность"
    if cv < 0.30:
        return "Хорошая согласованность"
    if cv < 0.60:
        return "Умеренная вариативность"
    if cv < 1.00:
        return "Высокая вариативность"
    return "Очень высокая вариативность"


def _consensus_level(cs) -> str:
    if cs is None:
        return "н/д"
    if cs > 0.7:
        return "Высокий консенсус (одинаковый модинг)"
    if cs >= 0.4:
        return "Средний консенсус (есть несколько вариантов)"
    return "Низкий консенсус (много разных вариантов)"


def _meets_threshold(delta: float, base: float, threshold: tuple) -> bool:
    kind, value = threshold
    if kind == "absolute":
        return delta > value
    return bool(base) and (delta / base) > value


VIEWS = ("delta", "pct", "final")
VIEW_SUFFIX = {"delta": "Δ", "pct": "%", "final": "итог"}


def _display_sorted(pairs: list, view: str) -> list:
    """pairs — [(delta, base), ...]. Переключатель Δ/%/Итог на странице (запрос
    пользователя 2026-09-22, чат #ac-оф-модинг — раньше p50/p70/p90 показывали только
    сырую дельту, из-за чего было неясно, это большая дельта от маленькой базы или
    наоборот): "pct" — дельта как % от базы игрока, "final" — итоговый стат (база+дельта)."""
    if view == "pct":
        return sorted((d / b * 100) if b else 0.0 for d, b in pairs)
    if view == "final":
        return sorted(b + d for d, b in pairs)
    return sorted(d for d, _b in pairs)


def _fmt_for_view(value: float, stat_name: str, view: str) -> str:
    if view == "pct":
        return mod_search.fmt_value(value) + "%"
    return _fmt_delta(value, stat_name)  # "delta" и "final" — та же единица, что и стат


def _stat_delta_stats(deltas: list, relevant_count: int, threshold: tuple, view: str = "delta") -> dict:
    """deltas — [(delta, base), ...] по всем релевантным игрокам, у которых стат посчитан.
    threshold — STAT_SF_THRESHOLD[stat_name], ("absolute"|"relative", значение). view влияет
    ТОЛЬКО на то, что показывается в p50/p70/p90/spread — ранг/NM/GF/CV всегда считаются по
    сырой дельте (delta_p50/delta_p90 ниже), не зависят от выбранного представления."""
    n = len(deltas)
    if n == 0:
        return {"n": 0, "sf": None, "p50": None, "p70": None, "p90": None, "spread": None, "cv": None, "nm": None, "gf": None}

    sf_count = sum(1 for d, b in deltas if _meets_threshold(d, b, threshold))
    sf = sf_count / relevant_count if relevant_count else 0.0

    sorted_deltas = sorted(d for d, _b in deltas)
    delta_p50 = _percentile(sorted_deltas, 50)
    delta_p90 = _percentile(sorted_deltas, 90)

    display_sorted = _display_sorted(deltas, view)
    p50 = _percentile(display_sorted, 50)
    p70 = _percentile(display_sorted, 70)
    p90 = _percentile(display_sorted, 90)
    spread = p90 - p50

    # PDF раздел 4: если SF >= 70%, CV считается по ВСЕЙ выборке релевантных игроков;
    # если SF < 70%, CV считается только по тем, у кого дельта уже превышает тот же
    # "порог осмысленного упора" (STAT_SF_THRESHOLD), что и в SF — иначе стат, которым
    # реально пользуется меньшинство гильдии, разбавляется морем игроков с почти нулевой
    # дельтой и получает обманчиво огромный/бессмысленный CV.
    cv_population = sorted_deltas if sf >= 0.70 else sorted(d for d, b in deltas if _meets_threshold(d, b, threshold))
    cv = None
    if len(cv_population) >= 2:
        mean_delta = statistics.mean(cv_population)
        if mean_delta:
            cv = statistics.stdev(cv_population) / mean_delta

    nm = (delta_p50 / delta_p90) if delta_p90 else 0.0
    gf = sf * nm

    return {"n": n, "sf": sf, "p50": p50, "p70": p70, "p90": p90, "spread": spread, "cv": cv, "nm": nm, "gf": gf}


def _build_relevant(base_id: str, roster_units: list) -> tuple:
    """roster_units: [(id_label, name, unit_or_None), ...] — id_label — код союзника (своя
    гильдия) либо playerId (чужая гильдия, см. build_report_live), только для отображения.
    Возвращает (relevant, total_open) — общая часть между своей (build_report) и чужой
    (build_report_live) гильдией, чтобы модель анализа не дублировалась."""
    relevant = []
    total_open = 0
    for id_label, name, unit in roster_units:
        if not unit:
            continue
        total_open += 1
        if not _relevant_unit(unit):
            continue
        decoded_mods = [d for d in (mod_search.decode_mod(m) for m in unit["equippedStatMod"]) if d is not None]
        if len(decoded_mods) != REQUIRED_MOD_COUNT:
            continue  # повреждённые/нечитаемые моды — не должно случаться, но не рушим отчёт
        relevant.append({
            "ally_code": id_label, "name": name, "unit": unit,
            "current_relic": stat_engine.get_current_relic_level(unit),
            "decoded_mods": decoded_mods,
            "config_key": _config_key(decoded_mods),
        })
    return relevant, total_open


def _compute_player_deltas(stat_calc, target_relic: int, relevant: list) -> None:
    """Дорогая часть отчёта — мутирует каждый relevant[i] полями final/base/delta.
    project_unit_relic + calc_final_stats + calc_base_stats на КАЖДОГО релевантного игрока —
    вынесено из _aggregate_report, чтобы результат можно было закэшировать (_REPORT_CACHE) и
    не пересчитывать при одной только смене view (см. докстринг кэша выше)."""
    for p in relevant:
        projected = stat_engine.project_unit_relic(p["unit"], target_relic)
        final_values = stat_engine.calc_final_stats(stat_calc, projected)
        base_values = stat_engine.calc_base_stats(stat_calc, projected)

        composite = _attack_and_crit_chance_delta(final_values, base_values)
        composite["Defense"] = _defense_delta(final_values, base_values)

        base_by_stat = {}
        delta_by_stat = {}
        for name, _label in ANALYZED_STATS:
            if name in composite:
                base_by_stat[name] = composite[name]["base"]
                delta_by_stat[name] = composite[name]["delta"]
            else:
                base_by_stat[name] = base_values.get(name, 0.0)
                delta_by_stat[name] = final_values.get(name, 0.0) - base_by_stat[name]

        p["final"] = final_values
        p["base"] = base_by_stat
        p["delta"] = delta_by_stat


def _aggregate_report(base_id: str, target_relic: int, relevant: list, total_open: int, view: str = "delta") -> dict:
    """Дешёвая часть — сеты/основы/статистика по дельте/направления/консенсус, целиком на
    уже посчитанных relevant[i]["final"/"base"/"delta"] (см. _compute_player_deltas). Только
    эта функция знает про view — переключатель Δ/%/Итог должен звать ТОЛЬКО её, не пересчитывая
    per-player StatCalc заново."""
    if view not in VIEWS:
        view = "delta"
    char_name = database.get_game_unit_name(base_id) or base_id
    if not relevant:
        return {
            "error": None, "char_name": char_name, "target_relic": target_relic,
            "total_open": total_open, "relevant_count": 0,
        }

    relevant_count = len(relevant)
    avg_relic = statistics.mean(p["current_relic"] for p in relevant)

    # --- Сеты ---
    combo_counts = {}
    for p in relevant:
        combo = p["config_key"][0]
        combo_counts[combo] = combo_counts.get(combo, 0) + 1
    top_combos = [
        {"label": _combo_label(combo), "count": count, "freq_pct": f"{count / relevant_count * 100:.0f}%"}
        for combo, count in sorted(combo_counts.items(), key=lambda kv: kv[1], reverse=True)[:3]
    ]

    # --- Основы по слотам ---
    slot_primary_counts = {slot_key: {} for slot_key, _label in mod_search.SLOT_CHOICES}
    for p in relevant:
        by_slot = {d["slot_key"]: d for d in p["decoded_mods"]}
        for slot_key, _label in mod_search.SLOT_CHOICES:
            d = by_slot.get(slot_key)
            stat_id = d["primary"]["stat_id"] if d and d.get("primary") else None
            if stat_id is None:
                continue
            bucket = slot_primary_counts[slot_key]
            bucket[stat_id] = bucket.get(stat_id, 0) + 1

    top_primaries_by_slot = []
    for slot_key, slot_label in mod_search.SLOT_CHOICES:
        if slot_key not in CONFIG_SLOTS:
            continue  # квадрат/ромб — фиксированный primary, "топ-3" там не несёт информации
        bucket = slot_primary_counts[slot_key]
        total = sum(bucket.values())
        rows = [
            {
                "name": mod_search.STAT_ID_TO_INFO.get(sid, (f"#{sid}", False))[0],
                "count": cnt, "freq_pct": f"{cnt / total * 100:.0f}%" if total else "0%",
            }
            for sid, cnt in sorted(bucket.items(), key=lambda kv: kv[1], reverse=True)[:3]
        ]
        if rows:
            top_primaries_by_slot.append({"slot_label": slot_label, "rows": rows})

    # --- Статистика по дельте на характеристику ---
    stat_rows = []
    cv_gf_pairs = []
    for stat_name, label in ANALYZED_STATS:
        deltas = [(p["delta"][stat_name], p["base"][stat_name]) for p in relevant if stat_name in p["delta"]]
        stats = _stat_delta_stats(deltas, relevant_count, STAT_SF_THRESHOLD[stat_name], view=view)
        row = {"stat": stat_name, "label": label, **stats}
        if stats["n"]:
            row["sf_pct"] = f"{stats['sf'] * 100:.0f}%"
            row["p50_fmt"] = _fmt_for_view(stats["p50"], stat_name, view)
            row["p70_fmt"] = _fmt_for_view(stats["p70"], stat_name, view)
            row["p90_fmt"] = _fmt_for_view(stats["p90"], stat_name, view)
            row["spread_fmt"] = _fmt_for_view(stats["spread"], stat_name, view)
            row["cv_fmt"] = f"{stats['cv']:.2f}" if stats["cv"] is not None else "—"
            row["cv_level"] = _cv_level(stats["cv"])
            row["nm_fmt"] = f"{stats['nm']:.2f}"
            row["gf_fmt"] = f"{stats['gf']:.2f}"
            row["gf_level"] = _gf_level(stats["gf"])
            if stats["cv"] is not None:
                cv_gf_pairs.append((stats["cv"], stats["gf"]))
        stat_rows.append(row)

    # Второй элемент ключа — фиктивный -1 (ниже любого реального GF) для строк без данных,
    # иначе сравнение двух None внутри одной группы валит sort() с TypeError.
    stat_rows.sort(key=lambda r: (r["gf"] is not None, r["gf"] if r["gf"] is not None else -1), reverse=True)
    for i, row in enumerate(stat_rows, start=1):
        row["rank"] = i if row["gf"] is not None else None
    top3_stats = [r for r in stat_rows if r["gf"] is not None][:3]

    # PDF раздел 4: WeightedCV = Σ(CV·GF) / Σ(GF) — статы, в которые гильдия почти не
    # вкладывается (низкий GF, например "чужой" тип урона персонажа с CV, раздутым
    # делением на близкую к нулю MeanDelta), почти не влияют на итоговый консенсус.
    # Раньше здесь было простое (невзвешенное) среднее CV — из-за этого такие
    # нерелевантные статы могли необоснованно занижать ConsensusScore.
    total_gf = sum(gf for _cv, gf in cv_gf_pairs)
    weighted_cv = (sum(cv * gf for cv, gf in cv_gf_pairs) / total_gf) if total_gf else None
    consensus = (1 - weighted_cv) if weighted_cv is not None else None

    # --- Направления модинга (конфигурации с частотой > 10%) ---
    config_counts = {}
    for p in relevant:
        config_counts[p["config_key"]] = config_counts.get(p["config_key"], 0) + 1

    directions = []
    for config_key, count in sorted(config_counts.items(), key=lambda kv: kv[1], reverse=True):
        freq = count / relevant_count
        if freq <= SIGNIFICANT_CF_THRESHOLD:
            continue
        members = [p for p in relevant if p["config_key"] == config_key]
        dir_stats = []
        for stat_name, label in ANALYZED_STATS:
            pairs = [(m["delta"][stat_name], m["base"][stat_name]) for m in members if stat_name in m["delta"]]
            if not pairs:
                continue
            deltas_only = sorted(d for d, _b in pairs)
            n = len(deltas_only)
            cv = None
            if n >= 2:
                mean_d = statistics.mean(deltas_only)
                if mean_d:
                    cv = statistics.stdev(deltas_only) / mean_d
            display_sorted = _display_sorted(pairs, view)
            dir_stats.append({
                "label": label,
                "p50_fmt": _fmt_for_view(_percentile(display_sorted, 50), stat_name, view),
                "p70_fmt": _fmt_for_view(_percentile(display_sorted, 70), stat_name, view),
                "p90_fmt": _fmt_for_view(_percentile(display_sorted, 90), stat_name, view),
                "cv_fmt": f"{cv:.2f}" if cv is not None else "—",
            })
        sets_label, primaries_label = _config_label_parts(config_key)
        directions.append({
            "label": sets_label, "primaries_label": primaries_label, "count": count,
            "freq_pct": f"{freq * 100:.0f}%",
            "avg_relic": statistics.mean(m["current_relic"] for m in members),
            "stats": dir_stats,
        })

    return {
        "error": None,
        "char_name": char_name,
        "target_relic": target_relic,
        "total_open": total_open,
        "relevant_count": relevant_count,
        "avg_relic": avg_relic,
        "top_combos": top_combos,
        "top_primaries_by_slot": top_primaries_by_slot,
        "stat_rows": stat_rows,
        "top3_stats": top3_stats,
        "consensus": consensus,
        "consensus_fmt": f"{consensus:.2f}" if consensus is not None else "—",
        "consensus_level": _consensus_level(consensus),
        "directions": directions,
        "directions_count": len(directions),
        "view": view,
        "view_suffix": VIEW_SUFFIX[view],
    }


async def build_report(stat_calc, base_id: str, target_relic: int, guild_id: int = 1, view: str = "delta") -> dict:
    """Анализ по СВОЕЙ (обслуживаемой) гильдии — данные из уже закэшированного
    player_unit_cache, без обращений к Comlink (см. докстринг модуля)."""
    roster = database.get_all_user_mappings(guild_id)
    if not roster:
        return {"error": "Никто из гильдии не зарегистрирован (/регистрация) — анализировать некого."}

    cache_key = ("own", guild_id, base_id, target_relic)
    cached = _cache_get(cache_key)
    if cached is not None:
        relevant, total_open, _extra = cached
    else:
        ally_codes = [ac for _discord_id, ac, _name in roster]
        units_by_ally = database.get_player_units_bulk(ally_codes)
        roster_units = [
            (ally_code, name, units_by_ally.get(ally_code, {}).get(base_id))
            for _discord_id, ally_code, name in roster
        ]
        relevant, total_open = _build_relevant(base_id, roster_units)
        _compute_player_deltas(stat_calc, target_relic, relevant)
        _cache_put(cache_key, relevant, total_open)

    return _aggregate_report(base_id, target_relic, relevant, total_open, view=view)


async def build_report_live(comlink, stat_calc, base_id: str, target_relic: int, guild, view: str = "delta") -> dict:
    """Анализ по ЧУЖОЙ гильдии (добавлено 2026-09-16, по прямому запросу пользователя —
    "чужую гильдию, а не только свою") — `guild` это services.steal_build.GuildLookupResult,
    уже отресолвленный по коду союзника/ID гильдии (см. web/routes/mod_analysis.py). В отличие
    от build_report, участников чужой гильдии приходится тянуть живым
    comlink.get_player(player_id=...) по одному — та же плата и тот же троттлинг, что
    services/steal_build.py::build_report (для крупной гильдии может занять до минуты)."""
    if not guild.members:
        return {"error": f"В гильдии «{guild.guild_name}» нет участников."}

    cache_key = ("live", guild.swgoh_guild_id, base_id, target_relic)
    cached = _cache_get(cache_key)
    if cached is not None:
        relevant, total_open, extra = cached
        fetch_errors = extra.get("fetch_errors", 0)
    else:
        roster_units = []
        fetch_errors = 0
        for player_id, player_name in guild.members:
            try:
                player_data = await asyncio.to_thread(comlink.get_player, player_id=player_id)
            except Exception:
                fetch_errors += 1
                continue
            roster = player_data.get("rosterUnit") or player_data.get("roster") or []
            unit = next(
                (u for u in roster if (u.get("baseId") or (u.get("definitionId", "") or "").split(":")[0]) == base_id),
                None,
            )
            roster_units.append((player_id, player_name, unit))
            await asyncio.sleep(0.1)

        relevant, total_open = _build_relevant(base_id, roster_units)
        _compute_player_deltas(stat_calc, target_relic, relevant)
        _cache_put(cache_key, relevant, total_open, extra={"fetch_errors": fetch_errors})

    report = _aggregate_report(base_id, target_relic, relevant, total_open, view=view)
    if report["error"] is None:
        report["guild_name"] = guild.guild_name
        report["swgoh_guild_id"] = guild.swgoh_guild_id
        report["total_members"] = len(guild.members)
        report["fetch_errors"] = fetch_errors
    return report
