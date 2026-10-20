"""/вг_соперник — своя гильдия против другой (как HotBot `/compare guild` / C3PO
`tw enemy quality`): ГМ, реликвии, зеты и омикроны, моды, датакроны, ГЛ, мета и флот.
Обе стороны считаются одной и той же сводкой /профиль (services/player_profile.py):
своя — по кэшам ежечасного синка, соперник — свежими get_player по каждому участнику.

Текущего соперника по ВГ игра без входа в аккаунт не показывает (territoryWarStatus
пуст), поэтому соперник — по коду союзника любого его участника или из прошлых ВГ:
guild.recentTerritoryWarResult[].opponentGuildProfile (проверено вживую 2026-10-20),
их запоминает cogs/raids.py в том же цикле, что и рейды (тот же ответ get_guild)."""

import json
from datetime import datetime
from zoneinfo import ZoneInfo

import database

MSK = ZoneInfo("Europe/Moscow")
STATE_OPPONENTS = "tw_recent_opponents"


def recent_opponents(resp: dict) -> list[dict]:
    """[{id, name, gp, end, score, opponent_score}] — прошлые соперники по ВГ, новые первыми."""
    guild = resp.get("guild", resp) if isinstance(resp, dict) else {}
    out = []
    for tw in guild.get("recentTerritoryWarResult") or []:
        prof = tw.get("opponentGuildProfile") or {}
        if not prof.get("id"):
            continue
        out.append({
            "id": prof["id"],
            "name": prof.get("name") or prof["id"],
            "gp": int(prof.get("guildGalacticPower") or 0),
            "end": int(tw.get("endTimeSeconds") or 0),
            "score": int(tw.get("score") or 0),
            "opponent_score": int(tw.get("opponentScore") or 0),
        })
    out.sort(key=lambda o: -o["end"])
    return out


def save_recent_opponents(guild_id: int, resp: dict) -> None:
    opponents = recent_opponents(resp)
    if opponents:
        database.set_bot_state(STATE_OPPONENTS, json.dumps(opponents, ensure_ascii=False), guild_id=guild_id)


def load_recent_opponents(guild_id: int) -> list[dict]:
    raw = database.get_bot_state(STATE_OPPONENTS, guild_id=guild_id)
    try:
        return json.loads(raw) if raw else []
    except ValueError:
        return []


def opponent_label(o: dict) -> str:
    date = datetime.fromtimestamp(o["end"], MSK).strftime("%d.%m") if o.get("end") else ""
    result = "победа" if o["score"] > o["opponent_score"] else "поражение"
    return f"{o['name']} · {date} · {result} {o['score']}:{o['opponent_score']}"


# Строки сравнения: (подпись, путь в сводке summarize_guild, формат, больше = лучше;
# None — без подсветки: меньше ДК 6–8 бывает просто потому, что их докачали до 9).
SECTIONS = [
    ("Итого", [
        ("Участников", ("members",), "int", True),
        ("ГМ", ("gp",), "compact", True),
        ("ГМ персонажей", ("char_gp",), "compact", True),
        ("ГМ кораблей", ("ship_gp",), "compact", True),
        ("Средний ГМ", ("avg", "gp"), "compact", True),
        ("Средний топ-80", ("avg", "top80"), "compact", True),
    ]),
    ("Прокачка", [
        ("G13", ("g13",), "int", True),
        ("Уровней реликвий", ("relics", "total"), "int", True),
        ("R10", ("relics", "r10"), "int", True),
        ("R9", ("relics", "r9"), "int", True),
        ("R8", ("relics", "r8"), "int", True),
        ("R7", ("relics", "r7"), "int", True),
        ("Зет на игрока", ("avg", "zetas"), "dec", True),
        ("Омикроны", ("omicrons",), "int", True),
        ("Омикроны ВГ", ("omicrons_tw",), "int", True),
    ]),
    ("Моды и датакроны", [
        ("6 точек", ("mods", "six_dot"), "int", True),
        ("Скорость 25+", ("mods", "s25"), "int", True),
        ("Скорость 20+", ("mods", "s20"), "int", True),
        ("Скорость 15+", ("mods", "s15"), "int", True),
        ("Скор. модов G11+", ("avg", "speed_g11"), "plus", True),
        ("Датакроны ур. 9", ("datacrons", "l9"), "int", True),
        ("Датакроны ур. 6–8", ("datacrons", "l6_8"), "int", None),
    ]),
]


def value_at(summary: dict, path: tuple):
    v = summary
    for key in path:
        v = v.get(key) if isinstance(v, dict) else None
    return v


def unit_rows(a: dict, b: dict, key: str) -> list[dict]:
    """Объединённый список юнитов (ГЛ/мета/флот) двух гильдий: владельцы и R8+ / 7★ у каждой."""
    rows_a = {r["base_id"]: r for r in a.get(key) or []}
    rows_b = {r["base_id"]: r for r in b.get(key) or []}
    order = list(dict.fromkeys([r["base_id"] for r in a.get(key) or []] + [r["base_id"] for r in b.get(key) or []]))
    out = []
    for base_id in order:
        ra, rb = rows_a.get(base_id), rows_b.get(base_id)
        any_row = ra or rb
        out.append({"base_id": base_id, "name": any_row["name"], "is_ship": any_row["is_ship"], "a": ra, "b": rb})
    out.sort(key=lambda r: -max((r["a"] or {}).get("owners", 0), (r["b"] or {}).get("owners", 0)))
    return out
