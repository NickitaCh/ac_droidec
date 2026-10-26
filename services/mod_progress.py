"""«Прогресс модинга» (web /mod-progress, только офицеры) — трекер по плейту: снимки того,
как каждый игрок выполняет требования плейта, по датам (заказ Ricardo, тред 2026-10-23;
раньше Дима делал это руками в таблице).

Трекер = гильдия + плейт + период автоснимков (0 — только вручную). Требования плейта
хранятся версиями (mod_progress_versions, JSON): при создании — версия 1, дальше при каждом
изменении плейта новая версия с датой («ТП изменён …»). Старые снимки остаются со своей
версией, ничего не пересчитывается. Ключ строки — cogs.stat_requirements.progress_row_key
(без порога), так что изменённая норма — та же строка с новой нормой.

Снимок считает тот же _evaluate_character_player, что /статы и /stats-check, по кэшу
player_unit_cache — без запросов к Comlink, сразу во всех трёх сценариях /статы (SCENARIOS);
сценарий выбирают при просмотре (просьба NicolozZ 2026-10-23: «как в ХБ или с перекрутом
вверх»). «Как есть» лежит в mod_progress_values, остальные — в mod_progress_scen_values;
снимки, снятые до этого, есть только «как есть» (scenarios IS NULL).
Отряды плейтов (stat_plate_squads): снимок считает всех игроков, а каких отрядов у игрока нет,
пишет в mod_progress_players.squad_missing; переключатель «С отрядом / Все» (как с_отрядом в
/статы, просьба NicolozZ 2026-10-24) отбрасывает их строки при показе. В снимках до этого
(all_players IS NULL) игроки без отряда не посчитаны — там всегда «с отрядом».
Автоснимки и поиск изменений плейта — cogs/mod_progress.py (раз в час)."""

import asyncio
import hashlib
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import database
from cogs.stat_requirements import (
    SCENARIO_FULL, SCENARIO_RAW, SCENARIO_UP, _fmt_compact, _evaluate_character_player, _missing_plate_squads,
    _unit_display_name, progress_row_key, progress_row_text,
)

MSK = ZoneInfo("Europe/Moscow")
PERIODS = {0: "только вручную", 1: "каждый день", 3: "раз в 3 дня", 7: "раз в неделю", 14: "раз в 2 недели"}
_TIME_FMT = "%Y-%m-%d %H:%M:%S"
# Сценарии — те же, что в /статы (SCENARIO_CHOICES); короткие подписи для переключателя.
SCENARIOS = {SCENARIO_RAW: "Как есть (как в ХБ)", SCENARIO_UP: "Релик вверх", SCENARIO_FULL: "Полный подгон релика"}

# Снимки одного трекера не должны идти параллельно (кнопка + автоснимок).
_locks: dict[int, asyncio.Lock] = {}


