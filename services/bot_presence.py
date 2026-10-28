"""Подключены ли боты у гильдии — для супер-админа на /admin/guilds (2026-10-28).

Discord: есть ли бот на сервере гильдии (один запрос /users/@me/guilds на всю страницу)
и каналы из настроек, куда бот сейчас не может писать (services/channel_health.py).
Telegram: привязанные чаты (/bind, tg_bot/store.py) и сколько игроков гильдии
зарегистрированы только через Telegram (владелец "tg:<id>")."""

import sqlite3

import database
from services import channel_health, discord_rest


def _tg_chats() -> dict:
    """{guild_id: [название чата, ...]}."""
    from tg_bot import store
    conn = store._connect()
    rows = conn.execute("SELECT guild_id, title, chat_type FROM tg_chats ORDER BY bound_at").fetchall()
    conn.close()
    out: dict = {}
    for gid, title, chat_type in rows:
        out.setdefault(gid, []).append(title or chat_type or "чат")
    return out


def _tg_players() -> dict:
    """{guild_id: число игроков, зарегистрированных из Telegram}."""
    conn = sqlite3.connect(database.DB_NAME)
    try:
        rows = conn.execute(
            "SELECT guild_id, COUNT(DISTINCT discord_id) FROM user_registration WHERE discord_id LIKE 'tg:%' GROUP BY guild_id"
        ).fetchall()
    except sqlite3.OperationalError:
        rows = []
    conn.close()
    return dict(rows)


async def overview(guilds: list[dict]) -> tuple[dict, str | None]:
    """({guild_id: {discord_in, discord_name, bad_channels: [подпись], tg_chats: [...], tg_players}},
    ошибка запроса к Discord или None)."""
    error = None
    try:
        bot_guilds = await discord_rest.list_bot_guilds()
    except Exception as e:
        bot_guilds, error = None, f"Не удалось спросить Discord, где состоит бот: {e}"
    chats, tg_players = _tg_chats(), _tg_players()
    out = {}
    for g in guilds:
        did = str(g.get("discord_guild_id") or "")
        bad = []
        for field in channel_health.channel_fields():
            value = g.get(field)
            err = channel_health.get_error(g["id"], field) if value else None
            if err and err.get("channel_id") == str(value):
                bad.append(f"{channel_health._label(field)} — {err.get('reason')}")
        out[g["id"]] = {
            "discord_in": None if bot_guilds is None else (did in bot_guilds),
            "discord_name": (bot_guilds or {}).get(did),
            "bad_channels": bad,
            "tg_chats": chats.get(g["id"], []),
            "tg_players": tg_players.get(g["id"], 0),
        }
    return out, error
