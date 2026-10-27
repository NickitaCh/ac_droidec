"""Сравнение игроков в карточке (как Compares → Player в HotUtils, решение пользователя
2026-10-27): до MAX_PLAYERS аккаунтов в столбик по той же сводке, что /профиль
(services/player_profile.summarize) — ГМ, прокачка, модули, датакроны, ГЛ, мета, флот.

Игроки гильдий бота — из кэшей ежечасного синка, остальные — живым get_player."""

import asyncio
import re

import database
from services import player_profile as pp

MAX_PLAYERS = 8
_CODE_RE = re.compile(r"(?<!\d)([1-9]{3})-?([1-9]{3})-?([1-9]{3})(?!\d)")


def parse_codes(raw: str) -> list[str]:
    """Коды союзника из свободного текста («Ник · 123-456-789, 987654321»), без повторов."""
    return list(dict.fromkeys("".join(m.groups()) for m in _CODE_RE.finditer(raw or "")))


async def load(comlink, codes: list[str], stat_calc) -> tuple[list[dict], list[str]]:
    """([summarize() по каждому найденному коду, в порядке codes], [не найденные коды])."""
    units = database.get_player_units_bulk(codes)
    datacrons = database.get_player_datacrons_bulk(codes)
    profiles = database.get_player_profiles_bulk(codes)
    ctx = await asyncio.to_thread(pp.load_context)

    async def one(code: str):
        roster = units.get(code)
        meta = profiles.get(code)
        if roster and meta:
            return await asyncio.to_thread(pp.summarize, list(roster.values()), datacrons.get(code) or [],
                                           meta, stat_calc, ctx)
        try:
            p = await asyncio.to_thread(comlink.get_player, allycode=code)
        except Exception as e:
            print(f"⚠️ [сравнение игроков] get_player {code}: {e!r}")
            return None
        if not p or not p.get("rosterUnit"):
            return None
        return await asyncio.to_thread(pp.summarize, p["rosterUnit"], p.get("datacron") or [], pp.player_meta(p),
                                       stat_calc, ctx)

    results = await asyncio.gather(*(one(c) for c in codes))
    return [r for r in results if r], [c for c, r in zip(codes, results) if not r]


# (подпись, функция значения, больше — лучше). None — нет данных.
def _g(*path):
    def get(p):
        value = p
        for key in path:
            value = value.get(key) if isinstance(value, dict) else None
        return value
    return get


SECTIONS = [
    ("Общее", [
        ("ГМ", _g("gp"), True),
        ("ГМ персонажей", _g("char_gp"), True),
        ("ГМ флота", _g("ship_gp"), True),
        ("Лучшие 80 по ГМ", _g("top80"), True),
        ("Арена: место", _g("meta", "arena_rank"), False),
        ("Флот: место", _g("meta", "fleet_rank"), False),
        ("Рейтинг ВА", _g("meta", "skill_rating"), True),
    ]),
    ("Прокачка", [
        ("G13", _g("g13"), True),
        ("G12", _g("g12"), True),
        ("Сумма реликвий", _g("relics", "total"), True),
        ("R10", _g("relics", "r10"), True),
        ("R9", _g("relics", "r9"), True),
        ("R8", _g("relics", "r8"), True),
        ("R7", _g("relics", "r7"), True),
        ("R5–R6", lambda p: (p["relics"].get("r5") or 0) + (p["relics"].get("r6") or 0), True),
        ("Зеты", _g("zetas"), True),
        ("Омикроны", _g("omicrons"), True),
        ("Омикроны ВГ", _g("omicrons_tw"), True),
        ("Омикроны ВИ", _g("omicrons_tb"), True),
        ("Омикроны ВА", _g("omicrons_gac"), True),
    ]),
    ("Модули", [
        ("Шеститочечные", _g("mods", "six_dot"), True),
        ("Скорость 25+", _g("mods", "s25"), True),
        ("Скорость 20+", _g("mods", "s20"), True),
        ("Скорость 15+", _g("mods", "s15"), True),
        ("Скорость 10+", _g("mods", "s10"), True),
        ("Ср. скорость от модулей G11+", _g("speed_g11"), True),
        ("Ср. скорость, лучшие 80", _g("speed_top80"), True),
        ("МодКу", _g("modku"), True),
        ("Нападение 6%+", _g("mods", "off6"), True),
        ("Защита 14%+", _g("mods", "def14"), True),
    ]),
    ("Датакроны", [
        ("9 уровня", _g("datacrons", "l9"), True),
        ("6–8 уровня", _g("datacrons", "l6_8"), True),
        ("3–5 уровня", _g("datacrons", "l3_5"), True),
        ("Перебросов", _g("datacrons", "rerolls"), True),
    ]),
]


