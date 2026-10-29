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
Третий адресат — ветка Telegram по метке (enqueue_relay): офицер пишет /topic вг в нужной
ветке группы, и Discord-команда /тг или зеркало канала (cogs/tg_relay.py, настраивается на
/settings → Telegram) кладут туда копию сообщения с вложениями.

Модуль без зависимостей от остального tg_bot — его импортируют Discord-бот и сайт."""

import json
import re
import sqlite3
import time

import database

# Виды оповещений гильдии, которые можно включить для чата (/notify).
KINDS = {
    "raid_tickets": "Энка — напоминания до сброса и итог дня",
    "tasks": "Задачи — новые, напоминания о сроке, итоги",
    "omicrons": "Выданные омикроны",
    "tb_order": "Ордер ВИ — этап (автопост в 20:00 и отправка с сайта)",
}


def _connect():
    conn = sqlite3.connect(database.DB_NAME)
    ensure_tables(conn.cursor())
    conn.commit()
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
    # Пересылка в ветку по метке: адресат задан прямо в строке очереди.
    for column in ("chat_id INTEGER", "thread_id INTEGER", "attachments TEXT"):
        try:
            cursor.execute(f"ALTER TABLE tg_outbox ADD COLUMN {column}")
        except sqlite3.OperationalError:
            pass
    # Метки веток гильдии (/topic вг в ветке группы); thread_id NULL — общий чат без веток.
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS tg_topics (
            guild_id INTEGER NOT NULL,
            label TEXT NOT NULL,
            chat_id INTEGER NOT NULL,
            thread_id INTEGER,
            title TEXT,
            set_by TEXT,
            set_at TEXT NOT NULL,
            PRIMARY KEY (guild_id, label)
        )
    """)
    # Зеркала: всё, что пишут в Discord-канале, уходит копией в ветку с этой меткой
    # (если у гильдии включён флаг tg_relay_auto и само зеркало не на паузе).
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS tg_relay_channels (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id INTEGER NOT NULL,
            discord_channel_id TEXT NOT NULL,
            label TEXT NOT NULL,
            enabled INTEGER NOT NULL DEFAULT 1,
            created_by TEXT,
            created_at TEXT NOT NULL,
            UNIQUE (guild_id, discord_channel_id, label)
        )
    """)


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
        "SELECT id, guild_id, kind, owner_id, text, created_at, chat_id, thread_id, attachments"
        " FROM tg_outbox WHERE sent_at IS NULL ORDER BY id LIMIT ?",
        (limit,),
    ).fetchall()
    conn.close()
    keys = ("id", "guild_id", "kind", "owner_id", "text", "created_at", "chat_id", "thread_id", "attachments")
    items = [dict(zip(keys, r)) for r in rows]
    for item in items:
        item["attachments"] = json.loads(item["attachments"]) if item["attachments"] else []
    return items


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


# ---- метки веток и пересылка (/topic, /тг, зеркала каналов) ----

LABEL_MAX = 20  # кириллица — 2 байта, а callback_data кнопок Telegram ≤ 64 байт


def normalize_label(raw: str | None) -> str:
    label = re.sub(r"\s+", " ", (raw or "").strip().lower().lstrip("#"))
    return label.replace("|", "")[:LABEL_MAX].strip()


def set_topic(guild_id: int, label: str, chat_id: int, thread_id: int | None, title: str | None, set_by: str):
    conn = _connect()
    conn.execute(
        """INSERT INTO tg_topics (guild_id, label, chat_id, thread_id, title, set_by, set_at)
           VALUES (?, ?, ?, ?, ?, ?, datetime('now'))
           ON CONFLICT(guild_id, label) DO UPDATE SET chat_id = excluded.chat_id, thread_id = excluded.thread_id,
               title = excluded.title, set_by = excluded.set_by, set_at = excluded.set_at""",
        (int(guild_id), label, int(chat_id), thread_id, title, set_by),
    )
    conn.commit()
    conn.close()


def remove_topic(guild_id: int, label: str) -> bool:
    conn = _connect()
    cur = conn.execute("DELETE FROM tg_topics WHERE guild_id = ? AND label = ?", (int(guild_id), label))
    conn.commit()
    conn.close()
    return cur.rowcount > 0


def guild_topics(guild_id: int) -> list[dict]:
    """Метки гильдии — только в чатах, которые всё ещё привязаны к ней (/bind)."""
    conn = _connect()
    rows = conn.execute(
        """SELECT t.label, t.chat_id, t.thread_id, t.title, c.title FROM tg_topics t
           JOIN tg_chats c ON c.chat_id = t.chat_id AND c.guild_id = t.guild_id
           WHERE t.guild_id = ? ORDER BY t.label""",
        (int(guild_id),),
    ).fetchall()
    conn.close()
    return [{"label": r[0], "chat_id": r[1], "thread_id": r[2], "topic_title": r[3], "chat_title": r[4]} for r in rows]


def topic(guild_id: int, label: str) -> dict | None:
    return next((t for t in guild_topics(guild_id) if t["label"] == label), None)


def topic_place(t: dict) -> str:
    """«Чат / ветка» — подпись метки в списках Discord и сайта."""
    chat = t.get("chat_title") or "чат"
    return f"{chat} / {t['topic_title']}" if t.get("thread_id") and t.get("topic_title") else chat


def enqueue_relay(guild_id: int, label: str, text: str, attachments: list[dict] | None = None) -> dict | None:
    """Копия сообщения в ветку с меткой. Возвращает метку (куда ушло) или None, если такой нет."""
    t = topic(guild_id, label)
    if t is None:
        return None
    conn = _connect()
    conn.execute(
        "INSERT INTO tg_outbox (guild_id, kind, text, created_at, chat_id, thread_id, attachments)"
        " VALUES (?, 'relay', ?, ?, ?, ?, ?)",
        (int(guild_id), text, time.time(), t["chat_id"], t["thread_id"],
         json.dumps(attachments, ensure_ascii=False) if attachments else None),
    )
    conn.commit()
    conn.close()
    return t


def has_tg(owner_id) -> bool:
    """Есть ли у владельца Telegram — тогда <@id> в тексте станет упоминанием в Telegram."""
    conn = _connect()
    try:
        row = conn.execute("SELECT 1 FROM tg_users WHERE owner_id = ? LIMIT 1", (str(owner_id),)).fetchone()
    except sqlite3.OperationalError:
        row = None  # Telegram-бот ещё ни разу не запускался
    conn.close()
    return row is not None


def tg_owner_for(discord_id) -> str | None:
    """Кого упомянуть в Telegram вместо игрока Discord: его самого, если Telegram привязан к
    профилю, иначе Telegram-владельца с тем же кодом союзника (зарегистрировался через бота
    отдельно, не привязав Discord). None — Telegram у игрока нет."""
    if has_tg(discord_id):
        return str(discord_id)
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT DISTINCT r2.discord_id FROM user_registration r1"
            " JOIN user_registration r2 ON r2.ally_code = r1.ally_code"
            " JOIN tg_users u ON u.owner_id = r2.discord_id"
            " WHERE r1.discord_id = ? AND r2.discord_id LIKE 'tg:%' LIMIT 1",
            (str(discord_id),),
        ).fetchone()
    except sqlite3.OperationalError:
        row = None
    conn.close()
    return row[0] if row else None


def guild_tg_owners(guild_id: int) -> list[str]:
    """Все игроки гильдии с Telegram — для @everyone/@here и роли участников."""
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT DISTINCT r.discord_id FROM user_registration r"
            " JOIN tg_users u ON u.owner_id = r.discord_id WHERE r.guild_id = ? ORDER BY r.discord_id",
            (int(guild_id),),
        ).fetchall()
    except sqlite3.OperationalError:
        rows = []
    conn.close()
    return [r[0] for r in rows]


def relay_channels(guild_id: int | None = None) -> list[dict]:
    conn = _connect()
    sql = "SELECT id, guild_id, discord_channel_id, label, enabled FROM tg_relay_channels"
    if guild_id is not None:
        rows = conn.execute(sql + " WHERE guild_id = ? ORDER BY id", (int(guild_id),)).fetchall()
    else:
        rows = conn.execute(sql + " ORDER BY id").fetchall()
    conn.close()
    return [{"id": r[0], "guild_id": r[1], "discord_channel_id": r[2], "label": r[3], "enabled": bool(r[4])}
            for r in rows]


def add_relay_channel(guild_id: int, discord_channel_id: str, label: str, created_by: str):
    conn = _connect()
    conn.execute(
        """INSERT INTO tg_relay_channels (guild_id, discord_channel_id, label, enabled, created_by, created_at)
           VALUES (?, ?, ?, 1, ?, datetime('now'))
           ON CONFLICT(guild_id, discord_channel_id, label) DO UPDATE SET enabled = 1""",
        (int(guild_id), str(discord_channel_id), label, created_by),
    )
    conn.commit()
    conn.close()


def set_relay_enabled(guild_id: int, relay_id: int, enabled: bool):
    conn = _connect()
    conn.execute("UPDATE tg_relay_channels SET enabled = ? WHERE id = ? AND guild_id = ?",
                 (int(enabled), int(relay_id), int(guild_id)))
    conn.commit()
    conn.close()


def remove_relay_channel(guild_id: int, relay_id: int):
    conn = _connect()
    conn.execute("DELETE FROM tg_relay_channels WHERE id = ? AND guild_id = ?", (int(relay_id), int(guild_id)))
    conn.commit()
    conn.close()
