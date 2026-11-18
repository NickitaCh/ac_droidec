"""/вг_соперник — своя гильдия против другой (как HotBot `/compare guild` / C3PO
`tw enemy quality`): ГМ, реликвии, зеты и омикроны, моды, датакроны, ГЛ, мета и флот.
Обе стороны считаются одной и той же сводкой /профиль (services/player_profile.py):
своя — по кэшам ежечасного синка, соперник — свежими get_player по каждому участнику.

Текущий соперник берётся из авторизованного снимка ВГ. Без снимка можно использовать
код союзника любого его участника или выбрать соперника из прошлых ВГ:
guild.recentTerritoryWarResult[].opponentGuildProfile (проверено вживую 2026-10-20),
их запоминает cogs/raids.py в том же цикле, что и рейды (тот же ответ get_guild)."""

import json
from datetime import datetime
import time
from zoneinfo import ZoneInfo

import database

MSK = ZoneInfo("Europe/Moscow")
STATE_OPPONENTS = "tw_recent_opponents"
STATE_CURRENT_OPPONENT = "tw_current_opponent"


def save_current_opponent(guild_id: int, home: dict, opponent: dict, event_id: str, end: float) -> None:
    expected = (database.get_guild_config(guild_id) or {}).get("swgoh_guild_id")
    if not expected or home.get("id") != expected or opponent.get("id") in (None, "", expected):
        return
    if event_id and end > time.time():
        database.set_bot_state(STATE_CURRENT_OPPONENT, json.dumps({
            "id": opponent["id"], "name": opponent.get("name") or opponent["id"],
            "event_id": event_id, "home_id": expected, "end": end,
        }, ensure_ascii=False), guild_id=guild_id)


def current_opponent(guild_id: int) -> dict | None:
    raw = database.get_bot_state(STATE_CURRENT_OPPONENT, guild_id=guild_id)
    if not raw:
        return None
    try:
        row = json.loads(raw)
    except ValueError:
        return None
    expected = (database.get_guild_config(guild_id) or {}).get("swgoh_guild_id")
    if expected and isinstance(row, dict) and row.get("home_id") == expected and row.get("id"):
        end = row.get("end")
        if isinstance(end, (int, float)) and end > time.time():
            return row
    return None


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