def _fmt(value) -> str:
    if value is None:
        return "—"
    if isinstance(value, float):
        return f"{value:.2f}".rstrip("0").rstrip(".").replace(".", ",") if value < 100 else f"{value:,.0f}".replace(",", " ")
    return f"{value:,}".replace(",", " ")


def _best(values: list, higher: bool) -> set:
    known = [v for v in values if v is not None]
    if len(known) < 2 or len(set(known)) == 1:
        return set()
    target = max(known) if higher else min(known)
    return {i for i, v in enumerate(values) if v == target}


def _unit_cell(r: dict) -> tuple[str, float | None]:
    """(текст, число для «лучшего») — реликвия/гир/звёзды юнита."""
    if r.get("missing"):
        return "—", None
    if r["is_ship"]:
        return f"{r['stars']}★", r["stars"]
    if r["relic"] is not None:
        return f"R{r['relic']}" + (" ⚡" if r.get("ultimate") else ""), 100 + r["relic"] + (0.5 if r.get("ultimate") else 0)
    return (f"G{r['gear']}" if r["stars"] >= 7 else f"{r['stars']}★ G{r['gear']}"), r["gear"] + r["stars"] / 10


def table(players: list[dict]) -> list[dict]:
    """[{title, rows: [{label, cells: [{text, best}]}]}] — для шаблона."""
    out = []
    for title, metrics in SECTIONS:
        rows = []
        for label, get, higher in metrics:
            values = []
            for p in players:
                try:
                    values.append(get(p))
                except (KeyError, TypeError):
                    values.append(None)
            if all(v is None for v in values):
                continue
            best = _best(values, higher)
            rows.append({"label": label, "cells": [{"text": _fmt(v), "best": i in best} for i, v in enumerate(values)]})
        out.append({"title": title, "rows": rows})
    league = []
    for p in players:
        m = p["meta"]
        name = pp.LEAGUE_NAMES.get(m.get("league") or "", m.get("league") or "")
        league.append({"text": f"{name} {m['division']}".strip() if name and m.get("division") else (name or "—"), "best": False})
    out[0]["rows"].insert(6, {"label": "Лига ВА", "cells": league})
    for title, key in (("Галактические легенды", "gls"), ("Мета", "meta_units"), ("Флот", "fleet")):
        rows = []
        for idx, unit in enumerate(players[0][key] if players else []):
            cells = [_unit_cell(p[key][idx]) for p in players]
            if all(v is None for _t, v in cells):
                continue
            best = _best([v for _t, v in cells], True)
            rows.append({"label": unit["name"], "base_id": unit["base_id"],
                         "cells": [{"text": t, "best": i in best} for i, (t, _v) in enumerate(cells)]})
        if key == "gls":
            owned = [sum(not r.get("missing") for r in p["gls"]) for p in players]
            best = _best(owned, True)
            rows.insert(0, {"label": "Всего ГЛ", "cells": [{"text": str(n), "best": i in best} for i, n in enumerate(owned)]})
        out.append({"title": title, "rows": rows, "units": True})
    return [s for s in out if s["rows"]]
