"""Read-only агрегации для веб-дашборда (состав/ТБ/нарушения) — только запросы
к database.py и подсчёты, без HTML (шаблоны — в web/routes/guild_dashboard.py).
Логика подсчётов ("последние 90 дней", порог 🚨 нарушений) намеренно скопирована
1:1 из cogs/guild_events.py и cogs/violations.py, чтобы дашборд показывал те же
цифры, что и команды бота в Discord. Просадка по ТБ — исключение: считается общей
функцией compute_tb_regressions из cogs/guild_events.py (было 2 независимые копии
до 2026-08-25, разошлись бы при следующей правке порога)."""

import json
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import database

MSK = ZoneInfo("Europe/Moscow")
from cogs.guild_events import (
    TB_ACTION_LABELS, TB_CONFLICT_LABELS, TB_HIDDEN_ZONE_ACTIONS,
    TB_REGRESSION_BASELINE_SIZE, compute_tb_regressions,
)

# Дублирует main.py::N_LIMIT (порог 🚨 для нарушений) — тот же паттерн
# дублирования операторских констант, что и COMLINK_URL/officer-роль (см. CLAUDE.md).
N_LIMIT = 3

WARN_CATEGORIES = ("ТБ", "ВГ", "Рейд")


RANK_LABELS = {4: "ГМ", 3: "Офицер", 2: "Участник"}


@dataclass
class RosterRow:
    ally_code: str
    ingame_name: str
    registered: bool
    rank_label: str = "—"
    swgoh_gg_url: str = ""


def get_roster(guild_id: int) -> list[RosterRow]:
    mappings = database.get_all_user_mappings_with_rank(guild_id)  # (ally_code, ingame_name, member_level)
    registered_codes = {ally_code for _, ally_code, _, _ in database.get_all_registrations(guild_id)}
    rows = [
        RosterRow(
            ally_code=ally_code,
            ingame_name=ingame_name or "?",
            registered=ally_code in registered_codes,
            rank_label=RANK_LABELS.get(member_level, "—"),
            swgoh_gg_url=f"https://swgoh.gg/p/{ally_code}/",
        )
        for ally_code, ingame_name, member_level in mappings
    ]
    # ГМ/офицеры выше по списку, дальше по имени — раз это теперь виджет на главной,
    # а не отдельная страница с сортировкой по клику, порядок по умолчанию важнее.
    rows.sort(key=lambda r: (-{"ГМ": 2, "Офицер": 1}.get(r.rank_label, 0), r.ingame_name.lower()))
    return rows


@dataclass
class TbSummaryRow:
    member_id: str
    name: str
    summary: int
    unit_donated: int
    covert_attempt: int
    strike_encounter: int
    strike_attempt: int


@dataclass
class TbHistoryRow:
    member_id: str
    name: str
    per_event: dict  # {event_id: TbSummaryRow}
    regressed: bool
    sparkline_points: str = ""  # "x,y x,y ..." для <polyline>, viewBox "0 0 100 30"
    sparkline_last_xy: tuple | None = None  # (x, y) последней точки — для точки-маркера
    sparkline_hits: list = field(default_factory=list)  # [(x, y, completed_at, value), ...] — hover-точки со значениями
    last_value: int | None = None


@dataclass
class TbReport:
    events: list  # [(event_id, completed_at), ...] старые -> новые
    latest: list[TbSummaryRow]  # последняя ТБ, отсортирована по очкам убыв.
    history: list[TbHistoryRow]  # матрица по всем событиям, тоже по убыв. последних очков
    event_totals: list  # [(completed_at, total_summary), ...] — по гильдии, для тренд-графика
    trend_points: str = ""  # "x,y x,y ..." для <polyline>, viewBox "0 0 100 40"
    trend_area_points: str = ""  # то же + замыкание по низу, для залитой площади под линией
    trend_coords: list = field(default_factory=list)  # [(x, y, completed_at, value), ...] — для точек-маркеров
    # ---- Блок ТБ на дашборде (widget: плотные узкие столбцы очков, каждый со своей
    # подписью значения + дельтой к предыдущей ТБ, и числом звёзд под баром — см.
    # web/templates/dashboard.html). Один список на всю колонку (а не 2-3 параллельных,
    # как было раньше) — так гарантированно нет рассинхрона по ширине между строками.
    # chart_bars используется только тут, trend_points/trend_coords выше не трогаем —
    # их использует /tb (tb_report.html). ----
    chart_bars: list = field(default_factory=list)  # [(completed_at, value, height_pct, compact_label, full_label, delta_label, delta_class, stars_or_None), ...]
    has_stars: bool = False  # есть ли хоть одно известное значение totalStars в этой истории


