"""Read-only агрегации для веб-дашборда (состав/ТБ/нарушения) — только запросы
к database.py и подсчёты, без HTML (шаблоны — в web/routes/guild_dashboard.py).
Логика подсчётов (регрессия по ТБ, "последние 90 дней", порог 🚨) намеренно
скопирована 1:1 из cogs/guild_events.py и cogs/violations.py, чтобы дашборд
показывал те же цифры, что и команды бота в Discord."""

from dataclasses import dataclass, field
from datetime import datetime, timedelta

import database

# Дублирует main.py::N_LIMIT (порог 🚨 для нарушений) — тот же паттерн
# дублирования операторских констант, что и COMLINK_URL/officer-роль (см. CLAUDE.md).
N_LIMIT = 3

WARN_CATEGORIES = ("ТБ", "ВГ", "Рейд")

# см. cogs/guild_events.py::_compute_tb_regressions
TB_REGRESSION_METRICS = ("summary", "covert_attempt", "strike_encounter")
TB_REGRESSION_BASELINE_SIZE = 3


@dataclass
class RosterRow:
    ally_code: str
    ingame_name: str
    registered: bool


def get_roster(guild_id: int) -> list[RosterRow]:
    mappings = database.get_all_user_mappings(guild_id)  # (discord_id_placeholder, ally_code, ingame_name)
    registered_codes = {ally_code for _, ally_code, _, _ in database.get_all_registrations(guild_id)}
    rows = [
        RosterRow(ally_code=ally_code, ingame_name=ingame_name or "?", registered=ally_code in registered_codes)
        for _, ally_code, ingame_name in mappings
    ]
    rows.sort(key=lambda r: r.ingame_name.lower())
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


@dataclass
class TbReport:
    events: list  # [(event_id, completed_at), ...] старые -> новые
    latest: list[TbSummaryRow]  # последняя ТБ, отсортирована по очкам убыв.
    history: list[TbHistoryRow]  # матрица по всем событиям, тоже по убыв. последних очков


def _compute_tb_regressions(event_ids: list[int], by_member: dict) -> set:
    if len(event_ids) < 2:
        return set()
    latest_id = event_ids[-1]
    baseline_ids = event_ids[max(0, len(event_ids) - 1 - TB_REGRESSION_BASELINE_SIZE):-1]

    regressed = set()
    for member_id, entry in by_member.items():
        latest_data = entry.get(latest_id)
        if not latest_data:
            continue
        baseline_values = [entry[eid] for eid in baseline_ids if eid in entry]
        if len(baseline_values) < 2:
            continue
        for metric in TB_REGRESSION_METRICS:
            baseline_avg = sum(getattr(v, metric) for v in baseline_values) / len(baseline_values)
            if baseline_avg > 0 and getattr(latest_data, metric) < baseline_avg:
                regressed.add(member_id)
                break
    return regressed


def get_tb_report(guild_id: int) -> TbReport | None:
    events = database.get_recent_tb_events(guild_id=guild_id)  # старые -> новые
    if not events:
        return None

    event_ids = [e[0] for e in events]
    summary_rows = database.get_tb_player_summary_for_events(event_ids)

    by_member = {}
    for event_id, member_id, name, summary, unit_donated, covert_attempt, strike_encounter, strike_attempt in summary_rows:
        row = TbSummaryRow(member_id, name, summary, unit_donated, covert_attempt, strike_encounter, strike_attempt)
        by_member.setdefault(member_id, {})[event_id] = row

    regressed = _compute_tb_regressions(event_ids, by_member)

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
        history.append(TbHistoryRow(member_id=member_id, name=name, per_event=rows, regressed=member_id in regressed))

    return TbReport(events=events, latest=latest, history=history)


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
