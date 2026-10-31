"""Known forum topics. Bot API supplies topic events, not an enumeration of old topics."""
import re
import sqlite3

import database


def topic_id(value):
    return 0 if value in (None, 0, 1) else int(value)


def _connect():
    conn = sqlite3.connect(database.DB_NAME)
    conn.execute("""CREATE TABLE IF NOT EXISTS tg_forum_topics (
        chat_id INTEGER NOT NULL, thread_id INTEGER NOT NULL, title TEXT NOT NULL,
        closed INTEGER NOT NULL DEFAULT 0, updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY (chat_id, thread_id))""")
    return conn


def remember(chat_id, thread_id, title=None, closed=None):
    tid = topic_id(thread_id)
    conn = _connect()
    try:
        with conn:
            conn.execute("""INSERT INTO tg_forum_topics (chat_id, thread_id, title, closed)
                VALUES (?, ?, ?, ?) ON CONFLICT(chat_id,thread_id) DO UPDATE SET
                title=COALESCE(?,tg_forum_topics.title), closed=COALESCE(?,tg_forum_topics.closed),
                updated_at=CURRENT_TIMESTAMP""",
                (chat_id, tid, title or ("Общая тема" if not tid else f"Тема №{tid}"), int(bool(closed)), title, closed))
    finally:
        conn.close()


def observe(message):
    chat = message.get("chat") or {}
    if chat.get("type") not in ("group", "supergroup"):
        return
    created = message.get("forum_topic_created") or {}
    edited = message.get("forum_topic_edited") or {}
    replied = message.get("reply_to_message") or {}
    tid = message.get("message_thread_id") if message.get("is_topic_message") or created else None
    if created and tid is None:
        tid = message.get("message_id")
    title = created.get("name") or edited.get("name")
    if not title and topic_id(replied.get("message_id")) == topic_id(tid):
        title = (replied.get("forum_topic_created") or {}).get("name")
    closed = 1 if "forum_topic_closed" in message else 0 if "forum_topic_reopened" in message else None
    remember(chat["id"], tid, title, closed)


def known(chat_id):
    remember(chat_id, 0)
    conn = _connect()
    try:
        # Seed older /topic labels and /notify destinations without requiring new messages.
        with conn:
            for table, title in (("tg_topics", "title"), ("tg_routes", "NULL")):
                try:
                    rows = conn.execute(f"SELECT thread_id,{title} FROM {table} WHERE chat_id=?", (chat_id,)).fetchall()
                except sqlite3.OperationalError:
                    continue
                for tid, name in rows:
                    tid = topic_id(tid)
                    conn.execute("INSERT OR IGNORE INTO tg_forum_topics(chat_id,thread_id,title) VALUES (?,?,?)",
                                 (chat_id, tid, name or ("Общая тема" if not tid else f"Тема №{tid}")))
        return [dict(zip(("id", "title", "closed"), r)) for r in conn.execute(
            "SELECT thread_id,title,closed FROM tg_forum_topics WHERE chat_id=? ORDER BY thread_id=0 DESC,title COLLATE NOCASE", (chat_id,))]
    finally:
        conn.close()


def add_link(chat_id, link, title):
    match = re.fullmatch(r"https?://t\.me/c/(\d+)/(\d+)(?:/\d+)?/?(?:\?.*)?", link)
    if not match or int("-100" + match[1]) != int(chat_id):
        raise ValueError("Нужна ссылка на тему этой группы вида https://t.me/c/…/… и её название.")
    remember(chat_id, int(match[2]), title.strip()[:128])
