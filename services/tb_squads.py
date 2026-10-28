"""Отряды для боёв ВИ «Восход Империи» — библиотека «миссия → какими отрядами проходят»
(2026-10-28, пользователь: «возьми их список отрядов… их отряды как базовый минимум»).

База — открытая карта ВИ swgoh.ru (`/api/tbmap`, без входа): по каждой планете миссии
(их номер c1…c8 — позиция на карте, не номер игровой зоны, поэтому миссии храним как у них:
номер, тип, условия, враги) и отряды (лидер + участники, все обязательные). Забирается
раз в сутки ботом (cogs/tasks.py), лежит в bot_state BASE_KEY; если сайт не ответил —
остаётся прошлая версия.

Поверх базы — правки гильдии (таблица tb_squad_overrides): скрыть базовый отряд и добавить
свои. squads_for() отдаёт итоговый список, player_squads() — какие из них игрок может
выставить: все юниты есть, персонажи на реликвии планеты (tb_platoon_data), корабли 7★."""

import json
import sqlite3
import time
import uuid

import httpx

import database
import tb_platoon_data
from services import tb_covert, tb_projection, unit_forecast

BASE_KEY = "tb_squads_base"
SOURCE_URL = "https://swgoh.ru/api/tbmap"
SOURCE_NAME = "swgoh.ru"
USER_AGENT = "AC-Droidec-bot/1.0 (+https://swgoh-sng.ru)"
MIN_PLANETS, MIN_SQUADS = 15, 50  # меньше — считаем, что у источника сменился формат
NEW_DAYS = 14  # сколько дней отряд/миссия источника помечены «новое»
FAIL_STATE = "tb_squads_fail"  # bot_state: текст последней ошибки, пока загрузка не починится
# Типы миссий у источника; deploy/platoon — не бои, их не берём.
MISSION_TYPES = {"usual": "Бой", "fleet": "Флот", "special": "Спецмиссия", "special2": "Спецмиссия", "reva": "Спецмиссия"}
MAX_UNITS = {"fleet": 8}
SQUAD_SIZE = 5


def _our_planet(name_en: str) -> str | None:
    """«Haven» → «Haven-class Medical Station», «Kafrene» → «Ring of Kafrene»."""
    ours = [p for branch in tb_projection.BRANCHES.values() for p in branch] + list(tb_projection.BONUS_PHASE)
    key = (name_en or "").lower()
    return next((p for p in ours if p.lower() == key), None) or next(
        (p for p in ours if key and (key in p.lower() or p.lower().split()[0] == key.split()[0])), None)


def parse(raw: dict) -> dict:
    """Ответ /api/tbmap → {planet: {phase, bonus, missions: [{id, type, label, requirements,
    enemies, squads: [{key, name, units, omicron, video}]}]}}."""
    out = {}
    for p in raw.get("planets") or []:
        planet = _our_planet(p.get("nameEn"))
        if not planet:
            continue
        missions = []
        for m in p.get("missions") or []:
            if m.get("type") not in MISSION_TYPES:
                continue
            squads = []
            for s in m.get("squads") or []:
                sq = s.get("squad")
                if sq:
                    leader = (sq.get("leader") or {}).get("baseId")
                    members = sorted(sq.get("members") or [], key=lambda x: x.get("order") or 0)
                    units = ([leader] if leader else []) + [(x.get("unit") or {}).get("baseId") for x in members]
                    omicron = ([leader] if sq.get("leaderHasOmicron") and leader else []) + \
                        [(x.get("unit") or {}).get("baseId") for x in members if x.get("hasOmicron")]
                    key, name = sq.get("_id") or "", sq.get("name") or ""
                else:  # отряд без карточки — просто список юнитов
                    units = [(x.get("unit") or {}).get("baseId") for x in sorted(s.get("units") or [], key=lambda x: x.get("order") or 0)]
                    omicron, key, name = [], s.get("_id") or "", ""
                units = [u for u in units if u]
                if units:
                    squads.append({"key": key, "name": name, "units": units, "omicron": [u for u in omicron if u],
                                   "video": s.get("videoUrl") or ""})
            missions.append({
                "id": m.get("missionId") or "", "type": m["type"], "label": MISSION_TYPES[m["type"]],
                "requirements": m.get("requirements") or [], "enemies": m.get("enemies") or [],
                "squads": squads,
            })
        missions.sort(key=lambda m: (m["type"] == "fleet", m["label"] != "Бой", m["id"]))
        out[planet] = {"phase": p.get("phase"), "bonus": bool(p.get("isBonus")), "missions": missions}
    return out