def _svg_coords(values: list, width: int = 100, height: int = 30, pad: int = 3) -> list:
    """Нормализует values в координаты (x, y) для SVG, viewBox "0 0 width height".
    min→низ, max→верх (SVG y растёт вниз, поэтому инвертируем)."""
    if not values:
        return []
    if len(values) == 1:
        return [(width / 2, height / 2)]
    vmin, vmax = min(values), max(values)
    span = (vmax - vmin) or 1
    coords = []
    for i, v in enumerate(values):
        x = (i / (len(values) - 1)) * (width - 2 * pad) + pad
        y = height - pad - ((v - vmin) / span) * (height - 2 * pad)
        coords.append((round(x, 1), round(y, 1)))
    return coords


def _points_str(coords: list) -> str:
    return " ".join(f"{x},{y}" for x, y in coords)


def _compact_number(value: float) -> str:
    """5002331105 -> "5.0B", 1234567 -> "1.2M" — гильдийские суммы очков ТБ легко уходят
    в миллиарды (десятки игроков × очки за весь ивент), полное число прямой подписью
    над столбцом было бы нечитаемо; точное значение остаётся в тултипе (data-tip)."""
    n = abs(value)
    sign = "-" if value < 0 else ""
    for threshold, suffix in ((1_000_000_000, "B"), (1_000_000, "M"), (1_000, "K")):
        if n >= threshold:
            scaled = n / threshold
            text = f"{scaled:.1f}".rstrip("0").rstrip(".")
            return f"{sign}{text}{suffix}"
    return f"{sign}{int(n)}"


def get_tb_report(guild_id: int) -> TbReport | None:
    events = database.get_recent_tb_events(guild_id=guild_id)  # старые -> новые
    if not events:
        return None

    event_ids = [e[0] for e in events]
    summary_rows = database.get_tb_player_summary_for_events(event_ids)

    by_member = {}
    totals_by_event = {eid: 0 for eid in event_ids}
    for event_id, member_id, name, summary, unit_donated, covert_attempt, strike_encounter, strike_attempt in summary_rows:
        row = TbSummaryRow(member_id, name, summary, unit_donated, covert_attempt, strike_encounter, strike_attempt)
        by_member.setdefault(member_id, {})[event_id] = row
        totals_by_event[event_id] = totals_by_event.get(event_id, 0) + summary

    # compute_tb_regressions ждёт голые dict'ы метрик, а не TbSummaryRow —
    # нормализуем только для этого вызова, остальной код ниже работает с dataclass'ами.
    by_member_metrics = {
        member_id: {eid: {"summary": row.summary} for eid, row in rows.items()}
        for member_id, rows in by_member.items()
    }
    regressed = compute_tb_regressions(event_ids, by_member_metrics)

    latest_event_id = event_ids[-1]
    latest = sorted(
        (rows[latest_event_id] for rows in by_member.values() if latest_event_id in rows),
        key=lambda r: r.summary,
        reverse=True,
    )

    def latest_summary(member_id_rows):
        _, rows = member_id_rows
        return rows.get(latest_event_id).summary if latest_event_id in rows else -1

    history = []
    for member_id, rows in sorted(by_member.items(), key=latest_summary, reverse=True):
        name = rows[latest_event_id].name if latest_event_id in rows else next(iter(rows.values())).name
        ordered_event_ids = [eid for eid in event_ids if eid in rows]
        ordered_values = [rows[eid].summary for eid in ordered_event_ids]
        completed_at_by_id = dict(events)
        spark_coords = _svg_coords(ordered_values, width=68, height=22, pad=3)
        sparkline_hits = [
            (x, y, completed_at_by_id[eid], v)
            for (x, y), eid, v in zip(spark_coords, ordered_event_ids, ordered_values)
        ]
        history.append(TbHistoryRow(
            member_id=member_id, name=name, per_event=rows, regressed=member_id in regressed,
            sparkline_points=_points_str(spark_coords),
            sparkline_last_xy=spark_coords[-1] if spark_coords else None,
            sparkline_hits=sparkline_hits,
            last_value=ordered_values[-1] if ordered_values else None,
        ))

    event_totals = [(completed_at, totals_by_event[eid]) for eid, completed_at in events]
    trend_w, trend_h, trend_pad = 100, 40, 4
    trend_coords = _svg_coords([t for _, t in event_totals], width=trend_w, height=trend_h, pad=trend_pad)
    trend_points = _points_str(trend_coords)
    trend_area_points = ""
    if trend_coords:
        baseline = trend_h - 1
        trend_area_points = f"{trend_coords[0][0]},{baseline} " + trend_points + f" {trend_coords[-1][0]},{baseline}"
    trend_coords_full = [
        (x, y, event_totals[i][0], event_totals[i][1]) for i, (x, y) in enumerate(trend_coords)
    ]

    # ---- chart_bars: столбцы очков для дашборда (высота в % от максимума этой истории —
    # столбчатый график всегда растёт от нулевой базовой линии, в отличие от trend_coords
    # выше, который намеренно масштабирует от min до max для наглядности колебаний линии).
    # compact_label — прямая подпись над столбцом (см. dataviz: "Bars -> value at the tip"),
    # чтобы сумму очков было видно сразу, без наведения; точное число остаётся в data-tip.
    # delta_label/delta_class — % к ПРЕДЫДУЩЕЙ ТБ (иначе округлённые до "5B" соседние
    # столбцы визуально неотличимы, а пользователю как раз важно быстро видеть рост/спад).
    # stars — из tb_events.stars, для инлайн-подписи со звёздочкой под баром. ----
    star_by_event = database.get_tb_event_stars(event_ids)
    max_points = max((t for _, t in event_totals), default=0) or 1
    chart_bars = []
    prev_total = None
    for eid, completed_at in events:
        total = totals_by_event[eid]
        delta_label, delta_class = None, None
        if prev_total:
            delta_pct = (total - prev_total) / prev_total * 100
            if abs(delta_pct) < 0.05:
                delta_label, delta_class = "±0%", "flat"
            else:
                arrow = "▲" if delta_pct > 0 else "▼"
                delta_label = f"{arrow}{abs(delta_pct):.1f}%"
                delta_class = "up" if delta_pct > 0 else "down"
        chart_bars.append((
            completed_at, total, round(total / max_points * 100, 1), _compact_number(total),
            f"{total:,}".replace(",", " "), delta_label, delta_class, star_by_event.get(eid),
        ))
        prev_total = total

    return TbReport(
        events=events, latest=latest, history=history, event_totals=event_totals,
        trend_points=trend_points, trend_area_points=trend_area_points, trend_coords=trend_coords_full,
        chart_bars=chart_bars, has_stars=bool(star_by_event),
    )


