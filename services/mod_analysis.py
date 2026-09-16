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
другой гильдии на /mod-analysis) кэша нет, участников приходится тянуть живым Comlink по
одному, как services/steal_build.py. Обе ветки считают модель по общей паре
_build_relevant/_compute_report, чтобы сама модель анализа не дублировалась.

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
   уточнено пользователем в чате 2026-09-15).
5. "Направления модинга" — уникальные конфигурации (сет-комбо + primary на 4 гибких слотах,
   без квадрата/ромба — там всегда один фиксированный primary, см. stat_engine.
   MOD_PRIMARY_OPTIONS) с долей игроков > 10%.
"""

import asyncio
import math
import statistics

import database
import stat_engine
from services import mod_search

# Флекс-слоты — форма, где primary реально варьируется (stat_engine.MOD_PRIMARY_OPTIONS
# даёт больше одного варианта); Квадрат/Ромб всегда несут ровно один фиксированный primary —
# не несут информации для "конфигурации билда" (PDF: "// без учета квадрата и ромба").
CONFIG_SLOTS = ("arrow", "triangle", "circle", "cross")

MIN_RELIC = 5  # "Relic > 5" — фильтр релевантности (исключение низкореловых персонажей)
REQUIRED_MOD_COUNT = 6  # "Количество надетых модулей = 6"
REQUIRED_MOD_LEVEL = 15  # "Средний уровень надетых модулей = 15"
SIGNIFICANT_CF_THRESHOLD = 0.10  # "направление модинга" — конфигурация с частотой > 10%
SF_DELTA_THRESHOLD = 0.10  # "явно ненулевая" дельта — > 10% от базы

# Характеристики для анализа — закрытый список, привязанный к таблице сетов из PDF
# (раздел 2: Скорость/Атака/Здоровье/Крит.урон/Крит.шанс/Оборона/Стойкость/Эффективность).
# Генерик "Critical Chance"/"Offense"/"Defense" в финальном словаре StatCalc не существуют
# (см. cogs/stat_requirements.py: только Physical/Special-варианты) — поэтому Атака,
# Крит.шанс распадаются на физ./особую, Оборона — на Броню/Сопротивление. Дельта естественно
# окажется ~0 на "неродном" для конкретного персонажа варианте — не нужно заранее угадывать
# его тип атаки.
ANALYZED_STATS = [
    ("Speed", "Скорость"),
    ("Physical Damage", "Атака (физ.)"),
    ("Special Damage", "Атака (особая)"),
    ("Health", "Здоровье"),
    ("Protection", "Защита"),
    ("Critical Damage", "Крит. урон"),
    ("Physical Critical Chance", "Крит. шанс (физ.)"),
    ("Special Critical Chance", "Крит. шанс (особая)"),
    ("Armor", "Броня"),
    ("Resistance", "Сопротивление"),
    ("Potency", "Эффективность"),
    ("Tenacity", "Стойкость"),
]
STAT_LABELS = dict(ANALYZED_STATS)


def _comparable_base(base_stats: dict, name: str):
    """stat_engine.calc_base_stats хранит Armor/Resistance как СЫРОЙ Defense-рейтинг (см. её
    докстринг), а не %, — final_stats те же статы отдаёт в %. Для дельты нужно единое
    представление, конвертируем базу тем же round-trip, что и stat_engine.
    apply_manual_stat_totals (_defense_to_armor_pct)."""
    value = base_stats.get(name)
    if value is None:
        return None
    if name in stat_engine.NONLINEAR_DEFENSE_STATS:
        return stat_engine._defense_to_armor_pct(value)
    return value


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
    количество модулей этого сета (stat_engine.MOD_SET_PIECE_COUNT)."""
    counts = {}
    for d in decoded_mods:
        counts[d["set_id"]] = counts.get(d["set_id"], 0) + 1
    significant = [sid for sid, cnt in counts.items() if cnt >= stat_engine.MOD_SET_PIECE_COUNT.get(sid, 99)]
    return tuple(sorted(significant))


def _combo_label(set_ids: tuple) -> str:
    if not set_ids:
        return "— без значимого набора —"
    return " + ".join(stat_engine.MOD_SET_IDS.get(str(sid), f"#{sid}") for sid in set_ids)


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
    suffix = "%" if stat_name in stat_engine.PERCENT_STATS else ""
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


def _stat_delta_stats(deltas: list, relevant_count: int) -> dict:
    """deltas — [(delta, base), ...] по всем релевантным игрокам, у которых стат посчитан."""
    n = len(deltas)
    if n == 0:
        return {"n": 0, "sf": None, "p50": None, "p70": None, "p90": None, "spread": None, "cv": None, "nm": None, "gf": None}

    sf_count = sum(1 for d, b in deltas if b and (d / b) > SF_DELTA_THRESHOLD)
    sf = sf_count / relevant_count if relevant_count else 0.0

    sorted_deltas = sorted(d for d, _b in deltas)
    p50 = _percentile(sorted_deltas, 50)
    p70 = _percentile(sorted_deltas, 70)
    p90 = _percentile(sorted_deltas, 90)
    spread = p90 - p50

    cv = None
    if n >= 2:
        mean_delta = statistics.mean(sorted_deltas)
        if mean_delta:
            cv = statistics.stdev(sorted_deltas) / mean_delta

    nm = (p50 / p90) if p90 else 0.0
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