class SourceError(Exception):
    """Источник не ответил или ответ не похож на карту ВИ (сменился формат)."""


def merge(prev: dict, new: dict, now: float) -> dict:
    """Свежая база поверх прошлой: у каждой миссии и отряда — first_seen (когда впервые
    появились у источника; 0 — были с самого начала, не «новые»). Убранное источником не
    теряется: остаётся с removed_at, пока гильдия сама не скроет (правки офицеров главнее)."""
    first_load = not prev
    out = {}
    for planet in list(new) + [p for p in prev if p not in new]:
        n_planet, p_planet = new.get(planet), prev.get(planet) or {"missions": []}
        old_missions = {m["id"]: m for m in p_planet.get("missions") or []}
        missions = []
        for m in (n_planet or {"missions": []})["missions"]:
            old = old_missions.pop(m["id"], None)
            old_squads = {sq["key"]: sq for sq in (old or {}).get("squads") or []}
            squads = []
            for sq in m["squads"]:
                was = old_squads.pop(sq["key"], None)
                squads.append({**sq, "first_seen": was.get("first_seen", 0) if was else (0 if first_load else now)})
            squads += [{**sq, "removed_at": sq.get("removed_at") or now} for sq in old_squads.values()]
            missions.append({**m, "squads": squads,
                             "first_seen": old.get("first_seen", 0) if old else (0 if first_load else now)})
        missions += [{**m, "removed_at": m.get("removed_at") or now} for m in old_missions.values()]
        out[planet] = {**(n_planet or p_planet), "missions": missions}
    return out


def sync() -> dict:
    """Забирает базу с источника и сливает с прошлой (блокирующая — звать через to_thread).
    → {squads, new, removed}; SourceError — не скачалось или формат не тот (бот пишет владельцу)."""
    try:
        resp = httpx.get(SOURCE_URL, timeout=60, headers={"User-Agent": USER_AGENT})
        resp.raise_for_status()
        raw = resp.json()
    except Exception as e:
        raise SourceError(f"не скачалось: {e!r}"[:300]) from e
    planets = parse(raw)
    total = sum(len(m["squads"]) for p in planets.values() for m in p["missions"])
    if len(planets) < MIN_PLANETS or total < MIN_SQUADS:
        raise SourceError(f"ответ не похож на карту ВИ: планет {len(planets)}, отрядов {total} — похоже, сменился формат")
    now = time.time()
    merged = merge(base().get("planets") or {}, planets, now)
    database.set_bot_state(BASE_KEY, json.dumps({"fetched": now, "planets": merged}, ensure_ascii=False), guild_id=0)
    squads = [sq for p in merged.values() for m in p["missions"] for sq in m["squads"]]
    return {"squads": total, "new": sum(1 for sq in squads if sq.get("first_seen") == now),
            "removed": sum(1 for sq in squads if sq.get("removed_at") == now)}


def base() -> dict:
    raw = database.get_bot_state(BASE_KEY, guild_id=0)
    return json.loads(raw) if raw else {}


# ---------------------------------------------------------------- правки гильдии

