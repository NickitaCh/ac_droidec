"""Разведка ВГ — web /tw/scout (как Scouting в HotUtils, решение пользователя 2026-10-27 «вг —
сделать то, что можем»): своя гильдия против соперника по сводке /профиль
(services/player_profile.py), участники по рангу ГМ (топ-10/25/50 и место к месту) и прогон
плейтов гильдии (/статы) по обеим сторонам — «у нас плейт проходят 40, у них 25».

Живое состояние ВГ без входа в аккаунт недоступно, поэтому соперник — из прошлых ВГ
(services/guild_compare.load_recent_opponents) или по коду союзника / ID гильдии.

Ростеры соперника собираются фоном (5 параллельных get_player, ~минута) и хранятся
ROSTER_TTL_SECONDS в tw_scout_rosters; отчёт считается тоже фоном (сводка ≈0,2 с на игрока,
плейты — StatCalc по каждому персонажу) и сохраняется в tw_scout_reports — последний на
соперника, страница показывает его сразу. Своя гильдия — из кэшей ежечасного синка."""

import asyncio
import json
import sqlite3
import time
import types
import zlib
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import database
from services import player_profile as pp

ROSTER_TTL_SECONDS = 12 * 3600
FETCH_PARALLEL = 5
KEEP_ROSTERS = 10
RANK_BRACKETS = (10, 25, 50)
PAUSE_SECONDS = 0.02  # после каждого игрока в прогоне плейтов
MSK = ZoneInfo("Europe/Moscow")

# (guild_id, swgoh_guild_id) -> {"stage", "done", "total", "error", "task"}
_progress: dict[tuple, dict] = {}