# Порядок вывода действий — совпадает с cogs/guild_events.py::_format_tb_player_report
# и _format_tb_player_compare_report, чтобы веб-версия показывала те же строки в том же
# порядке, что и Discord-команды "тб_отчет игрок"/"сравнение_по_игроку".
TB_GLOBAL_ACTION_ORDER = ("summary", "unit_donated", "strike_encounter", "strike_attempt", "covert_attempt", "disobey")
TB_PHASE_ACTION_ORDER = ("summary", "power", "strike_attempt", "strike_encounter", "covert_attempt", "unit_donated", "disobey")


def _tb_conflict_sort_key(conflict_key: str):
    base = conflict_key.split("_")[0]
    return (base, 1 if "_bonus" in conflict_key else 0)


def _tb_planet_label(phase: str, conflict_key: str, planet_map: dict) -> str:
    base_conflict = conflict_key.split("_")[0]
    is_bonus = "_bonus" in conflict_key
    fallback = TB_CONFLICT_LABELS.get(base_conflict, f"Conflict {base_conflict}")
    name = planet_map.get((phase, "bonus" if is_bonus else base_conflict))
    if name:
        return f"{name} — {fallback}" + (" (бонус)" if is_bonus else "")
    return fallback + (" (бонус)" if is_bonus else "")


def _tb_planet_label_compare(phase: str, conflict_key: str, planet_maps: list) -> str:
    base_conflict = conflict_key.split("_")[0]
    is_bonus = "_bonus" in conflict_key
    fallback = TB_CONFLICT_LABELS.get(base_conflict, f"Conflict {base_conflict}")
    lookup_key = "bonus" if is_bonus else base_conflict
    names = [pm.get((phase, lookup_key)) if pm else None for pm in planet_maps]
    if any(names):
        shown = " | ".join(n if n else "—" for n in names)
        return f"{shown} ({fallback})" + (", бонус" if is_bonus else "")
    return fallback + (" (бонус)" if is_bonus else "")


def _resolve_tb_member_id(events: list, name: str) -> str | None:
    """Ищет member_id по отображаемому имени игрока среди сохранённых событий ТБ,
    начиная с самого свежего — member_id (comlink playerId) стабилен между ТБ, даже
    если игровой ник менялся, поэтому одного совпадения имени в любом сохранённом
    событии достаточно, чтобы дальше опознавать игрока во всех остальных событиях."""
    if not events:
        return None
    event_ids = [e[0] for e in events]
    rows = database.get_tb_player_summary_for_events(event_ids)
    name_lower = name.strip().lower()
    by_event = {}
    for event_id, member_id, player_name, *_rest in rows:
        if player_name.lower() == name_lower:
            by_event[event_id] = member_id
    for event_id, _ in reversed(events):
        if event_id in by_event:
            return by_event[event_id]
    return None