def _ensure_table(cursor) -> None:
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS tb_squad_overrides (
            guild_id INTEGER NOT NULL, planet TEXT NOT NULL, mission TEXT NOT NULL,
            hidden_json TEXT NOT NULL DEFAULT '[]', custom_json TEXT NOT NULL DEFAULT '[]',
            updated_at TEXT, updated_by TEXT,
            PRIMARY KEY (guild_id, planet, mission)
        )""")


def overrides(guild_id: int) -> dict:
    """{(planet, mission): {"hidden": set, "custom": [squad]}}."""
    conn = sqlite3.connect(database.DB_NAME)
    cur = conn.cursor()
    _ensure_table(cur)
    conn.commit()
    rows = cur.execute("SELECT planet, mission, hidden_json, custom_json FROM tb_squad_overrides WHERE guild_id = ?",
                       (guild_id,)).fetchall()
    conn.close()
    return {(p, m): {"hidden": set(json.loads(h)), "custom": json.loads(c)} for p, m, h, c in rows}


def _save(guild_id: int, planet: str, mission: str, hidden: set, custom: list, by: str) -> None:
    conn = sqlite3.connect(database.DB_NAME)
    cur = conn.cursor()
    _ensure_table(cur)
    cur.execute("""INSERT OR REPLACE INTO tb_squad_overrides
                   (guild_id, planet, mission, hidden_json, custom_json, updated_at, updated_by)
                   VALUES (?, ?, ?, ?, ?, datetime('now'), ?)""",
                (guild_id, planet, mission, json.dumps(sorted(hidden)), json.dumps(custom, ensure_ascii=False), by))
    conn.commit()
    conn.close()


def set_hidden(guild_id: int, planet: str, mission: str, key: str, hidden: bool, by: str) -> None:
    cur = overrides(guild_id).get((planet, mission)) or {"hidden": set(), "custom": []}
    cur["hidden"].discard(key)
    if hidden:
        cur["hidden"].add(key)
    _save(guild_id, planet, mission, cur["hidden"], cur["custom"], by)


def add_custom(guild_id: int, planet: str, mission: str, name: str, units: list, by: str) -> None:
    cur = overrides(guild_id).get((planet, mission)) or {"hidden": set(), "custom": []}
    cur["custom"].append({"key": "g" + uuid.uuid4().hex[:10], "name": name.strip()[:80], "units": units, "omicron": [], "video": ""})
    _save(guild_id, planet, mission, cur["hidden"], cur["custom"], by)


def delete_custom(guild_id: int, planet: str, mission: str, key: str, by: str) -> None:
    cur = overrides(guild_id).get((planet, mission)) or {"hidden": set(), "custom": []}
    cur["custom"] = [s for s in cur["custom"] if s["key"] != key]
    _save(guild_id, planet, mission, cur["hidden"], cur["custom"], by)


def library(guild_id: int) -> dict:
    """База + правки гильдии: {planet: {phase, bonus, ru, min_relic, missions: [{…, squads:
    [{…, source: base|guild, hidden}]}]}} — скрытые базовые остаются (для страницы правки)."""
    data = base().get("planets") or {}
    ov = overrides(guild_id)
    out = {}
    for planet, p in data.items():
        missions = []
        for m in p["missions"]:
            o = ov.get((planet, m["id"])) or {"hidden": set(), "custom": []}
            fresh = time.time() - NEW_DAYS * 86400
            squads = [{**s, "source": "base", "hidden": s["key"] in o["hidden"],
                       "new": (s.get("first_seen") or 0) > fresh and not s.get("removed_at")} for s in m["squads"]]
            squads += [{**s, "source": "guild", "hidden": False} for s in o["custom"]]
            missions.append({**m, "squads": squads, "new": (m.get("first_seen") or 0) > fresh and not m.get("removed_at")})
        out[planet] = {**p, "missions": missions, "ru": tb_covert.planet_ru(planet),
                       "min_relic": tb_platoon_data.ROTE_MIN_RELIC_BY_PLANET.get(planet, 5)}
    sides = {planet: i for i, branch in enumerate(tb_projection.BRANCHES.values()) for planet in branch}
    return dict(sorted(out.items(), key=lambda kv: (kv[1]["phase"] or 9, kv[1]["bonus"], sides.get(kv[0], 9))))


# ---------------------------------------------------------------- кто может выставить

def _fits(unit: dict | None, min_relic: int, is_ship: bool) -> bool:
    if unit is None:
        return False
    lv = unit_forecast.current_level(unit)
    if is_ship:
        return lv["stars"] >= 7
    return lv["stars"] >= 7 and lv["relic"] is not None and lv["relic"] >= min_relic


def player_squads(mission: dict, roster: dict, min_relic: int, ships: set, busy: set = frozenset()) -> list[dict]:
    """Отряды миссии (без скрытых) со статусом для игрока: {…, ready, missing: [base_id],
    busy: [base_id]} — busy: юниты есть и годны, но заняты (во взводах этапа)."""
    out = []
    for s in mission["squads"]:
        if s.get("hidden"):
            continue
        missing = [u for u in s["units"] if not _fits(roster.get(u), min_relic, u in ships)]
        taken = [u for u in s["units"] if u in busy and u not in missing]
        out.append({**s, "ready": not missing and not taken, "missing": missing, "busy": taken})
    return out
