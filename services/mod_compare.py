"""«Сравнение гильдий» в анализе модинга (web /mod-analysis/compare) — как модят одних и тех же
персонажей в нескольких гильдиях сразу (просьба NicolozZ, тред «Гайд по АС Боту» 2026-10-25:
руками собирал по VDO/RedSun/Clones Unleashed… скорость, атаку и основы — «надо
автоматизировать»). До 6 чужих гильдий (код союзника любого участника или ID гильдии) + своя,
до 6 персонажей; по каждому персонажу — строка на гильдию: медианы скорости, урона, живучести,
куда гильдия вкладывается (топ-3 по GF модели /mod-analysis), самые частые сеты и основы.

Своя гильдия — из player_unit_cache. Чужие — живой get_player по каждому участнику (5 параллельно);
ростеры кэшируются в mod_compare_rosters на сутки (только «релевантные» юниты — фильтр
mod_analysis._relevant_unit: релик > 5, 6 модулей 15 уровня), так что повторное сравнение
с другими персонажами не опрашивает гильдию заново. Сбор идёт фоном (fetch_guild, прогресс в
_progress), страница перезагружается, пока он не закончится.

Реликвия: «как есть» (None — реальный релик каждого игрока, как NicolozZ смотрел руками) или
одна целевая для всех (stat_engine.project_unit_relic, как в /mod-analysis)."""

import asyncio
import json
import sqlite3
import statistics
import time
import zlib
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import database
import stat_engine
from services import mod_analysis, mod_search, toon

MAX_GUILDS = 6
MAX_CHARS = 6
ROSTER_TTL_SECONDS = 24 * 3600
FETCH_PARALLEL = 5
_REF_TTL_SECONDS = 3600
MSK = ZoneInfo("Europe/Moscow")

# Строки блока «Статы»: (ключ, подпись). Урон — родной тип персонажа (физ./особый, по базе).
STAT_LINES = [
    ("Damage", "Урон"),
    ("Critical Damage", "Крит. урон"),
    ("Health", "Здоровье"),
    ("Protection", "Защита"),
    ("Potency", "Эффективность"),
    ("Tenacity", "Стойкость"),
]
PERCENT_STATS = {"Critical Damage", "Potency", "Tenacity"}
# Куда гильдия вкладывается — те же статы и пороги, что в /mod-analysis (GF), подписи короче.
FOCUS_LABELS = {"Critical Damage": "крит. урон", "Attack": "урон", "Speed": "скорость", "Health": "здоровье",
                "Protection": "защита", "Critical Chance": "крит. шанс", "Defense": "оборона",
                "Potency": "эффективность", "Tenacity": "стойкость"}
SLOT_SHORT = {"arrow": "стрела", "triangle": "треуг.", "circle": "круг", "cross": "крест"}
SET_SHORT = {1: "Здоровье", 2: "Атака", 3: "Оборона", 4: "Скорость", 5: "Шанс крита", 6: "Крит. урон",
             7: "Эффективность", 8: "Стойкость"}


def _combo_label(combo: tuple) -> str:
    """Как mod_analysis._combo_label, но по-русски: «Здоровье ×2 + Скорость»."""
    if not combo:
        return "без полного сета"
    return " + ".join(SET_SHORT.get(int(sid), f"#{sid}") + (f" ×{mult}" if mult > 1 else "") for sid, mult in combo)

# swgoh_guild_id -> {"name", "done", "total", "errors", "task"} — сбор ростеров в этом процессе.
_progress: dict[str, dict] = {}
# ref (код/ID, как ввели) -> (time, GuildLookupResult)
_refs: dict[str, tuple] = {}