@dataclass
class TbPlayerPlanetDetail:
    label: str
    actions: list  # [(action_label, value), ...]


@dataclass
class TbPlayerPhaseDetail:
    phase: str
    round_rows: list  # [(action_label, value), ...]
    planets: list  # [TbPlayerPlanetDetail, ...]


@dataclass
class TbPlayerReport:
    player_name: str
    completed_at: str
    global_rows: list  # [(action_label, value), ...]
    phases: list  # [TbPlayerPhaseDetail, ...]
    hidden_entries: list  # [(raw_key, value), ...] — недостоверные данные comlink, см. TB_HIDDEN_ZONE_ACTIONS


def get_tb_player_report(guild_id: int, name: str) -> TbPlayerReport | None:
    """Порт cogs/guild_events.py::_format_tb_player_report — читает уже раскодированные
    и сохранённые данные последней ТБ (не дёргает Comlink живьём, см. решения плана)."""
    events = database.get_recent_tb_events(guild_id=guild_id)
    if not events:
        return None
    member_id = _resolve_tb_member_id(events, name)
    if member_id is None:
        return None

    event_id, completed_at = events[-1]
    detail = database.get_tb_player_detail(event_id, member_id)
    if not detail:
        return None
    zone_data = json.loads(detail[0])
    global_totals = json.loads(detail[1])
    round_totals = json.loads(detail[2])
    raw_keys = json.loads(detail[3])
    planet_map = database.get_tb_event_planet_names(event_id)

    summary_rows = database.get_tb_player_summary_for_events([event_id])
    display_name = next((r[2] for r in summary_rows if r[1] == member_id), name)

    global_rows = [(TB_ACTION_LABELS.get(a, a), global_totals[a]) for a in TB_GLOBAL_ACTION_ORDER if a in global_totals]

    all_phases = sorted(set(round_totals.keys()) | set(zone_data.keys()), key=int)
    phases = []
    hidden_entries = []
    for phase in all_phases:
        rt = round_totals.get(phase, {})
        round_rows = [(TB_ACTION_LABELS.get(a, a), rt[a]) for a in TB_PHASE_ACTION_ORDER if a in rt]

        planets = []
        conflicts = zone_data.get(phase, {})
        for conflict_key in sorted(conflicts.keys(), key=_tb_conflict_sort_key):
            actions = conflicts[conflict_key]
            hidden_actions = TB_HIDDEN_ZONE_ACTIONS.get((phase, conflict_key), set())
            action_rows = []
            for action in sorted(actions.keys()):
                if action in hidden_actions:
                    entries = raw_keys.get(phase, {}).get(conflict_key, {}).get(action, {})
                    for entry_key, value in actions[action].items():
                        raw_key = entries.get(entry_key, f"{action}_phase{phase}_conflict{conflict_key}_{entry_key}")
                        hidden_entries.append((raw_key, value))
                    continue
                total = sum(actions[action].values())
                action_rows.append((TB_ACTION_LABELS.get(action, action), total))
            planets.append(TbPlayerPlanetDetail(label=_tb_planet_label(phase, conflict_key, planet_map), actions=action_rows))

        phases.append(TbPlayerPhaseDetail(phase=phase, round_rows=round_rows, planets=planets))

    return TbPlayerReport(
        player_name=display_name, completed_at=completed_at,
        global_rows=global_rows, phases=phases, hidden_entries=hidden_entries,
    )


@dataclass
class TbCompareRow:
    label: str
    values: list  # по одному значению на событие (None, если данных нет)


@dataclass
class TbComparePlanet:
    label: str
    rows: list  # [TbCompareRow, ...]


@dataclass
class TbComparePhase:
    phase: str
    round_rows: list  # [TbCompareRow, ...]
    planets: list  # [TbComparePlanet, ...]


@dataclass
class TbPlayerCompare:
    player_name: str
    event_labels: list  # ["ТБ-3 (2026-08-01)", ...], в порядке events (старые -> новые)
    global_rows: list  # [TbCompareRow, ...]
    phases: list  # [TbComparePhase, ...]
    hidden_entries: list  # [(event_label, raw_key, value), ...]


