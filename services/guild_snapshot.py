"""Ежедневный срез гильдии для виджета «Срез гильдии» на главной (решение пользователя
2026-10-26: «виджеты среза на главную» — и прогресс модинга, и сводка ростера).

Считается только из кэшей ежечасного синка (player_unit_cache, player_profile_cache,
player_datacron_cache), без StatCalc и без запросов в игру — поэтому без скорости от модулей
(её даёт только StatCalc, это ~20 с на гильдию). Бот пишет срез после каждого часового синка
ростеров, но не чаще раза в REFRESH_HOURS часов: строка за сегодня (МСК) перезаписывается, так
что у каждого дня остаётся последний срез. Виджет сравнивает сегодняшний срез со срезом
недельной давности."""

import json
import sqlite3
from datetime import date, datetime, timedelta, timezone

import database
from services import player_profile as pp

MSK = timezone(timedelta(hours=3))
COMPARE_DAYS = 7
KEEP_DAYS = 400
REFRESH_HOURS = 6

# (ключ, подпись, формат) — порядок вывода в виджете.
METRICS = [
    ("gp", "ГМ гильдии", "compact"),
    ("members", "Игроков", "int"),
    ("g13", "Персонажей G13", "int"),
    ("relic_levels", "Уровней реликвий", "int"),
    ("r9_plus", "R9 и выше", "int"),
    ("gls", "ГЛ", "int"),
    ("zetas", "Зет", "int"),
    ("omicrons", "Омикронов", "int"),
    ("six_dot", "Модулей 6 точек", "int"),
    ("speed20", "Скорость 20+ в допах", "int"),
    ("modku", "МодКу (среднее)", "float2"),
    ("dc_l9", "Датакронов 9 ур.", "int"),
]


