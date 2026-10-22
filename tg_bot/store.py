"""Таблицы Telegram-бота. Отдельно от database.py: Telegram — самостоятельный слой
поверх общей БД, и его таблицы никто, кроме tg_bot (и ссылки «Привязать Telegram»
на сайте), не читает.

Идентичность. Всё в системе прав и регистрации завязано на строковый discord_id
(user_registration, bot_admins, manual_access_grants, личные задачи…). Telegram-игрок
получает там же «владельца» owner_id:
  * "tg:<telegram id>" — если он пользуется только Telegram;
  * настоящий discord_id — если связал Telegram со своим Discord (ссылка с сайта),
    тогда это один и тот же игрок с теми же привязками и правами.
Discord-ID — всегда цифры, поэтому префикс "tg:" с ними не пересекается."""

import secrets
import sqlite3
import time

import database

TG_PREFIX = "tg:"
LINK_TOKEN_TTL = 15 * 60


def _connect():
    conn = sqlite3.connect(database.DB_NAME)
    _ensure_tables(conn.cursor())
    return conn


def _ensure_tables(cursor):
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS tg_users (
            tg_user_id INTEGER PRIMARY KEY,
            owner_id TEXT NOT NULL,
            username TEXT,
            full_name TEXT,
            dm_open INTEGER NOT NULL DEFAULT 0,
            linked_at TEXT,
            updated_at TEXT NOT NULL
        )
    """)
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_tg_users_owner ON tg_users(owner_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_tg_users_username ON tg_users(username COLLATE NOCASE)")
    # Чаты (группы/каналы), привязанные к гильдии — куда слать рассылки гильдии.
    # Одна гильдия может иметь несколько чатов (общий + офицерский + канал).
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS tg_chats (
            chat_id INTEGER PRIMARY KEY,
            guild_id INTEGER NOT NULL,
            title TEXT,
            chat_type TEXT,
            bound_by TEXT,
            bound_at TEXT NOT NULL
        )
    """)
    # Одноразовые токены «связать Telegram с Discord» (создаёт сайт, гасит /start).
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS tg_link_tokens (
            token TEXT PRIMARY KEY,
            discord_id TEXT NOT NULL,
            expires_at REAL NOT NULL
        )
    """)


def tg_owner_id(tg_user_id: int) -> str:
    return f"{TG_PREFIX}{tg_user_id}"


def is_tg_owner(owner_id: str | None) -> bool:
    return bool(owner_id) and str(owner_id).startswith(TG_PREFIX)


def touch_user(tg_user: dict, dm_open: bool = False) -> str:
    """Запоминает (или обновляет имя) Telegram-пользователя и возвращает его owner_id.
    dm_open=True — пользователь писал боту в личку, значит бот может писать ему сам."""
    tg_id = int(tg_user["id"])
    username = tg_user.get("username")
    full_name = " ".join(p for p in (tg_user.get("first_name"), tg_user.get("last_name")) if p) or None
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    conn = _connect()
    cur = conn.cursor()
    cur.execute("SELECT owner_id FROM tg_users WHERE tg_user_id = ?", (tg_id,))
    row = cur.fetchone()
    if row:
        cur.execute(
            "UPDATE tg_users SET username = ?, full_name = ?, dm_open = MAX(dm_open, ?), updated_at = ? WHERE tg_user_id = ?",
            (username, full_name, 1 if dm_open else 0, now, tg_id),
        )
        owner_id = row[0]
    else:
        owner_id = tg_owner_id(tg_id)
        cur.execute(
            "INSERT INTO tg_users (tg_user_id, owner_id, username, full_name, dm_open, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
            (tg_id, owner_id, username, full_name, 1 if dm_open else 0, now),
        )
    conn.commit()
    conn.close()
    return owner_id


def get_owner_id(tg_user_id: int) -> str | None:
    conn = _connect()
    row = conn.execute("SELECT owner_id FROM tg_users WHERE tg_user_id = ?", (int(tg_user_id),)).fetchone()
    conn.close()
    return row[0] if row else None


def find_by_username(username: str) -> dict | None:
    username = username.lstrip("@")
    conn = _connect()
    row = conn.execute(
        "SELECT tg_user_id, owner_id, full_name FROM tg_users WHERE username = ? COLLATE NOCASE", (username,)
    ).fetchone()
    conn.close()
    return {"tg_user_id": row[0], "owner_id": row[1], "full_name": row[2]} if row else None


def tg_users_for_owner(owner_id: str) -> list[dict]:
    """Telegram-аккаунты игрока — для личных оповещений и подписи «@ник» в списках."""
    conn = _connect()
    rows = conn.execute(
        "SELECT tg_user_id, username, full_name, dm_open FROM tg_users WHERE owner_id = ?", (str(owner_id),)
    ).fetchall()
    conn.close()
    return [{"tg_user_id": r[0], "username": r[1], "full_name": r[2], "dm_open": bool(r[3])} for r in rows]


# ---- связь с Discord ----

def create_link_token(discord_id: str) -> str:
    """Токен для ссылки t.me/<бот>?start=link_<token> — создаёт сайт («Мой профиль»)."""
    token = secrets.token_urlsafe(16)
    conn = _connect()
    conn.execute("DELETE FROM tg_link_tokens WHERE expires_at < ? OR discord_id = ?", (time.time(), str(discord_id)))
    conn.execute(
        "INSERT INTO tg_link_tokens (token, discord_id, expires_at) VALUES (?, ?, ?)",
        (token, str(discord_id), time.time() + LINK_TOKEN_TTL),
    )
    conn.commit()
    conn.close()
    return token


def consume_link_token(token: str) -> str | None:
    conn = _connect()
    row = conn.execute(
        "SELECT discord_id FROM tg_link_tokens WHERE token = ? AND expires_at >= ?", (token, time.time())
    ).fetchone()
    conn.execute("DELETE FROM tg_link_tokens WHERE token = ?", (token,))
    conn.commit()
    conn.close()
    return row[0] if row else None


def link_to_discord(tg_user_id: int, discord_id: str) -> int:
    """Делает Telegram-аккаунт тем же игроком, что и discord_id. Привязки кодов союзника,
    сделанные раньше из Telegram (owner "tg:<id>"), переносятся на Discord-владельца,
    если такого кода у него ещё нет. Возвращает число перенесённых привязок."""
    old_owner = tg_owner_id(tg_user_id)
    discord_id = str(discord_id)
    conn = _connect()
    cur = conn.cursor()
    moved = 0
    try:
        cur.execute("SELECT guild_id, ally_code, ingame_name, is_main FROM user_registration WHERE discord_id = ?", (old_owner,))
        for guild_id, ally_code, name, is_main in cur.fetchall():
            cur.execute(
                "SELECT 1 FROM user_registration WHERE guild_id = ? AND ally_code = ? AND discord_id != ?",
                (guild_id, ally_code, old_owner),
            )
            if cur.fetchone():
                continue  # код уже у кого-то другого (вероятно, у самого Discord-аккаунта)
            cur.execute(
                "SELECT 1 FROM user_registration WHERE guild_id = ? AND discord_id = ? AND is_main = 1", (guild_id, discord_id)
            )
            keep_main = is_main and not cur.fetchone()
            cur.execute(
                "UPDATE user_registration SET discord_id = ?, is_main = ? WHERE guild_id = ? AND discord_id = ? AND ally_code = ?",
                (discord_id, 1 if keep_main else 0, guild_id, old_owner, ally_code),
            )
            moved += 1
        cur.execute("DELETE FROM user_registration WHERE discord_id = ?", (old_owner,))
    except sqlite3.OperationalError:
        pass  # user_registration ещё нет — переносить нечего
    cur.execute(
        "UPDATE tg_users SET owner_id = ?, linked_at = datetime('now') WHERE tg_user_id = ?", (discord_id, int(tg_user_id))
    )
    conn.commit()
    conn.close()
    return moved


def unlink_from_discord(tg_user_id: int):
    """Обратно в отдельного Telegram-игрока (привязки остаются у Discord-аккаунта)."""
    conn = _connect()
    conn.execute(
        "UPDATE tg_users SET owner_id = ?, linked_at = NULL WHERE tg_user_id = ?",
        (tg_owner_id(tg_user_id), int(tg_user_id)),
    )
    conn.commit()
    conn.close()


# ---- чаты гильдий ----

def bind_chat(chat_id: int, guild_id: int, title: str | None, chat_type: str | None, bound_by: str):
    conn = _connect()
    conn.execute(
        """INSERT INTO tg_chats (chat_id, guild_id, title, chat_type, bound_by, bound_at)
           VALUES (?, ?, ?, ?, ?, datetime('now'))
           ON CONFLICT(chat_id) DO UPDATE SET guild_id = excluded.guild_id, title = excluded.title,
               chat_type = excluded.chat_type, bound_by = excluded.bound_by, bound_at = excluded.bound_at""",
        (int(chat_id), int(guild_id), title, chat_type, bound_by),
    )
    conn.commit()
    conn.close()


def unbind_chat(chat_id: int):
    conn = _connect()
    conn.execute("DELETE FROM tg_chats WHERE chat_id = ?", (int(chat_id),))
    conn.commit()
    conn.close()


def chat_guild(chat_id: int) -> int | None:
    conn = _connect()
    row = conn.execute("SELECT guild_id FROM tg_chats WHERE chat_id = ?", (int(chat_id),)).fetchone()
    conn.close()
    return row[0] if row else None


def guild_chats(guild_id: int) -> list[dict]:
    conn = _connect()
    rows = conn.execute(
        "SELECT chat_id, title, chat_type FROM tg_chats WHERE guild_id = ? ORDER BY bound_at", (int(guild_id),)
    ).fetchall()
    conn.close()
    return [{"chat_id": r[0], "title": r[1], "chat_type": r[2]} for r in rows]


def update_chat_id(old_chat_id: int, new_chat_id: int):
    """Группа превратилась в супергруппу — у неё новый chat_id (migrate_to_chat_id)."""
    conn = _connect()
    conn.execute("UPDATE tg_chats SET chat_id = ? WHERE chat_id = ?", (int(new_chat_id), int(old_chat_id)))
    conn.commit()
    conn.close()