def get_tb_player_compare(guild_id: int, name: str) -> TbPlayerCompare | None:
    """Порт cogs/guild_events.py::_format_tb_player_compare_report — по всем сохранённым
    событиям ТБ (до TB_HISTORY_KEEP), не только последнему."""
    events = database.get_recent_tb_events(guild_id=guild_id)
    if not events:
        return None
    member_id = _resolve_tb_member_id(events, name)
    if member_id is None:
        return None

    event_ids = [e[0] for e in events]
    summary_rows = database.get_tb_player_summary_for_events(event_ids)
    names_by_event = {r[0]: r[2] for r in summary_rows if r[1] == member_id}
    display_name = names_by_event.get(event_ids[-1]) or next(iter(names_by_event.values()), name)

    n = len(events)
    event_labels = [f"ТБ-{n - i} ({events[i][1][:10]})" for i in range(n)]

    per_event = []
    planet_maps = []
    for event_id, _completed_at in events:
        detail = database.get_tb_player_detail(event_id, member_id)
        planet_maps.append(database.get_tb_event_planet_names(event_id))
        if not detail:
            per_event.append(None)
            continue
        per_event.append({
            "zone_data": json.loads(detail[0]),
            "global_totals": json.loads(detail[1]),
            "round_totals": json.loads(detail[2]),
            "raw_keys": json.loads(detail[3]),
        })

    global_rows = []
    for action in TB_GLOBAL_ACTION_ORDER:
        values = [e["global_totals"].get(action) if e else None for e in per_event]
        if all(v is None for v in values):
            continue
        global_rows.append(TbCompareRow(label=TB_ACTION_LABELS.get(action, action), values=values))

    all_phases = sorted(
        {p for e in per_event if e for p in set(e["round_totals"].keys()) | set(e["zone_data"].keys())},
        key=int,
    )

    hidden_entries = []
    phases = []
    for phase in all_phases:
        round_rows = []
        for action in TB_PHASE_ACTION_ORDER:
            values = [(e["round_totals"].get(phase, {}).get(action) if e else None) for e in per_event]
            if all(v is None for v in values):
                continue
            round_rows.append(TbCompareRow(label=TB_ACTION_LABELS.get(action, action), values=values))

        conflict_keys = set()
        for e in per_event:
            if e:
                conflict_keys |= set(e["zone_data"].get(phase, {}).keys())

        planets = []
        for conflict_key in sorted(conflict_keys, key=_tb_conflict_sort_key):
            action_keys = set()
            for e in per_event:
                if e:
                    action_keys |= set(e["zone_data"].get(phase, {}).get(conflict_key, {}).keys())

            hidden_actions = TB_HIDDEN_ZONE_ACTIONS.get((phase, conflict_key), set())
            rows = []
            for action in sorted(action_keys):
                values = []
                for e in per_event:
                    if not e:
                        values.append(None)
                        continue
                    entries = e["zone_data"].get(phase, {}).get(conflict_key, {}).get(action)
                    values.append(sum(entries.values()) if entries else None)

                if action in hidden_actions:
                    for idx, e in enumerate(per_event):
                        if not e or values[idx] is None:
                            continue
                        raw_entries = e["raw_keys"].get(phase, {}).get(conflict_key, {}).get(action, {})
                        actual_entries = e["zone_data"].get(phase, {}).get(conflict_key, {}).get(action, {})
                        for entry_key, val in actual_entries.items():
                            raw_key = raw_entries.get(entry_key, f"{action}_phase{phase}_conflict{conflict_key}_{entry_key}")
                            hidden_entries.append((event_labels[idx], raw_key, val))
                    continue

                rows.append(TbCompareRow(label=TB_ACTION_LABELS.get(action, action), values=values))

            planets.append(TbComparePlanet(label=_tb_planet_label_compare(phase, conflict_key, planet_maps), rows=rows))

        phases.append(TbComparePhase(phase=phase, round_rows=round_rows, planets=planets))

    return TbPlayerCompare(
        player_name=display_name, event_labels=event_labels,
        global_rows=global_rows, phases=phases, hidden_entries=hidden_entries,
    )


@dataclass
class ViolationRow:
    ally_code: str
    name: str
    counts_recent: dict  # {"ТБ": n, "ВГ": n, "Рейд": n}
    recent_total: int
    lifetime_total: int
    flagged: bool  # recent_total >= N_LIMIT


def get_violations_overview(guild_id: int, include_zero: bool = False) -> list[ViolationRow]:
    roster_names = {ally_code: name for _, ally_code, name in database.get_all_user_mappings(guild_id)}
    all_warns = database.get_all_warns(guild_id)  # [(ally_code, category, date_str), ...] — все, без даты-фильтра

    three_months_ago = datetime.now() - timedelta(days=90)
    per_code = {}
    for ally_code, category, date_str in all_warns:
        entry = per_code.setdefault(ally_code, {"recent": {c: 0 for c in WARN_CATEGORIES}, "lifetime": 0})
        entry["lifetime"] += 1
        try:
            w_date = datetime.strptime(date_str, "%d.%m.%Y")
        except ValueError:
            continue
        if w_date >= three_months_ago and category in entry["recent"]:
            entry["recent"][category] += 1

    rows = []
    for ally_code, entry in per_code.items():
        recent_total = sum(entry["recent"].values())
        if not include_zero and recent_total == 0:
            continue
        rows.append(ViolationRow(
            ally_code=ally_code,
            name=roster_names.get(ally_code, ally_code),
            counts_recent=entry["recent"],
            recent_total=recent_total,
            lifetime_total=entry["lifetime"],
            flagged=recent_total >= N_LIMIT,
        ))

    rows.sort(key=lambda r: r.recent_total, reverse=True)
    return rows