def _compute_report(stat_calc, base_id: str, target_relic: int, relevant: list, total_open: int) -> dict:
    char_name = database.get_game_unit_name(base_id) or base_id
    if not relevant:
        return {
            "error": None, "char_name": char_name, "target_relic": target_relic,
            "total_open": total_open, "relevant_count": 0,
        }

    for p in relevant:
        projected = stat_engine.project_unit_relic(p["unit"], target_relic)
        final_values = stat_engine.calc_final_stats(stat_calc, projected)
        base_values = stat_engine.calc_base_stats(stat_calc, projected)
        base_by_stat = {name: _comparable_base(base_values, name) for name, _label in ANALYZED_STATS}
        p["final"] = final_values
        p["base"] = base_by_stat
        p["delta"] = {
            name: final_values.get(name, 0.0) - base_by_stat[name]
            for name, _label in ANALYZED_STATS if base_by_stat.get(name) is not None
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
    cv_values = []
    for stat_name, label in ANALYZED_STATS:
        deltas = [(p["delta"][stat_name], p["base"][stat_name]) for p in relevant if stat_name in p["delta"]]
        stats = _stat_delta_stats(deltas, relevant_count)
        row = {"stat": stat_name, "label": label, **stats}
        if stats["n"]:
            row["sf_pct"] = f"{stats['sf'] * 100:.0f}%"
            row["p50_fmt"] = _fmt_delta(stats["p50"], stat_name)
            row["p70_fmt"] = _fmt_delta(stats["p70"], stat_name)
            row["p90_fmt"] = _fmt_delta(stats["p90"], stat_name)
            row["spread_fmt"] = _fmt_delta(stats["spread"], stat_name)
            row["cv_fmt"] = f"{stats['cv']:.2f}" if stats["cv"] is not None else "—"
            row["cv_level"] = _cv_level(stats["cv"])
            row["nm_fmt"] = f"{stats['nm']:.2f}"
            row["gf_fmt"] = f"{stats['gf']:.2f}"
            row["gf_level"] = _gf_level(stats["gf"])
            if stats["cv"] is not None:
                cv_values.append(stats["cv"])
        stat_rows.append(row)

    # Второй элемент ключа — фиктивный -1 (ниже любого реального GF) для строк без данных,
    # иначе сравнение двух None внутри одной группы валит sort() с TypeError.
    stat_rows.sort(key=lambda r: (r["gf"] is not None, r["gf"] if r["gf"] is not None else -1), reverse=True)
    for i, row in enumerate(stat_rows, start=1):
        row["rank"] = i if row["gf"] is not None else None
    top3_stats = [r for r in stat_rows if r["gf"] is not None][:3]

    consensus = (1 - statistics.mean(cv_values)) if cv_values else None

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
            deltas = sorted(m["delta"][stat_name] for m in members if stat_name in m["delta"])
            if not deltas:
                continue
            n = len(deltas)
            cv = None
            if n >= 2:
                mean_d = statistics.mean(deltas)
                if mean_d:
                    cv = statistics.stdev(deltas) / mean_d
            dir_stats.append({
                "label": label,
                "p50_fmt": _fmt_delta(_percentile(deltas, 50), stat_name),
                "p70_fmt": _fmt_delta(_percentile(deltas, 70), stat_name),
                "p90_fmt": _fmt_delta(_percentile(deltas, 90), stat_name),
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
    }


async def build_report(stat_calc, base_id: str, target_relic: int, guild_id: int = 1) -> dict:
    """Анализ по СВОЕЙ (обслуживаемой) гильдии — данные из уже закэшированного
    player_unit_cache, без обращений к Comlink (см. докстринг модуля)."""
    roster = database.get_all_user_mappings(guild_id)
    if not roster:
        return {"error": "Никто из гильдии не зарегистрирован (/регистрация) — анализировать некого."}

    ally_codes = [ac for _discord_id, ac, _name in roster]
    units_by_ally = database.get_player_units_bulk(ally_codes)
    roster_units = [
        (ally_code, name, units_by_ally.get(ally_code, {}).get(base_id))
        for _discord_id, ally_code, name in roster
    ]
    relevant, total_open = _build_relevant(base_id, roster_units)
    return _compute_report(stat_calc, base_id, target_relic, relevant, total_open)


async def build_report_live(comlink, stat_calc, base_id: str, target_relic: int, guild) -> dict:
    """Анализ по ЧУЖОЙ гильдии (добавлено 2026-09-16, по прямому запросу пользователя —
    "чужую гильдию, а не только свою") — `guild` это services.steal_build.GuildLookupResult,
    уже отресолвленный по коду союзника/ID гильдии (см. web/routes/mod_analysis.py). В отличие
    от build_report, участников чужой гильдии приходится тянуть живым
    comlink.get_player(player_id=...) по одному — та же плата и тот же троттлинг, что
    services/steal_build.py::build_report (для крупной гильдии может занять до минуты)."""
    if not guild.members:
        return {"error": f"В гильдии «{guild.guild_name}» нет участников."}

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
    report = _compute_report(stat_calc, base_id, target_relic, relevant, total_open)
    if report["error"] is None:
        report["guild_name"] = guild.guild_name
        report["swgoh_guild_id"] = guild.swgoh_guild_id
        report["total_members"] = len(guild.members)
        report["fetch_errors"] = fetch_errors
    return report