def _connect():
    conn = sqlite3.connect(database.DB_NAME)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS tw_scout_rosters (
            swgoh_guild_id TEXT PRIMARY KEY,
            guild_name TEXT,
            fetched_at REAL NOT NULL,
            members INTEGER NOT NULL,
            errors INTEGER NOT NULL DEFAULT 0,
            payload BLOB NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS tw_scout_reports (
            guild_id INTEGER NOT NULL,
            swgoh_guild_id TEXT NOT NULL,
            built_at REAL NOT NULL,
            payload BLOB NOT NULL,
            PRIMARY KEY (guild_id, swgoh_guild_id)
        )
    """)
    conn.commit()
    return conn


def _msk(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).astimezone(MSK).strftime("%d.%m %H:%M")


def roster_age(swgoh_guild_id: str) -> float | None:
    conn = _connect()
    row = conn.execute("SELECT fetched_at FROM tw_scout_rosters WHERE swgoh_guild_id = ?", (swgoh_guild_id,)).fetchone()
    conn.close()
    return time.time() - row[0] if row else None


def _load_rosters(swgoh_guild_id: str) -> tuple[str, float, int, list[dict]] | None:
    conn = _connect()
    row = conn.execute("SELECT guild_name, fetched_at, errors, payload FROM tw_scout_rosters WHERE swgoh_guild_id = ?",
                       (swgoh_guild_id,)).fetchone()
    conn.close()
    if not row:
        return None
    return row[0], row[1], row[2], json.loads(zlib.decompress(row[3]))


def load_report(guild_id: int, swgoh_guild_id: str) -> dict | None:
    conn = _connect()
    row = conn.execute("SELECT payload FROM tw_scout_reports WHERE guild_id = ? AND swgoh_guild_id = ?",
                       (guild_id, swgoh_guild_id)).fetchone()
    conn.close()
    return json.loads(zlib.decompress(row[0])) if row else None


def _save_report(guild_id: int, swgoh_guild_id: str, report: dict) -> None:
    conn = _connect()
    conn.execute("INSERT OR REPLACE INTO tw_scout_reports VALUES (?, ?, ?, ?)",
                 (guild_id, swgoh_guild_id, time.time(),
                  zlib.compress(json.dumps(report, ensure_ascii=False).encode("utf-8"), 6)))
    conn.commit()
    conn.close()


def progress(guild_id: int, swgoh_guild_id: str) -> dict | None:
    p = _progress.get((guild_id, swgoh_guild_id))
    if not p:
        return None
    running = p["task"] is not None and not p["task"].done()
    return {**{k: v for k, v in p.items() if k != "task"}, "running": running}


# ------------------------------------------------------------------ сбор ростеров

async def _fetch(comlink, lookup, state: dict) -> None:
    sem = asyncio.Semaphore(FETCH_PARALLEL)
    players, errors = [], 0
    state.update(stage="Собираю ростеры соперника", done=0, total=len(lookup.members))

    async def one(player_id: str, name: str):
        nonlocal errors
        async with sem:
            try:
                p = await asyncio.to_thread(comlink.get_player, player_id=player_id)
            except Exception:
                p = None
            finally:
                state["done"] += 1
        if not p or not p.get("rosterUnit"):
            errors += 1
            return
        players.append({"ally_code": str(p.get("allyCode") or player_id), "name": p.get("name") or name,
                        "roster": p["rosterUnit"], "datacrons": p.get("datacron") or [], "meta": pp.player_meta(p)})

    await asyncio.gather(*(one(pid, name) for pid, name in lookup.members))
    payload = zlib.compress(json.dumps(players, ensure_ascii=False).encode("utf-8"), 6)
    conn = _connect()
    conn.execute("INSERT OR REPLACE INTO tw_scout_rosters VALUES (?, ?, ?, ?, ?, ?)",
                 (lookup.swgoh_guild_id, lookup.guild_name, time.time(), len(lookup.members), errors, payload))
    old = conn.execute("SELECT swgoh_guild_id FROM tw_scout_rosters ORDER BY fetched_at DESC LIMIT -1 OFFSET ?",
                       (KEEP_ROSTERS,)).fetchall()
    conn.executemany("DELETE FROM tw_scout_rosters WHERE swgoh_guild_id = ?", old)
    conn.commit()
    conn.close()


def start(comlink, guild_id: int, lookup, plates: list[str], stat_calc, refetch: bool) -> None:
    """Фоном: (пере)собрать ростеры, если устарели или refetch, и посчитать отчёт."""
    key = (guild_id, lookup.swgoh_guild_id)
    current = _progress.get(key)
    if current and current["task"] is not None and not current["task"].done():
        return
    state = {"stage": "Готовлюсь", "done": 0, "total": 0, "error": None, "task": None}
    _progress[key] = state

    async def run():
        try:
            age = roster_age(lookup.swgoh_guild_id)
            if refetch or age is None or age > ROSTER_TTL_SECONDS:
                await _fetch(comlink, lookup, state)
            state.update(stage="Считаю сводку и плейты", done=0, total=0)
            report = await asyncio.to_thread(_compute, guild_id, lookup.swgoh_guild_id, plates, stat_calc, state)
            _save_report(guild_id, lookup.swgoh_guild_id, report)
            state["stage"] = "Готово"
        except Exception as e:
            state["error"] = str(e) or e.__class__.__name__
            print(f"⚠️ [web] разведка ВГ {lookup.guild_name}: {state['error']!r}")

    state["task"] = asyncio.create_task(run())


# ------------------------------------------------------------------ отчёт

def _overview(own: dict, opp: dict) -> list[dict]:
    """Строки «мы / они»: (подпись, мы, они, больше — лучше)."""
    def gl_total(g, field="owners"):
        return sum(u[field] for u in g["gls"])

    def relic_plus(g, n):
        return sum(g["relics"].get(f"r{i}", 0) for i in range(n, 11))

    rows = [
        ("Участников", own["members"], opp["members"], True),
        ("ГМ гильдии", own["gp"], opp["gp"], True),
        ("Средний ГМ", own["avg"]["gp"], opp["avg"]["gp"], True),
        ("Средние лучшие 80", own["avg"]["top80"], opp["avg"]["top80"], True),
        ("ГЛ всего", gl_total(own), gl_total(opp), True),
        ("Ульты ГЛ", gl_total(own, "ultimates"), gl_total(opp, "ultimates"), True),
        ("G13", own["g13"], opp["g13"], True),
        ("R9+", relic_plus(own, 9), relic_plus(opp, 9), True),
        ("R8+", relic_plus(own, 8), relic_plus(opp, 8), True),
        ("R7+", relic_plus(own, 7), relic_plus(opp, 7), True),
        ("Сумма реликвий", own["relics"]["total"], opp["relics"]["total"], True),
        ("Омикроны ВГ", own["omicrons_tw"], opp["omicrons_tw"], True),
        ("Омикроны всего", own["omicrons"], opp["omicrons"], True),
        ("Модули скорость 25+", own["mods"].get("s25"), opp["mods"].get("s25"), True),
        ("Модули скорость 20+", own["mods"].get("s20"), opp["mods"].get("s20"), True),
        ("Ср. скорость от модулей G11+", own["avg"]["speed_g11"], opp["avg"]["speed_g11"], True),
        ("Средний МодКу", own["avg"]["modku"], opp["avg"]["modku"], True),
        ("Датакроны 9 уровня", own["datacrons"]["l9"], opp["datacrons"]["l9"], True),
    ]
    return [{"label": l, "own": a, "opp": b, "higher": h} for l, a, b, h in rows]


def _units(own: dict, opp: dict, key: str) -> list[dict]:
    """ГЛ/мета/флот: сколько у кого (и с ультой, R8+)."""
    by_id = {}
    for side, g in (("own", own), ("opp", opp)):
        for u in g[key]:
            row = by_id.setdefault(u["base_id"], {"base_id": u["base_id"], "name": u["name"], "is_ship": u["is_ship"],
                                                  "own": 0, "opp": 0, "own_ult": 0, "opp_ult": 0,
                                                  "own_r8": 0, "opp_r8": 0})
            row[side] = u["owners"]
            row[f"{side}_ult"] = u["ultimates"]
            row[f"{side}_r8"] = u["r8_plus"]
    return list(by_id.values())


def _ranks(own_players: list[dict], opp_players: list[dict]) -> dict:
    own = sorted(((p["meta"].get("name") or p["meta"].get("ally_code"), p["gp"]) for p in own_players), key=lambda x: -x[1])
    opp = sorted(((p["meta"].get("name") or p["meta"].get("ally_code"), p["gp"]) for p in opp_players), key=lambda x: -x[1])
    brackets = []
    for n in RANK_BRACKETS:
        a, b = own[:n], opp[:n]
        if not a or not b:
            continue
        brackets.append({"n": n, "own": sum(g for _n, g in a) / len(a), "opp": sum(g for _n, g in b) / len(b),
                         "own_count": len(a), "opp_count": len(b)})
    rows = []
    for i in range(max(len(own), len(opp))):
        a = own[i] if i < len(own) else None
        b = opp[i] if i < len(opp) else None
        rows.append({"rank": i + 1, "own": a, "opp": b, "diff": (a[1] - b[1]) if a and b else None})
    ahead = sum(1 for r in rows if r["diff"] is not None and r["diff"] > 0)
    return {"brackets": brackets, "rows": rows, "ahead": ahead, "compared": sum(r["diff"] is not None for r in rows)}


def _row_summary(sr, plate: str, base_id: str, guild_id: int, stat: dict) -> list[dict]:
    """Попап персонажа в «Плейтах»: по каждой строке плейта — сколько не прошли у нас и у них (из
    тех, у кого персонаж есть) и среднее значение против нормы (запрос пользователя 28.10: «что
    именно не пройдено хотя бы в среднем»). Сверху — строки, которые чаще всего не проходят."""
    loaded = sr._load_char_rows(plate, base_id, guild_id)
    if not loaded or not stat:
        return []
    out = []
    for row in loaded[0]:
        key = sr.progress_row_key(row)
        if key not in stat:
            continue
        label, norm = sr.progress_row_text(row)
        operator, threshold, priority, compare_key = row[4], row[5], row[6], row[13]
        no_avg = row[3] == sr.STAT_SET  # «Сет: X (N мод.)» — среднее число наборов ничего не говорит
        item = {"label": label, "norm": norm, "required": priority == "required"}
        for side in ("own", "opp"):
            n, fails, margins = stat[key].get(side, [0, 0, []])
            avg = None
            if margins and not no_avg:
                m = sum(margins) / len(margins)
                # запас до нормы → среднее значение (у сравнения с персонажем норма плавающая —
                # показываем только запас)
                avg = sr._fmt_compact(m) if compare_key else sr._fmt_compact(threshold + sr._compare_offset_sign(operator) * m)
                item[f"{side}_short"] = m < 0
            item[side] = {"n": n, "fails": fails, "avg": avg, "is_margin": bool(compare_key)}
        if item["own"]["fails"] or item["opp"]["fails"]:
            out.append(item)
    share = lambda x: x["fails"] / x["n"] if x["n"] else 0
    out.sort(key=lambda i: (not i["required"], -max(share(i["own"]), share(i["opp"]))))
    return out


async def _plates(guild_id: int, plates: list[str], sides: dict, stat_calc, state: dict) -> list[dict]:
    from cogs import stat_requirements as sr
    bot = types.SimpleNamespace(comlink=None, stat_calc=stat_calc)
    sr.FOREIGN_ROSTERS.set({code: roster for players in sides.values() for code, _name, roster in players})
    sr.ROWS_CACHE.set({})
    state.update(done=0, total=len(plates))
    out = []
    for plate in plates:
        chars = database.get_stat_requirement_characters(plate, guild_id=guild_id)
        names = database.get_game_unit_names(chars)
        per_char = {b: {"base_id": b, "name": names.get(b, b), "own": 0, "opp": 0, "own_has": 0, "opp_has": 0,
                        "checked": False} for b in chars}
        result = {"plate": plate, "own": 0, "opp": 0, "own_total": len(sides["own"]), "opp_total": len(sides["opp"])}
        rows_stat: dict = {b: {} for b in chars}  # {base_id: {row_key: {side: [n, fails, [margin…]]}}}
        for side, players in sides.items():
            for code, name, roster in players:
                passed = True
                for b in chars:
                    if b not in roster:
                        passed = False
                        continue
                    rows_out = []
                    r = await sr._evaluate_character_player(bot, plate, b, code, False, name, guild_id=guild_id,
                                                            rows_out=rows_out)
                    if r is None:
                        continue
                    per_char[b]["checked"] = True
                    per_char[b][f"{side}_has"] += 1
                    if r[5]:
                        passed = False
                    else:
                        per_char[b][side] += 1
                    for row in rows_out:
                        if row["ok"] is None:
                            continue
                        acc = rows_stat[b].setdefault(row["key"], {}).setdefault(side, [0, 0, []])
                        acc[0] += 1
                        acc[1] += not row["ok"]
                        if row["margin"] is not None:
                            acc[2].append(row["margin"])
                result[side] += passed
                # Расчёт идёт в потоке веб-процесса: пауза отпускает GIL, чтобы сайт не тормозил
                time.sleep(PAUSE_SECONDS)
        for b, c in per_char.items():
            c["rows"] = _row_summary(sr, plate, b, guild_id, rows_stat[b])
        result["chars"] = [c for c in per_char.values() if c["checked"]]
        if result["chars"]:
            out.append(result)
        state["done"] += 1
    return out


def _compute(guild_id: int, swgoh_guild_id: str, plates: list[str], stat_calc, state: dict) -> dict:
    loaded = _load_rosters(swgoh_guild_id)
    if not loaded:
        raise RuntimeError("Ростеры соперника не собрались")
    opp_name, fetched_at, errors, opp_raw = loaded
    cfg = database.get_guild_config(guild_id) or {}
    ctx = pp.load_context(pp.personal_units(cfg))
    opp_players = [pp.summarize(p["roster"], p["datacrons"], p["meta"], stat_calc, ctx) for p in opp_raw]
    own_players = pp.guild_players_from_cache(guild_id, stat_calc, ctx)
    if not own_players or not opp_players:
        raise RuntimeError("Нет ростеров своей гильдии или соперника")
    own = pp.summarize_guild(cfg.get("name") or "Мы", None, own_players, ctx)
    opp = pp.summarize_guild(opp_name, None, opp_players, ctx)

    plate_rows = []
    if plates:
        names = database.get_player_names(guild_id)
        own_codes = [code for code, _n in database.get_roster_by_player_id(guild_id).values() if code]
        own_units = database.get_player_units_bulk(own_codes)
        sides = {
            "own": [(c, names.get(c, c), own_units[c]) for c in own_codes if own_units.get(c)],
            "opp": [(p["ally_code"], p["name"], {pp._base_id(u): u for u in p["roster"]}) for p in opp_raw],
        }
        plate_rows = asyncio.run(_plates(guild_id, plates, sides, stat_calc, state))

    return {
        "own_name": own["name"], "opp_name": opp_name, "fetched_at": fetched_at, "fetched_msk": _msk(fetched_at),
        "built_msk": _msk(time.time()), "errors": errors, "plates_selected": plates,
        "overview": _overview(own, opp),
        "gls": _units(own, opp, "gls"), "meta": _units(own, opp, "meta_units"), "fleet": _units(own, opp, "fleet"),
        "personal": _units(own, opp, "personal"),
        "ranks": _ranks(own_players, opp_players),
        "plates": plate_rows,
    }