ACTIVITY_ACTION_LABELS = {
    "gear": "Снаряжение",
    "level": "Уровень",
    "era": "Эра",
    "relic": "Реликвия",
    "star": "Звёзды",
    "zeta": "Зета",
    "omicron": "Омикрон",
    "unlock": "Новый герой",
}

# Суффикс CSS-класса .badge-<...> (web/static/style.css) — цветовое кодирование по типу
# изменения, как на карточках активности swgoh.gg (ориентир для этой страницы).
ACTIVITY_ACTION_CLASSES = {
    "gear": "gear",
    "level": "neutral",
    "era": "neutral",
    "relic": "relic",
    "star": "star",
    "zeta": "zeta",
    "omicron": "omicron",
    "unlock": "unlock",
}

PLAYER_STATS_SYNC_HOURS = database.PLAYER_STATS_SYNC_HOURS


@dataclass
class ActivityEventRow:
    ally_code: str
    player_name: str
    base_id: str
    unit_name: str
    action_type: str
    action_label: str
    action_class: str
    old_value: str | None
    new_value: str
    event_date: str
    skill_url: str | None = None


def get_guild_activity(guild_id: int, ally_code: str | None = None, action_type: str | None = None,
                        limit: int = 500, offset: int = 0,
                        date_from: str | None = None, date_to: str | None = None) -> list[ActivityEventRow]:
    names_by_code = {code: name for _, code, name in database.get_all_user_mappings(guild_id)}
    rows = database.get_guild_activity_events(guild_id, ally_code=ally_code, action_type=action_type,
                                                limit=limit, offset=offset,
                                                date_from=date_from, date_to=date_to)
    unit_names = database.get_game_unit_names([r[1] for r in rows])
    # zeta/omicron хранят raw skill_id в new_value (см. services/activity_diff.py) — резолвим
    # в человекочитаемое имя способности + слаг для ссылки на swgoh.gg разом на весь батч.
    skill_ids = {new_value for _, _, action_type, _, new_value, _, _ in rows if action_type in ("zeta", "omicron")}
    skill_info = database.get_skill_display_info(list(skill_ids))
    result = []
    for ac, base_id, action_type, old_value, new_value, event_date, scraped_at in rows:
        skill_url = None
        display_value = new_value
        if action_type in ("zeta", "omicron"):
            name, ability_id, _ability_type, _omicron_mode = skill_info.get(new_value, (new_value, None, None, None))
            display_value = name
            if ability_id:
                skill_url = f"https://swgoh.gg/units/{base_id}/ability/{ability_id}/1/"
        result.append(ActivityEventRow(
            ally_code=ac,
            player_name=names_by_code.get(ac, ac),
            base_id=base_id,
            unit_name=unit_names.get(base_id) or base_id,
            action_type=action_type,
            action_label=ACTIVITY_ACTION_LABELS.get(action_type, action_type),
            action_class=ACTIVITY_ACTION_CLASSES.get(action_type, "neutral"),
            old_value=old_value,
            new_value=display_value,
            event_date=event_date,
            skill_url=skill_url,
        ))
    return result


def get_guild_activity_count(guild_id: int, ally_code: str | None = None, action_type: str | None = None,
                              date_from: str | None = None, date_to: str | None = None) -> int:
    """Общее число событий по текущему фильтру (без учёта limit/offset) — для пагинации."""
    return database.get_guild_activity_events_count(guild_id, ally_code=ally_code, action_type=action_type,
                                                      date_from=date_from, date_to=date_to)


def get_guild_activity_dates(guild_id: int, ally_code: str | None = None, action_type: str | None = None,
                              date_from: str | None = None, date_to: str | None = None) -> list[str]:
    return database.get_guild_activity_distinct_dates(guild_id, ally_code=ally_code, action_type=action_type,
                                                        date_from=date_from, date_to=date_to)