def _ensure_tables(cursor):
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS mod_progress_trackers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id INTEGER NOT NULL,
            plate_name TEXT NOT NULL,
            period_days INTEGER NOT NULL DEFAULT 0,
            created_by TEXT,
            created_at TEXT NOT NULL,
            last_snapshot_at TEXT
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS mod_progress_versions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tracker_id INTEGER NOT NULL,
            version_num INTEGER NOT NULL,
            req_hash TEXT NOT NULL,
            reqs_json TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS mod_progress_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tracker_id INTEGER NOT NULL,
            version_id INTEGER NOT NULL,
            taken_at TEXT NOT NULL,
            kind TEXT NOT NULL,
            taken_by TEXT
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS mod_progress_players (
            snapshot_id INTEGER NOT NULL,
            ally_code TEXT NOT NULL,
            name TEXT,
            skip_reason TEXT,
            PRIMARY KEY (snapshot_id, ally_code)
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS mod_progress_values (
            snapshot_id INTEGER NOT NULL,
            ally_code TEXT NOT NULL,
            req_key TEXT NOT NULL,
            value TEXT,
            margin REAL,
            ok INTEGER,
            PRIMARY KEY (snapshot_id, ally_code, req_key)
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS mod_progress_scen_values (
            snapshot_id INTEGER NOT NULL,
            scenario TEXT NOT NULL,
            ally_code TEXT NOT NULL,
            req_key TEXT NOT NULL,
            value TEXT,
            margin REAL,
            ok INTEGER,
            PRIMARY KEY (snapshot_id, scenario, ally_code, req_key)
        )
    """)
    for ddl in ("ALTER TABLE mod_progress_snapshots ADD COLUMN scenarios TEXT",
                "ALTER TABLE mod_progress_snapshots ADD COLUMN all_players INTEGER",
                "ALTER TABLE mod_progress_players ADD COLUMN squad_missing TEXT"):
        try:
            cursor.execute(ddl)
        except sqlite3.OperationalError:
            pass


def _connect():
    conn = sqlite3.connect(database.DB_NAME)
    _ensure_tables(conn.cursor())
    conn.commit()
    return conn


def _now() -> str:
    return datetime.now(timezone.utc).strftime(_TIME_FMT)


def _parse(ts: str) -> datetime:
    return datetime.strptime(ts, _TIME_FMT).replace(tzinfo=timezone.utc)


def msk_date(ts: str | None, with_time: bool = False) -> str:
    if not ts:
        return ""
    return _parse(ts).astimezone(MSK).strftime("%d.%m.%Y %H:%M" if with_time else "%d.%m.%Y")


def msk_short(ts: str) -> str:
    return _parse(ts).astimezone(MSK).strftime("%d.%m")


# ---------------- требования плейта и версии ----------------

def plate_requirements(plate_name: str, guild_id: int) -> list[dict]:
    """Текущие требования плейта в порядке /статы: [{key, base_id, char, label, req, priority}]."""
    reqs = []
    seen = set()
    for base_id in database.get_stat_requirement_characters(plate_name, guild_id=guild_id):
        char_name = _unit_display_name(base_id)
        for row in database.get_stat_requirements(plate_name, base_id, guild_id=guild_id):
            key = progress_row_key(row)
            if key in seen:
                continue
            seen.add(key)
            label, req = progress_row_text(row)
            reqs.append({"key": key, "base_id": base_id, "char": char_name, "label": label,
                         "req": req, "priority": row[6], "source": row[1]})
    return reqs


def _hash(reqs: list[dict]) -> str:
    payload = json.dumps([(r["key"], r["req"], r["priority"]) for r in reqs], ensure_ascii=False)
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()


def _latest_version(cursor, tracker_id: int):
    cursor.execute("""
        SELECT id, version_num, req_hash, reqs_json, created_at FROM mod_progress_versions
        WHERE tracker_id = ? ORDER BY version_num DESC LIMIT 1
    """, (tracker_id,))
    return cursor.fetchone()


def sync_version(tracker_id: int) -> dict | None:
    """Сверяет требования плейта с последней версией трекера; если плейт изменился — пишет
    новую версию с текущей датой. Удалённый/опустевший плейт версию не меняет.
    Возвращает последнюю версию {id, num, reqs, created_at} или None, если трекера нет."""
    conn = _connect()
    cursor = conn.cursor()
    cursor.execute("SELECT guild_id, plate_name FROM mod_progress_trackers WHERE id = ?", (tracker_id,))
    row = cursor.fetchone()
    if not row:
        conn.close()
        return None
    guild_id, plate_name = row
    latest = _latest_version(cursor, tracker_id)
    reqs = plate_requirements(plate_name, guild_id)
    if reqs:
        req_hash = _hash(reqs)
        if latest is None or latest[2] != req_hash:
            num = (latest[1] if latest else 0) + 1
            cursor.execute("""
                INSERT INTO mod_progress_versions (tracker_id, version_num, req_hash, reqs_json, created_at)
                VALUES (?, ?, ?, ?, ?)
            """, (tracker_id, num, req_hash, json.dumps(reqs, ensure_ascii=False), _now()))
            conn.commit()
            latest = _latest_version(cursor, tracker_id)
    conn.close()
    if latest is None:
        return None
    return {"id": latest[0], "num": latest[1], "reqs": json.loads(latest[3]), "created_at": latest[4]}


# ---------------- трекеры ----------------

def create_tracker(guild_id: int, plate_name: str, period_days: int, created_by: str) -> int | None:
    """None — у плейта нет требований (следить не за чем)."""
    if not plate_requirements(plate_name, guild_id):
        return None
    conn = _connect()
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO mod_progress_trackers (guild_id, plate_name, period_days, created_by, created_at)
        VALUES (?, ?, ?, ?, ?)
    """, (guild_id, plate_name, period_days if period_days in PERIODS else 0, created_by, _now()))
    tracker_id = cursor.lastrowid
    conn.commit()
    conn.close()
    sync_version(tracker_id)
    return tracker_id


def _tracker_dict(row) -> dict:
    tid, guild_id, plate_name, period_days, created_by, created_at, last_snapshot_at = row
    return {"id": tid, "guild_id": guild_id, "plate_name": plate_name, "period_days": period_days,
            "period_label": PERIODS.get(period_days, f"раз в {period_days} дн."), "created_by": created_by,
            "created_at": created_at, "created_msk": msk_date(created_at),
            "last_snapshot_at": last_snapshot_at, "last_snapshot_msk": msk_date(last_snapshot_at, True)}


def get_tracker(guild_id: int, tracker_id: int) -> dict | None:
    conn = _connect()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM mod_progress_trackers WHERE id = ? AND guild_id = ?", (tracker_id, guild_id))
    row = cursor.fetchone()
    conn.close()
    return _tracker_dict(row) if row else None


def list_trackers(guild_id: int) -> list[dict]:
    conn = _connect()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM mod_progress_trackers WHERE guild_id = ? ORDER BY plate_name, id", (guild_id,))
    trackers = [_tracker_dict(r) for r in cursor.fetchall()]
    for t in trackers:
        cursor.execute("SELECT COUNT(*) FROM mod_progress_snapshots WHERE tracker_id = ?", (t["id"],))
        t["snapshots"] = cursor.fetchone()[0]
        cursor.execute("SELECT created_at FROM mod_progress_versions WHERE tracker_id = ? AND version_num > 1 ORDER BY version_num",
                       (t["id"],))
        t["changes"] = [msk_date(r[0]) for r in cursor.fetchall()]
    conn.close()
    return trackers


def set_period(guild_id: int, tracker_id: int, period_days: int) -> None:
    if period_days not in PERIODS:
        return
    conn = _connect()
    conn.execute("UPDATE mod_progress_trackers SET period_days = ? WHERE id = ? AND guild_id = ?",
                 (period_days, tracker_id, guild_id))
    conn.commit()
    conn.close()


def delete_tracker(guild_id: int, tracker_id: int) -> bool:
    conn = _connect()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM mod_progress_trackers WHERE id = ? AND guild_id = ?", (tracker_id, guild_id))
    if not cursor.rowcount:
        conn.close()
        return False
    cursor.execute("SELECT id FROM mod_progress_snapshots WHERE tracker_id = ?", (tracker_id,))
    snap_ids = [r[0] for r in cursor.fetchall()]
    for sid in snap_ids:
        cursor.execute("DELETE FROM mod_progress_values WHERE snapshot_id = ?", (sid,))
        cursor.execute("DELETE FROM mod_progress_scen_values WHERE snapshot_id = ?", (sid,))
        cursor.execute("DELETE FROM mod_progress_players WHERE snapshot_id = ?", (sid,))
    cursor.execute("DELETE FROM mod_progress_snapshots WHERE tracker_id = ?", (tracker_id,))
    cursor.execute("DELETE FROM mod_progress_versions WHERE tracker_id = ?", (tracker_id,))
    conn.commit()
    conn.close()
    return True


def delete_snapshot(guild_id: int, tracker_id: int, snapshot_id: int) -> bool:
    if not get_tracker(guild_id, tracker_id):
        return False
    conn = _connect()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM mod_progress_snapshots WHERE id = ? AND tracker_id = ?", (snapshot_id, tracker_id))
    deleted = bool(cursor.rowcount)
    if deleted:
        cursor.execute("DELETE FROM mod_progress_values WHERE snapshot_id = ?", (snapshot_id,))
        cursor.execute("DELETE FROM mod_progress_scen_values WHERE snapshot_id = ?", (snapshot_id,))
        cursor.execute("DELETE FROM mod_progress_players WHERE snapshot_id = ?", (snapshot_id,))
    conn.commit()
    conn.close()
    return deleted


def all_tracker_ids(guild_ids: set[int]) -> list[int]:
    conn = _connect()
    cursor = conn.cursor()
    cursor.execute("SELECT id, guild_id FROM mod_progress_trackers")
    ids = [tid for tid, gid in cursor.fetchall() if gid in guild_ids]
    conn.close()
    return ids


def due_tracker_ids(guild_ids: set[int]) -> list[int]:
    """Трекеры с автоснимками, у которых период уже прошёл (с допуском в час, чтобы снимок
    не уезжал каждый раз на час позже из-за часового цикла)."""
    conn = _connect()
    cursor = conn.cursor()
    cursor.execute("SELECT id, guild_id, period_days, last_snapshot_at FROM mod_progress_trackers WHERE period_days > 0")
    now = datetime.now(timezone.utc)
    due = []
    for tid, gid, period, last in cursor.fetchall():
        if gid not in guild_ids:
            continue
        if last is None or now - _parse(last) >= timedelta(days=period) - timedelta(hours=1):
            due.append(tid)
    conn.close()
    return due


# ---------------- снимок ----------------

async def take_snapshot(bot_like, tracker_id: int, kind: str, taken_by: str | None = None) -> int | None:
    """Снимок всех игроков гильдии по текущей версии плейта. bot_like — объект с .comlink и
    .stat_calc (бот или services.stat_forecast._bot_stand_in). Возвращает id снимка."""
    lock = _locks.setdefault(tracker_id, asyncio.Lock())
    async with lock:
        version = sync_version(tracker_id)
        if version is None:
            return None
        conn = _connect()
        cursor = conn.cursor()
        cursor.execute("SELECT guild_id, plate_name FROM mod_progress_trackers WHERE id = ?", (tracker_id,))
        guild_id, plate_name = cursor.fetchone()
        conn.close()

        keys_by_char: dict[str, list[tuple[str, str]]] = {}
        for r in version["reqs"]:
            keys_by_char.setdefault(r["base_id"], []).append((r["key"], r.get("source") or plate_name))
        squads = database.get_stat_plate_squads(guild_id)

        players, values = [], []
        for _discord_id, ally_code, name in database.get_all_user_mappings(guild_id):
            missing = _missing_plate_squads(ally_code, guild_id, squads) if squads else {}
            evaluated = 0
            for base_id, keys in keys_by_char.items():
                for scenario in SCENARIOS:
                    out = []
                    result = await _evaluate_character_player(
                        bot_like, plate_name, base_id, ally_code, False, name, guild_id=guild_id,
                        scenario=scenario, rows_out=out,
                    )
                    if result is None:
                        continue
                    if scenario == SCENARIO_RAW:
                        evaluated += 1
                    if not out:
                        # Юнита нет у игрока (не открыт) — все его требования не выполнены.
                        out = [{"key": k, "value": "нет юнита", "margin": None, "ok": False} for k, _source in keys]
                    for item in out:
                        ok = None if item["ok"] is None else int(bool(item["ok"]))
                        values.append((scenario, ally_code, item["key"], item["value"], item["margin"], ok))
            players.append((ally_code, name, None if evaluated else "нет данных ростера",
                            json.dumps(missing, ensure_ascii=False) if missing else None))
            await asyncio.sleep(0)

        now = _now()
        conn = _connect()
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO mod_progress_snapshots (tracker_id, version_id, taken_at, kind, taken_by, scenarios, all_players)
            VALUES (?, ?, ?, ?, ?, ?, 1)
        """, (tracker_id, version["id"], now, kind, taken_by, ",".join(SCENARIOS)))
        snapshot_id = cursor.lastrowid
        cursor.executemany("""
            INSERT OR REPLACE INTO mod_progress_players (snapshot_id, ally_code, name, skip_reason, squad_missing)
            VALUES (?, ?, ?, ?, ?)
        """, [(snapshot_id, *p) for p in players])
        cursor.executemany("INSERT OR REPLACE INTO mod_progress_values VALUES (?, ?, ?, ?, ?, ?)",
                           [(snapshot_id, *v[1:]) for v in values if v[0] == SCENARIO_RAW])
        cursor.executemany("INSERT OR REPLACE INTO mod_progress_scen_values VALUES (?, ?, ?, ?, ?, ?, ?)",
                           [(snapshot_id, *v) for v in values if v[0] != SCENARIO_RAW])
        cursor.execute("UPDATE mod_progress_trackers SET last_snapshot_at = ? WHERE id = ?", (now, tracker_id))
        conn.commit()
        conn.close()
        return snapshot_id


# ---------------- чтение для страниц ----------------

def scenario_or_default(scenario: str | None) -> str:
    return scenario if scenario in SCENARIOS else SCENARIO_RAW


def _snapshots(cursor, tracker_id: int, scenario: str = SCENARIO_RAW) -> list[dict]:
    """Снимки трекера, в которых посчитан этот сценарий (старые — только «как есть»)."""
    cursor.execute("""
        SELECT s.id, s.taken_at, s.kind, v.version_num, v.id, s.scenarios, s.all_players
        FROM mod_progress_snapshots s JOIN mod_progress_versions v ON v.id = s.version_id
        WHERE s.tracker_id = ? ORDER BY s.taken_at, s.id
    """, (tracker_id,))
    snaps = []
    prev_version = None
    for sid, taken_at, kind, vnum, vid, scenarios, all_players in cursor.fetchall():
        if scenario not in (scenarios or SCENARIO_RAW).split(","):
            continue
        snaps.append({"id": sid, "taken_at": taken_at, "date": msk_short(taken_at),
                      "datetime": msk_date(taken_at, True), "kind": kind, "version_num": vnum, "version_id": vid, "all_players": bool(all_players),
                      "plate_changed": prev_version is not None and vnum != prev_version})
        prev_version = vnum
    # Несколько снимков за день — в подписи столбца ещё и время.
    per_day = {}
    for s in snaps:
        per_day[s["date"]] = per_day.get(s["date"], 0) + 1
    for s in snaps:
        if per_day[s["date"]] > 1:
            s["date"] = _parse(s["taken_at"]).astimezone(MSK).strftime("%d.%m %H:%M")
    return snaps


def _versions(cursor, tracker_id: int) -> dict[int, dict]:
    cursor.execute("SELECT id, version_num, reqs_json, created_at FROM mod_progress_versions WHERE tracker_id = ?",
                   (tracker_id,))
    return {vid: {"num": num, "reqs": json.loads(rj), "created_at": created_at, "date": msk_date(created_at)}
            for vid, num, rj, created_at in cursor.fetchall()}


def _snapshot_values(cursor, snapshot_id: int, ally_code: str | None = None, scenario: str = SCENARIO_RAW) -> dict:
    """{ally_code: {req_key: (value, margin, ok)}}"""
    if scenario == SCENARIO_RAW:
        sql, args = "SELECT ally_code, req_key, value, margin, ok FROM mod_progress_values WHERE snapshot_id = ?", [snapshot_id]
    else:
        sql = "SELECT ally_code, req_key, value, margin, ok FROM mod_progress_scen_values WHERE snapshot_id = ? AND scenario = ?"
        args = [snapshot_id, scenario]
    if ally_code:
        sql += " AND ally_code = ?"
        args.append(ally_code)
    cursor.execute(sql, args)
    result = {}
    for ac, key, value, margin, ok in cursor.fetchall():
        result.setdefault(ac, {})[key] = (value, margin, ok)
    return result


def _snapshot_players(cursor, snap: dict, plate_name: str, squad_only: bool) -> dict:
    """{ally_code: (name, причина пропуска | None, {плейт без отряда: причина})}. Отряды
    отбрасываются только при squad_only; в старых снимках (до all_players) игроки без отряда
    и так не посчитаны (skip_reason)."""
    cursor.execute("SELECT ally_code, name, skip_reason, squad_missing FROM mod_progress_players WHERE snapshot_id = ?",
                   (snap["id"],))
    players = {}
    for ally_code, name, skip_reason, squad_missing in cursor.fetchall():
        missing = json.loads(squad_missing) if squad_missing and squad_only and snap["all_players"] else {}
        players[ally_code] = (name, skip_reason or missing.get(plate_name), missing)
    return players


def _squad_filter(values: dict, reqs_by_key: dict, missing: dict, plate_name: str) -> tuple[dict, str | None]:
    """Отбрасывает строки плейтов, отряда которых у игрока нет. (значения, причина) — причина,
    если отброшено всё (персонажи плейта игроку не нужны)."""
    if not missing:
        return values, None
    kept = {k: v for k, v in values.items()
            if (reqs_by_key.get(k, {}).get("source") or plate_name) not in missing}
    return kept, (None if kept or not values else next(iter(missing.values())))


def _counts(player_values: dict, reqs_by_key: dict) -> tuple[int, int]:
    """(выполнено, всего) по обязательным требованиям версии снимка."""
    done = total = 0
    for key, (_value, _margin, ok) in player_values.items():
        req = reqs_by_key.get(key)
        if req is None or req["priority"] != "required" or ok is None:
            continue
        total += 1
        done += ok
    return done, total


def _changed_required(cur_vals: dict, other_vals: dict, reqs_latest: dict) -> list[dict]:
    """Какие обязательные требования сменили статус (не выполнено ↔ выполнено) между
    текущим снимком и другим (прошлым/первым) — для «Сдвиг»: список {char, label, req, to}
    в порядке плейта, to="ok" — закрыл, to="fail" — откатился. Строки, которых нет в одном
    из снимков (требование отсутствовало тогда, или не считалось этому игроку), пропускаются
    — "сдвиг" тут про смену статуса, а не про появление/исчезновение требования."""
    changed = []
    for key, req in reqs_latest.items():  # порядок плейта
        if req["priority"] != "required" or key not in cur_vals or key not in other_vals:
            continue
        _cur_value, _cur_margin, cur_ok = cur_vals[key]
        _other_value, _other_margin, other_ok = other_vals[key]
        if cur_ok is None or other_ok is None or cur_ok == other_ok:
            continue
        changed.append({"char": req["char"], "label": req["label"], "req": req["req"],
                         "to": "ok" if cur_ok else "fail"})
    return changed


def _gap_text(value, margin, req: dict) -> str:
    """Сколько не хватает до нормы — только у числовых требований (стат, сравнение с
    персонажем: норма начинается с оператора); у основ, сетов, омикронов — пусто."""
    if margin is None or margin >= 0 or not value or req["req"][:1] not in ("<", ">", "="):
        return ""
    return _fmt_compact(-margin)


def guild_summary(guild_id: int, tracker_id: int, scenario: str = SCENARIO_RAW, squad_only: bool = True) -> dict | None:
    """Сводка по гильдии на последнем снимке сценария: выполнено X из Y обязательных, сдвиг
    с прошлого и с первого снимка (число + changed_prev/changed_first — какие именно
    требования закрыл/откатил, см. _changed_required), что не выполнено (по персонажам, с
    нехваткой)."""
    tracker = get_tracker(guild_id, tracker_id)
    if not tracker:
        return None
    conn = _connect()
    cursor = conn.cursor()
    snaps = _snapshots(cursor, tracker_id, scenario)
    all_snaps = _snapshots(cursor, tracker_id) if scenario != SCENARIO_RAW else snaps
    versions = _versions(cursor, tracker_id)
    result = {"tracker": tracker, "snapshots": snaps, "players": [], "skipped": [],
              "scenario": scenario, "squad_only": squad_only, "older_snapshots": len(all_snaps) - len(snaps),
              "changes": [v for v in sorted(versions.values(), key=lambda v: v["num"]) if v["num"] > 1]}
    if not snaps:
        conn.close()
        return result
    latest, prev, first = snaps[-1], (snaps[-2] if len(snaps) > 1 else None), snaps[0]
    reqs_latest = {r["key"]: r for r in versions[latest["version_id"]]["reqs"]}
    cur = _snapshot_values(cursor, latest["id"], scenario=scenario)
    prev_vals = _snapshot_values(cursor, prev["id"], scenario=scenario) if prev else {}
    first_vals = _snapshot_values(cursor, first["id"], scenario=scenario) if len(snaps) > 1 else {}
    reqs_prev = {r["key"]: r for r in versions[prev["version_id"]]["reqs"]} if prev else {}
    reqs_first = {r["key"]: r for r in versions[first["version_id"]]["reqs"]}
    names = database.get_player_names(guild_id)
    plate_name = tracker["plate_name"]
    cur_players = _snapshot_players(cursor, latest, plate_name, squad_only)
    prev_players = _snapshot_players(cursor, prev, plate_name, squad_only) if prev else {}
    first_players = _snapshot_players(cursor, first, plate_name, squad_only) if first_vals else {}

    def other_values(vals: dict, players: dict, reqs: dict, ally_code: str) -> dict | None:
        # Прошлый/первый снимок — с тем же фильтром отрядов, что и последний.
        if ally_code not in vals or ally_code not in players or players[ally_code][1]:
            return None
        return _squad_filter(vals[ally_code], reqs, players[ally_code][2], plate_name)[0]

    for ally_code, (name, skip_reason, missing_squads) in cur_players.items():
        name = names.get(ally_code) or name or ally_code
        pv, all_dropped = _squad_filter(cur.get(ally_code, {}), reqs_latest, missing_squads, plate_name)
        skip_reason = skip_reason or all_dropped
        if skip_reason:
            result["skipped"].append({"name": name, "reason": skip_reason})
            continue
        done, total = _counts(pv, reqs_latest)
        missing_by_char = {}
        for key, req in reqs_latest.items():  # порядок плейта
            if key not in pv or req["priority"] != "required":
                continue
            value, margin, ok = pv[key]
            if ok == 0:
                missing_by_char.setdefault(req["char"], []).append(
                    {"label": req["label"], "req": req["req"], "value": value or "—", "gap": _gap_text(value, margin, req)})
        missing = sorted(missing_by_char.items(), key=lambda x: -len(x[1]))
        entry = {"ally_code": ally_code, "name": name, "done": done, "total": total, "missing": missing,
                 "missing_count": sum(len(items) for _char, items in missing)}
        prev_pv = other_values(prev_vals, prev_players, reqs_prev, ally_code) if prev else None
        if prev_pv is not None:
            entry["delta_prev"] = done - _counts(prev_pv, reqs_prev)[0]
            entry["changed_prev"] = _changed_required(pv, prev_pv, reqs_latest)
        first_pv = other_values(first_vals, first_players, reqs_first, ally_code) if first_vals else None
        if first_pv is not None:
            entry["delta_first"] = done - _counts(first_pv, reqs_first)[0]
            entry["changed_first"] = _changed_required(pv, first_pv, reqs_latest)
        result["players"].append(entry)
    conn.close()
    result["players"].sort(key=lambda e: (e["done"] - e["total"], e["name"].lower()))
    result["skipped"].sort(key=lambda e: e["name"].lower())
    result["total_done"] = sum(e["done"] for e in result["players"])
    result["total_reqs"] = sum(e["total"] for e in result["players"])
    result["full"] = sum(1 for e in result["players"] if e["total"] and e["done"] == e["total"])
    return result


def _cell_state(ok, margin, prev_margin, prev_ok) -> str:
    """ok — выполнено; up — не выполнено, но ближе к норме, чем в прошлом снимке; down —
    было выполнено/ближе, стало хуже; stall — без сдвига; none — нет данных."""
    if ok is None:
        return "none"
    if ok:
        return "ok"
    if prev_ok:
        return "down"
    if margin is not None and prev_margin is not None:
        if margin > prev_margin + 1e-9:
            return "up"
        if margin < prev_margin - 1e-9:
            return "down"
    return "stall"


def player_table(guild_id: int, tracker_id: int, ally_code: str, limit: int | None = 6,
                 scenario: str = SCENARIO_RAW, squad_only: bool = True) -> dict | None:
    """Таблица игрока: строки — требования (по персонажам), колонки — снимки (последние
    limit, None — все). Норма берётся из версии каждого снимка; если в колонке она другая,
    чем в последней версии, — показывается в ячейке."""
    tracker = get_tracker(guild_id, tracker_id)
    if not tracker:
        return None
    conn = _connect()
    cursor = conn.cursor()
    snaps = _snapshots(cursor, tracker_id, scenario)
    versions = _versions(cursor, tracker_id)
    presence, all_values = {}, {}
    for s in snaps:
        player = _snapshot_players(cursor, s, tracker["plate_name"], squad_only).get(ally_code)
        if player is None:
            continue
        name_then, reason, missing_squads = player
        reqs_here = {r["key"]: r for r in versions[s["version_id"]]["reqs"]}
        vals = _snapshot_values(cursor, s["id"], ally_code, scenario).get(ally_code, {})
        vals, all_dropped = _squad_filter(vals, reqs_here, missing_squads, tracker["plate_name"])
        presence[s["id"]] = (name_then, reason or all_dropped)
        all_values[s["id"]] = {} if reason else vals
    snaps = [s for s in snaps if s["id"] in presence]
    conn.close()
    if not snaps:
        return {"tracker": tracker, "columns": [], "groups": [], "name": ally_code, "ally_code": ally_code,
                "hidden": 0, "scenario": scenario, "squad_only": squad_only}

    name = database.get_player_names(guild_id).get(ally_code) or presence[snaps[-1]["id"]][0] or ally_code
    shown = snaps if limit is None else snaps[-limit:]
    hidden = len(snaps) - len(shown)
    first_shown_idx = len(snaps) - len(shown)
    latest_reqs = versions[snaps[-1]["version_id"]]["reqs"]

    # Порядок строк: последняя версия плейта, затем убранные из плейта (из старых версий).
    order, meta = [], {}
    for r in latest_reqs:
        order.append(r["key"])
        meta[r["key"]] = {**r, "removed": False}
    for s in reversed(shown):
        for r in versions[s["version_id"]]["reqs"]:
            if r["key"] not in meta:
                order.append(r["key"])
                meta[r["key"]] = {**r, "removed": True}

    columns = []
    for i, s in enumerate(shown):
        reason = presence[s["id"]][1]
        columns.append({**s, "skip_reason": reason, "version_date": versions[s["version_id"]]["date"]})

    groups: dict[str, dict] = {}
    for key in order:
        m = meta[key]
        cells = []
        for idx, s in enumerate(shown):
            reqs_here = {r["key"]: r for r in versions[s["version_id"]]["reqs"]}
            abs_idx = first_shown_idx + idx
            prev = all_values[snaps[abs_idx - 1]["id"]].get(key) if abs_idx > 0 else None
            if key not in reqs_here:
                cells.append({"state": "absent", "value": "", "note": "не было в плейте"})
                continue
            if key not in all_values[s["id"]]:
                cells.append({"state": "absent", "value": "—", "note": presence[s["id"]][1] or ""})
                continue
            value, margin, ok = all_values[s["id"]][key]
            note = reqs_here[key]["req"] if reqs_here[key]["req"] != m["req"] else ""
            cells.append({"state": _cell_state(ok, margin, prev[1] if prev else None, prev[2] if prev else None),
                          "value": value or "нет данных", "note": f"норма тогда: {note}" if note else ""})
        if all(c["state"] == "absent" and not c["value"] for c in cells):
            continue
        group = groups.setdefault(m["base_id"], {"char": m["char"], "rows": []})
        group["rows"].append({"label": m["label"], "req": m["req"], "priority": m["priority"],
                              "removed": m["removed"], "cells": cells})
    return {"tracker": tracker, "name": name, "ally_code": ally_code, "columns": columns,
            "groups": list(groups.values()), "hidden": hidden, "scenario": scenario, "squad_only": squad_only}