def _ensure_table(cursor):
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS guild_daily_snapshots (
            guild_id INTEGER NOT NULL,
            day TEXT NOT NULL,
            metrics_json TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (guild_id, day)
        )
    """)


def compute(guild_id: int) -> dict | None:
    """Метрики текущего состава по кэшам. None, если кэша ростеров ещё нет."""
    from services import mod_ku
    codes = [code for code, _name in database.get_roster_by_player_id(guild_id).values() if code]
    units = database.get_player_units_bulk(codes)
    if not units:
        return None
    profiles = database.get_player_profiles_bulk(codes)
    datacrons = database.get_player_datacrons_bulk(codes)
    skills = database.get_all_skill_tier_info()
    gl_ids = set(pp.galactic_legends())
    m = {key: 0 for key, _label, _fmt in METRICS}
    modku = []
    for code in codes:
        roster = units.get(code)
        if not roster:
            continue
        m["members"] += 1
        m["gp"] += ((profiles.get(code) or {}).get("stats") or {}).get("STAT_GALACTIC_POWER_ACQUIRED_NAME") or 0
        for base_id, unit in roster.items():
            gear = unit.get("currentTier") or 0
            relic_tier = (unit.get("relic") or {}).get("currentTier") or 0
            if gear >= 13:
                m["g13"] += 1
                if relic_tier >= 3:
                    m["relic_levels"] += relic_tier - 2
                    m["r9_plus"] += relic_tier - 2 >= 9
            if base_id in gl_ids:
                m["gls"] += 1
            zetas, omicrons = pp._skill_counts(unit, skills)
            m["zetas"] += zetas
            m["omicrons"] += len(omicrons)
        mods = pp._mod_summary(list(roster.values()))
        m["six_dot"] += mods["six_dot"]
        m["speed20"] += mods["s20"]
        m["dc_l9"] += pp._datacron_summary(datacrons.get(code) or [])["l9"]
        result = mod_ku.compute(roster)
        if result:
            modku.append(result["modku"])
    m["modku"] = round(sum(modku) / len(modku), 2) if modku else None
    return m


def save(guild_id: int, metrics: dict) -> None:
    today = datetime.now(MSK).date().isoformat()
    conn = sqlite3.connect(database.DB_NAME)
    cursor = conn.cursor()
    _ensure_table(cursor)
    cursor.execute(
        "INSERT OR REPLACE INTO guild_daily_snapshots (guild_id, day, metrics_json, updated_at) VALUES (?, ?, ?, ?)",
        (guild_id, today, json.dumps(metrics), datetime.now(timezone.utc).isoformat()),
    )
    cutoff = (date.fromisoformat(today) - timedelta(days=KEEP_DAYS)).isoformat()
    cursor.execute("DELETE FROM guild_daily_snapshots WHERE guild_id = ? AND day < ?", (guild_id, cutoff))
    conn.commit()
    conn.close()


def _fresh(guild_id: int) -> bool:
    """Сегодняшний срез уже есть и моложе REFRESH_HOURS."""
    today = datetime.now(MSK).date().isoformat()
    conn = sqlite3.connect(database.DB_NAME)
    cursor = conn.cursor()
    _ensure_table(cursor)
    conn.commit()
    cursor.execute("SELECT updated_at FROM guild_daily_snapshots WHERE guild_id = ? AND day = ?", (guild_id, today))
    row = cursor.fetchone()
    conn.close()
    if not row:
        return False
    return datetime.now(timezone.utc) - datetime.fromisoformat(row[0]) < timedelta(hours=REFRESH_HOURS)


def take(guild_id: int, force: bool = False) -> bool:
    """Посчитать и сохранить срез за сегодня. Блокирующая — из бота звать через to_thread.
    Чтение кэша ростеров гильдии — ~10–15 с чистого Python (JSON ростеров), а он держит GIL и
    тормозит event loop бота, поэтому не чаще раза в REFRESH_HOURS часов."""
    if not force and _fresh(guild_id):
        return False
    metrics = compute(guild_id)
    if metrics is None:
        return False
    save(guild_id, metrics)
    return True


def _fmt(value, fmt: str, signed: bool = False) -> str:
    if value is None:
        return "—"
    sign = "+" if signed and value > 0 else ("−" if signed and value < 0 else "")
    n = abs(value) if signed else value
    if fmt == "compact":
        from services.dashboard_data import _compact_number
        return sign + _compact_number(n)
    if fmt == "float2":
        return sign + f"{n:.2f}".replace(".", ",")
    return sign + f"{int(n):,}".replace(",", " ")


def _day_label(day: str) -> str:
    d = date.fromisoformat(day)
    return f"{d.day:02d}.{d.month:02d}"


def widget(guild_id: int) -> dict | None:
    """{day, base_day, rows: [{label, value, delta, fmt}]} — сегодняшний (последний) срез против
    среза COMPARE_DAYS дней назад (или самого раннего, если истории меньше недели)."""
    conn = sqlite3.connect(database.DB_NAME)
    cursor = conn.cursor()
    _ensure_table(cursor)
    conn.commit()
    cursor.execute("SELECT day, metrics_json FROM guild_daily_snapshots WHERE guild_id = ? ORDER BY day DESC LIMIT 1",
                   (guild_id,))
    latest = cursor.fetchone()
    if not latest:
        conn.close()
        return None
    target = (date.fromisoformat(latest[0]) - timedelta(days=COMPARE_DAYS)).isoformat()
    cursor.execute(
        "SELECT day, metrics_json FROM guild_daily_snapshots WHERE guild_id = ? AND day <= ? ORDER BY day DESC LIMIT 1",
        (guild_id, target),
    )
    base = cursor.fetchone()
    if not base:
        cursor.execute(
            "SELECT day, metrics_json FROM guild_daily_snapshots WHERE guild_id = ? AND day < ? ORDER BY day LIMIT 1",
            (guild_id, latest[0]),
        )
        base = cursor.fetchone()
    conn.close()
    cur = json.loads(latest[1])
    old = json.loads(base[1]) if base else {}
    rows = []
    for key, label, fmt in METRICS:
        value = cur.get(key)
        prev = old.get(key)
        delta = None
        if value is not None and prev is not None:
            delta = round(value - prev, 2) if fmt == "float2" else value - prev
        rows.append({"key": key, "label": label, "text": _fmt(value, fmt),
                     "delta": delta, "delta_text": _fmt(delta, fmt, signed=True) if delta else None,
                     "delta_class": "up" if delta and delta > 0 else "down"})
    return {
        "day": _day_label(latest[0]),
        "base_day": _day_label(base[0]) if base else None,
        "full_week": bool(base) and base[0] <= target,
        "rows": rows,
    }