def get_guild_activity_breakdown(guild_id: int, ally_code: str | None = None,
                                  date_from: str | None = None, date_to: str | None = None) -> list:
    """[(action_label, count), ...] отсортировано по убыванию — намеренно игнорирует фильтр
    по типу события (см. get_guild_activity_type_counts), иначе выбор одного типа в фильтре
    ленты схлопывал бы эту панель до единственного столбика."""
    counts = database.get_guild_activity_type_counts(guild_id, ally_code=ally_code,
                                                       date_from=date_from, date_to=date_to)
    labeled = Counter()
    for action_type, count in counts:
        labeled[ACTIVITY_ACTION_LABELS.get(action_type, action_type)] += count
    return sorted(labeled.items(), key=lambda kv: kv[1], reverse=True)


def get_guild_activity_players(guild_id: int) -> list:
    """[(ally_code, name), ...] по ВСЕМ игрокам с активностью в этой гильдии — не зависит
    от текущего фильтра/лимита get_guild_activity, иначе выбор игрока в выпадающем списке
    на веб-странице схлопывал бы сам список до одного уже выбранного игрока."""
    names_by_code = {code: name for _, code, name in database.get_all_user_mappings(guild_id)}
    codes = database.get_guild_activity_player_codes(guild_id)
    return sorted(((c, names_by_code.get(c, c)) for c in codes), key=lambda p: p[1].lower())


def _friendly_date_label(event_date: str, today) -> str:
    if event_date == today.isoformat():
        return "Сегодня"
    try:
        d = datetime.strptime(event_date, "%Y-%m-%d").date()
    except ValueError:
        return event_date
    if d == today - timedelta(days=1):
        return "Вчера"
    return d.strftime("%d.%m.%Y")


def friendly_activity_date_label(event_date: str | None) -> str | None:
    """Публичная обёртка над _friendly_date_label с текущей датой (МСК) — для подписи
    текущей страницы в постраничной навигации /activity (1 страница = 1 день)."""
    if not event_date:
        return None
    return _friendly_date_label(event_date, datetime.now(MSK).date())


def group_activity(rows: list[ActivityEventRow]) -> list[dict]:
    """[{"date_label", "players": [{"ally_code", "player_name", "rows": [...]}]}] — двухуровневая
    группировка (дата -> игрок), как на карточках активности swgoh.gg. rows уже упорядочены
    по убыванию id (свежие сначала, см. database.get_guild_activity_events) — группировка
    сохраняет этот порядок появления, без пересортировки по алфавиту."""
    today = datetime.now(MSK).date()
    groups: dict[str, dict[str, dict]] = {}
    order: list[str] = []
    for row in rows:
        if row.event_date not in groups:
            groups[row.event_date] = {}
            order.append(row.event_date)
        by_player = groups[row.event_date]
        if row.ally_code not in by_player:
            by_player[row.ally_code] = {"ally_code": row.ally_code, "player_name": row.player_name, "rows": []}
        by_player[row.ally_code]["rows"].append(row)

    result = []
    for event_date in order:
        result.append({
            "date_label": _friendly_date_label(event_date, today),
            "players": list(groups[event_date].values()),
        })
    return result


def get_activity_sync_status(guild_id: int) -> dict:
    """Для панели статуса на /activity: когда последний раз обновлялся кэш ростера этой
    гильдии (учитывает и авто-, и ручной синк — оба пишут через upsert_player_units) и
    ориентировочно когда сработает следующий автоматический цикл бота."""
    ally_codes = [code for _, code, _ in database.get_all_user_mappings(guild_id)]
    last_sync_raw = database.get_player_units_last_sync(ally_codes)
    last_sync = None
    if last_sync_raw:
        try:
            # updated_at пишется через SQLite datetime('now') — наивный UTC.
            last_sync = datetime.strptime(last_sync_raw, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc).astimezone(MSK)
        except ValueError:
            pass

    next_auto = None
    last_auto_raw = database.get_bot_state("player_units_sync_loop_last_auto_run")
    if last_auto_raw:
        try:
            next_auto = datetime.fromisoformat(last_auto_raw) + timedelta(hours=PLAYER_STATS_SYNC_HOURS)
        except ValueError:
            pass

    return {"last_sync": last_sync, "next_auto": next_auto, "sync_hours": PLAYER_STATS_SYNC_HOURS}


def get_top_violators(guild_id: int, limit: int = 10) -> list[ViolationRow]:
    """Для виджета дашборда — get_violations_overview уже сортирует по recent_total убыв.
    и по умолчанию не включает игроков без нарушений за 90 дней, просто берём первые N."""
    return get_violations_overview(guild_id, include_zero=False)[:limit]


TW_RESULT_LABELS = {"win": "Победа", "loss": "Поражение", "draw": "Ничья"}
TW_RESULT_CLASSES = {"win": "ok", "loss": "danger", "draw": "neutral"}