def _connect():
    conn = sqlite3.connect(database.DB_NAME)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS mod_compare_rosters (
            swgoh_guild_id TEXT PRIMARY KEY,
            guild_name TEXT,
            fetched_at REAL NOT NULL,
            members INTEGER NOT NULL,
            errors INTEGER NOT NULL DEFAULT 0,
            payload BLOB NOT NULL
        )
    """)
    conn.commit()
    return conn


def roster_info(swgoh_guild_id: str) -> dict | None:
    """{name, fetched_at, members, errors, age_hours} или None, если ростеры не собирались."""
    conn = _connect()
    row = conn.execute("SELECT guild_name, fetched_at, members, errors FROM mod_compare_rosters WHERE swgoh_guild_id = ?",
                       (swgoh_guild_id,)).fetchone()
    conn.close()
    if not row:
        return None
    name, fetched_at, members, errors = row
    return {"name": name, "fetched_at": fetched_at, "members": members, "errors": errors,
            "age_hours": (time.time() - fetched_at) / 3600,
            "fetched_msk": datetime.fromtimestamp(fetched_at, timezone.utc).astimezone(MSK).strftime("%d.%m %H:%M")}


def load_roster(swgoh_guild_id: str) -> list[tuple]:
    """[(player_id, name, {base_id: unit})] из кэша."""
    conn = _connect()
    row = conn.execute("SELECT payload FROM mod_compare_rosters WHERE swgoh_guild_id = ?", (swgoh_guild_id,)).fetchone()
    conn.close()
    if not row:
        return []
    return [(p["id"], p["name"], p["units"]) for p in json.loads(zlib.decompress(row[0]))]


def is_fresh(swgoh_guild_id: str) -> bool:
    info = roster_info(swgoh_guild_id)
    return info is not None and info["age_hours"] * 3600 < ROSTER_TTL_SECONDS


def is_fetching(swgoh_guild_id: str) -> bool:
    p = _progress.get(swgoh_guild_id)
    return bool(p) and p["task"] is not None and not p["task"].done()


async def resolve(comlink, ref: str):
    """Код союзника/ID гильдии -> GuildLookupResult (кэш на час — страница перезагружается
    во время сбора, не опрашивать игру на каждую перезагрузку)."""
    from services import steal_build
    cached = _refs.get(ref)
    if cached and time.time() - cached[0] < _REF_TTL_SECONDS:
        return cached[1]
    lookup = await steal_build.resolve_guild(comlink, ref)
    if lookup.ok:
        _refs[ref] = (time.time(), lookup)
    return lookup


async def _fetch(comlink, lookup) -> None:
    state = _progress[lookup.swgoh_guild_id]
    sem = asyncio.Semaphore(FETCH_PARALLEL)
    players = []

    async def one(player_id: str, name: str):
        async with sem:
            try:
                data = await asyncio.to_thread(comlink.get_player, player_id=player_id)
            except Exception:
                state["errors"] += 1
                return
            finally:
                state["done"] += 1
        units = {}
        for u in data.get("rosterUnit") or []:
            if mod_analysis._relevant_unit(u):
                units[(u.get("definitionId") or "").split(":")[0]] = u
        players.append({"id": player_id, "name": data.get("name") or name, "units": units})

    await asyncio.gather(*(one(pid, name) for pid, name in lookup.members))
    payload = zlib.compress(json.dumps(players, ensure_ascii=False).encode("utf-8"), 6)
    conn = _connect()
    conn.execute("INSERT OR REPLACE INTO mod_compare_rosters VALUES (?, ?, ?, ?, ?, ?)",
                 (lookup.swgoh_guild_id, lookup.guild_name, time.time(), len(lookup.members), state["errors"], payload))
    conn.commit()
    conn.close()


def start_fetch(comlink, lookup) -> None:
    """Фоновый сбор ростеров гильдии (если уже не идёт)."""
    if is_fetching(lookup.swgoh_guild_id):
        return
    state = {"name": lookup.guild_name, "done": 0, "total": len(lookup.members), "errors": 0, "task": None}
    _progress[lookup.swgoh_guild_id] = state

    async def run():
        try:
            await _fetch(comlink, lookup)
        except Exception as e:
            state["error"] = str(e) or e.__class__.__name__
            print(f"⚠️ [web] сравнение гильдий: сбор {lookup.guild_name} не удался: {state['error']}")

    state["task"] = asyncio.create_task(run())


def progress(swgoh_guild_id: str) -> dict | None:
    p = _progress.get(swgoh_guild_id)
    return {k: v for k, v in p.items() if k != "task"} if p else None


def own_roster(guild_id: int) -> list[tuple]:
    """Своя гильдия из player_unit_cache — в том же виде, что load_roster."""
    roster = database.get_all_user_mappings(guild_id)
    units_by_ally = database.get_player_units_bulk([ac for _d, ac, _n in roster])
    return [(ac, name, {b: u for b, u in units_by_ally.get(ac, {}).items() if u and mod_analysis._relevant_unit(u)})
            for _d, ac, name in roster]


def _median(values: list):
    return statistics.median(values) if values else None


def _fmt(value, stat: str) -> str:
    if value is None:
        return "—"
    if stat in PERCENT_STATS:
        return f"{value:.0f}%"
    if abs(value) >= 10000:
        return f"{value / 1000:.0f}к"
    if abs(value) >= 1000:
        return f"{value / 1000:.1f}к".replace(".", ",")
    return f"{value:.0f}"


def _summary(stat_calc, base_id: str, roster: list[tuple], target_relic: int | None) -> dict:
    """Одна гильдия × один персонаж."""
    rows = []
    for _pid, _name, units in roster:
        unit = units.get(base_id)
        if not unit:
            continue
        decoded = [d for d in (mod_search.decode_mod(m) for m in unit["equippedStatMod"]) if d is not None]
        if len(decoded) != mod_analysis.REQUIRED_MOD_COUNT:
            continue
        current_relic = stat_engine.get_current_relic_level(unit)
        projected = unit if target_relic is None else stat_engine.project_unit_relic(unit, target_relic)
        final = stat_engine.calc_final_stats(stat_calc, projected)
        base = stat_engine.calc_base_stats(stat_calc, projected)
        rows.append({"relic": current_relic, "decoded": decoded, "final": final, "base": base,
                     "config": mod_analysis._config_key(decoded)})
    n = len(rows)
    result = {"n": n, "players": len(roster)}
    if not n:
        return result
    result["avg_relic"] = statistics.mean(r["relic"] for r in rows)

    speed_final = [r["final"].get("Speed", 0.0) for r in rows]
    speed_delta = [r["final"].get("Speed", 0.0) - r["base"].get("Speed", 0.0) for r in rows]
    result["speed"] = f"{_median(speed_final):.0f}"
    result["speed_delta"] = f"+{_median(speed_delta):.0f}"

    # Родной тип урона — по базе (у физика физ. база выше).
    phys = sum(r["base"].get("Physical Damage", 0.0) for r in rows)
    spec = sum(r["base"].get("Special Damage", 0.0) for r in rows)
    dmg_key = "Physical Damage" if phys >= spec else "Special Damage"
    stats = []
    for key, label in STAT_LINES:
        src = dmg_key if key == "Damage" else key
        finals = [r["final"].get(src, 0.0) for r in rows]
        if key in PERCENT_STATS:
            extra = _median([r["final"].get(src, 0.0) - r["base"].get(src, 0.0) for r in rows])
            extra_text = f"+{extra:.0f}" if extra is not None and extra >= 0.5 else ""
        else:
            pcts = [(r["final"].get(src, 0.0) / r["base"][src] - 1) * 100 for r in rows if r["base"].get(src)]
            extra = _median(pcts)
            extra_text = f"+{extra:.0f}%" if extra is not None and extra >= 0.5 else ""
        stats.append({"key": key, "label": label if key != "Damage" else ("Физ. урон" if dmg_key == "Physical Damage" else "Особый урон"),
                      "value": _fmt(_median(finals), key), "extra": extra_text})
    result["stats"] = stats

    # Куда вкладываются — GF из модели /mod-analysis.
    deltas = []
    for r in rows:
        composite = mod_analysis._attack_and_crit_chance_delta(r["final"], r["base"])
        composite["Defense"] = mod_analysis._defense_delta(r["final"], r["base"])
        d = {}
        for name, _label in mod_analysis.ANALYZED_STATS:
            if name in composite:
                d[name] = (composite[name]["delta"], composite[name]["base"])
            else:
                d[name] = (r["final"].get(name, 0.0) - r["base"].get(name, 0.0), r["base"].get(name, 0.0))
        deltas.append(d)
    focus = []
    for name, _label in mod_analysis.ANALYZED_STATS:
        st = mod_analysis._stat_delta_stats([d[name] for d in deltas], n, mod_analysis.STAT_SF_THRESHOLD[name])
        if st["n"] and st["gf"] and st["gf"] >= 0.1:
            focus.append((st["gf"], FOCUS_LABELS[name]))
    result["focus"] = [label for _gf, label in sorted(focus, reverse=True)[:3]]

    combos = {}
    for r in rows:
        combos[r["config"][0]] = combos.get(r["config"][0], 0) + 1
    result["sets"] = [{"label": _combo_label(c), "pct": round(cnt / n * 100)}
                      for c, cnt in sorted(combos.items(), key=lambda kv: -kv[1])[:2]]

    primaries = []
    for slot in mod_analysis.CONFIG_SLOTS:
        counts = {}
        for r in rows:
            d = next((m for m in r["decoded"] if m["slot_key"] == slot), None)
            if d and d.get("primary"):
                counts[d["primary"]["stat_id"]] = counts.get(d["primary"]["stat_id"], 0) + 1
        if counts:
            sid, cnt = max(counts.items(), key=lambda kv: kv[1])
            primaries.append({"slot": SLOT_SHORT[slot], "stat": toon.MOD_STAT_RU.get(sid) or mod_search.STAT_ID_TO_INFO.get(sid, (f"#{sid}", False))[0],
                              "pct": round(cnt / n * 100)})
    result["primaries"] = primaries
    return result


def compare(stat_calc, base_ids: list[str], guilds: list[dict], target_relic: int | None) -> list[dict]:
    """guilds — [{name, roster: [(id, name, {base_id: unit})], ...}]. [{base_id, char_name,
    rows: [{guild, summary}]}] по персонажам в заданном порядке."""
    names = database.get_game_unit_names(base_ids)
    out = []
    for base_id in base_ids:
        rows = [{"guild": g, "summary": _summary(stat_calc, base_id, g["roster"], target_relic)} for g in guilds]
        out.append({"base_id": base_id, "char_name": names.get(base_id, base_id), "rows": rows})
    return out
