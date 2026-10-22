"""Очередь сообщений для Telegram-бота. Telegram-бот — отдельный процесс, поэтому
Discord-бот (cogs/*) и сайт не шлют в Telegram сами, а кладут сообщение сюда, в общую
БД; tg_bot/notifier.py забирает очередь раз в несколько секунд.

Два вида адресатов:
  * группа гильдии — enqueue_guild(guild_id, kind, text): уходит во все привязанные к
    гильдии чаты, где офицер включил этот вид оповещений (/notify), иначе не ставится;
  * игрок лично — enqueue_dm(owner_id, text): если у владельца есть Telegram, который
    писал боту в личку (иначе бот не может начать разговор первым).

Текст — в Discord-разметке, как его уже собирают cogs; упоминание <@owner_id> (Discord
id или "tg:<id>") превращается в Telegram-упоминание игрока, если он есть в Telegram.
Модуль без зависимостей от остального tg_bot — его импортируют Discord-бот и сайт."""

import sqlite3
import time

import database

# Виды оповещений гильдии, которые можно включить для чата (/notify).
KINDS = {
    "raid_tickets": "Энка — напоминания до сброса и итог дня",
    "tasks": "Задачи — новые, напоминания о сроке, итоги",
    "omicrons": "Выданные омикроны",
}


def _connect():
    conn = sqlite3.connect(database.DB_NAME)
    ensure_tables(conn.cursor())
    return conn


def ensure_tables(cursor):
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS tg_outbox (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id INTEGER,
            kind TEXT,
            owner_id TEXT,
            text TEXT NOT NULL,
            created_at REAL NOT NULL,
            sent_at REAL,
            error TEXT
        )
    """)
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_tg_outbox_pending ON tg_outbox(sent_at)")
    # tg_chats создаёт tg_bot/store.py; колонка kinds — включённые виды оповещений чата.
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
    try:
        cursor.execute("ALTER TABLE tg_chats ADD COLUMN kinds TEXT NOT NULL DEFAULT ''")
    except sqlite3.OperationalError:
        pass  # колонка уже есть


def chats_for(guild_id: int, kind: str) -> list[int]:
    conn = _connect()
    rows = conn.execute("SELECT chat_id, kinds FROM tg_chats WHERE guild_id = ?", (int(guild_id),)).fetchall()
    conn.close()
    return [chat_id for chat_id, kinds in rows if kind in (kinds or "").split(",")]


def guild_wants(guild_id: int, kind: str) -> bool:
    return bool(chats_for(guild_id, kind))


def enqueue_guild(guild_id: int, kind: str, text: str) -> bool:
    if not guild_wants(guild_id, kind):
        return False
    conn = _connect()
    conn.execute(
        "INSERT INTO tg_outbox (guild_id, kind, text, created_at) VALUES (?, ?, ?, ?)",
        (int(guild_id), kind, text, time.time()),
    )
    conn.commit()
    conn.close()
    return True


def can_dm(owner_id: str) -> bool:
    conn = _connect()
    try:
        row = conn.execute("SELECT 1 FROM tg_users WHERE owner_id = ? AND dm_open = 1 LIMIT 1", (str(owner_id),)).fetchone()
    except sqlite3.OperationalError:
        row = None  # Telegram-бот ещё ни разу не запускался
    conn.close()
    return row is not None


def enqueue_dm(owner_id: str, text: str) -> bool:
    if not can_dm(owner_id):
        return False
    conn = _connect()
    conn.execute(
        "INSERT INTO tg_outbox (owner_id, kind, text, created_at) VALUES (?, 'dm', ?, ?)",
        (str(owner_id), text, time.time()),
    )
    conn.commit()
    conn.close()
    return True


def owner_for_ally(guild_id: int, ally_code: str) -> str | None:
    """Владелец кода союзника в гильдии — Discord id или "tg:<id>" (в отличие от
    database.get_discord_id_for_ally, которая отдаёт только Discord)."""
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT discord_id FROM user_registration WHERE guild_id = ? AND ally_code = ? ORDER BY is_main DESC LIMIT 1",
            (int(guild_id), str(ally_code)),
        ).fetchone()
    except sqlite3.OperationalError:
        row = None
    conn.close()
    return row[0] if row else None


def is_tg_owner(owner_id) -> bool:
    return str(owner_id or "").startswith("tg:")


# ---- для tg_bot/notifier.py ----

def pending(limit: int = 20) -> list[dict]:
    conn = _connect()
    rows = conn.execute(
        "SELECT id, guild_id, kind, owner_id, text, created_at FROM tg_outbox WHERE sent_at IS NULL ORDER BY id LIMIT ?",
        (limit,),
    ).fetchall()
    conn.close()
    return [dict(zip(("id", "guild_id", "kind", "owner_id", "text", "created_at"), r)) for r in rows]


def mark_sent(item_id: int, error: str | None = None):
    conn = _connect()
    conn.execute("UPDATE tg_outbox SET sent_at = ?, error = ? WHERE id = ?", (time.time(), error, item_id))
    # Историю держим неделю — для разбора «почему не пришло».
    conn.execute("DELETE FROM tg_outbox WHERE sent_at IS NOT NULL AND sent_at < ?", (time.time() - 7 * 86400,))
    conn.commit()
    conn.close()


def chat_kinds(chat_id: int) -> set[str]:
    conn = _connect()
    row = conn.execute("SELECT kinds FROM tg_chats WHERE chat_id = ?", (int(chat_id),)).fetchone()
    conn.close()
    return set((row[0] or "").split(",")) - {""} if row else set()


def set_chat_kinds(chat_id: int, kinds: set[str]):
    conn = _connect()
    conn.execute("UPDATE tg_chats SET kinds = ? WHERE chat_id = ?", (",".join(sorted(kinds)), int(chat_id)))
    conn.commit()
    conn.close()