@dataclass
class TwEventRow:
    territory_war_id: str
    own_score: int
    opponent_score: int
    own_power: int
    opponent_name: str
    opponent_guild_id: str
    opponent_gp: int
    date_label: str
    result: str
    result_label: str
    result_class: str
    opponent_url: str = ""


def get_recent_tw_results(guild_id: int, limit: int = 10) -> list[TwEventRow]:
    """История ВГ, накопленная ботом из recentTerritoryWarResult (см. cogs/guild_events.py::
    generate_tw_report) — только гильдийский уровень, без пер-игрока разбивки, см. память
    project_territory_war_report_gap за подтверждённым потолком Comlink API."""
    rows = database.get_recent_tw_events(guild_id, limit=limit)
    result = []
    for (territory_war_id, own_score, opponent_score, own_power, opponent_name,
         opponent_guild_id, opponent_gp, start_time, end_time, tw_result) in rows:
        try:
            date_label = datetime.fromtimestamp(end_time or start_time, tz=timezone.utc).astimezone(MSK).strftime("%d.%m.%Y")
        except (ValueError, OSError, OverflowError):
            date_label = "—"
        result.append(TwEventRow(
            territory_war_id=territory_war_id,
            own_score=own_score,
            opponent_score=opponent_score,
            own_power=own_power,
            opponent_name=opponent_name or "—",
            opponent_guild_id=opponent_guild_id,
            opponent_gp=opponent_gp,
            date_label=date_label,
            result=tw_result,
            result_label=TW_RESULT_LABELS.get(tw_result, tw_result or "—"),
            result_class=TW_RESULT_CLASSES.get(tw_result, "neutral"),
            # swgoh.gg принимает сырой comlink guild id прямо в URL, без отдельного
            # маппинга на свой внутренний id (подтверждено примером от пользователя).
            opponent_url=f"https://swgoh.gg/g/{opponent_guild_id}/" if opponent_guild_id else "",
        ))
    return result


def _ru_plural(n: int, one: str, few: str, many: str) -> str:
    n_abs = abs(n) % 100
    if 11 <= n_abs <= 14:
        return many
    n1 = n_abs % 10
    if n1 == 1:
        return one
    if 2 <= n1 <= 4:
        return few
    return many


@dataclass
class TwPeriodStat:
    label: str
    winrate: int | None
    wins: int
    total: int


@dataclass
class TwStats:
    periods: list[TwPeriodStat]
    streak_label: str
    streak_class: str
    avg_margin: int | None


def get_tw_stats(guild_id: int) -> TwStats | None:
    """Общая статистика по ВГ (винрейт за 3/6 месяцев и за всё время, текущая серия,
    средняя разница в очках) — на основе всей истории, накопленной ботом в tw_events.
    "Всё время" — не настоящий алл-тайм, а глубина с момента, когда бот начал писать эту
    таблицу (2026-08-25), т.к. Comlink отдаёт только последние ~8 завершённых ВГ и дальше
    история копится только тиками бота, см. память project_territory_war_report_gap.
    Ничьи не учитываются в винрейте (решающих партий в ВГ практически не бывает), но не
    ломают подсчёт серии — встретив ничью, серия просто обрывается."""
    rows = database.get_recent_tw_events(guild_id, limit=None)
    if not rows:
        return None

    now_ts = datetime.now(timezone.utc).timestamp()
    periods = []
    for label, days in (("3 месяца", 90), ("6 месяцев", 180), ("Всё время", None)):
        cutoff = now_ts - days * 86400 if days else None
        decisive = [r for r in rows if r[9] in ("win", "loss") and (cutoff is None or r[7] >= cutoff)]
        wins = sum(1 for r in decisive if r[9] == "win")
        winrate = round(wins / len(decisive) * 100) if decisive else None
        periods.append(TwPeriodStat(label=label, winrate=winrate, wins=wins, total=len(decisive)))

    streak_result = None
    streak_count = 0
    for r in rows:
        res = r[9]
        if res not in ("win", "loss"):
            break
        if streak_result is None:
            streak_result = res
        elif res != streak_result:
            break
        streak_count += 1

    if streak_result == "win":
        streak_label = f"{streak_count} {_ru_plural(streak_count, 'победа', 'победы', 'побед')} подряд"
        streak_class = "ok"
    elif streak_result == "loss":
        streak_label = f"{streak_count} {_ru_plural(streak_count, 'поражение', 'поражения', 'поражений')} подряд"
        streak_class = "danger"
    else:
        streak_label = "—"
        streak_class = "neutral"

    margins = [r[1] - r[2] for r in rows if r[9] in ("win", "loss")]
    avg_margin = round(sum(margins) / len(margins)) if margins else None

    return TwStats(periods=periods, streak_label=streak_label, streak_class=streak_class, avg_margin=avg_margin)
