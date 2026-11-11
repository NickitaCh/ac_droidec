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
from services import bot_profiles

# Виды оповещений гильдии. Куда уходит каждый — маршрут гильдии (tg_routes: один чат или
# ветка на вид, можно в другую группу — например, офицерскую), задаётся на сайте
# (/settings → Telegram) или /notify прямо в нужном чате/ветке.
# discord — поле guilds с каналом, куда то же уходит в Discord (сайт показывает его рядом);
# forwarded — в Telegram уходит копия поста бота в Discord (cogs/tg_relay.py::forward_sent),
# без канала Discord не уйдёт и в Telegram; officer — для офицеров (сайт помечает).
KIND_INFO = {
    "raid_tickets": {"label": "Энка — напоминания до сброса и итог дня", "discord": "raid_tickets_channel_id"},
    "tasks": {"label": "Задачи — новые, напоминания о сроке, итоги", "discord": "tasks_log_channel_id"},
    "omicrons": {"label": "Выданные омикроны", "discord": "omicron_channel_id"},
    "tb_order": {"label": "Ордер ВИ — этап (автопост в 20:00 и отправка с сайта)", "discord": "tb_plan_channel_id"},
    "tb_ping": {"label": "Тег на ротацию/взводы ВИ", "discord": "ping_channel_id", "forwarded": True},
    "birthdays": {"label": "Поздравления с днём рождения", "discord": "birthday_channel_id", "forwarded": True},
    "tb_report": {"label": "Автоотчёт ВИ и «кто что не сделал»", "discord": "officer_channel_id",
                  "forwarded": True, "officer": True},
}
KINDS = {kind: info["label"] for kind, info in KIND_INFO.items()}


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
    for column in ("chat_id INTEGER", "thread_id INTEGER", "attachments TEXT", "bot_profile TEXT NOT NULL DEFAULT 'test'"):
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
    try:
        cursor.execute("ALTER TABLE tg_relay_channels ADD COLUMN direction TEXT NOT NULL DEFAULT 'both'")
    except sqlite3.OperationalError:
        pass  # колонка уже есть
    # Куда уходит каждый вид оповещений гильдии: один чат (thread_id NULL — общий) или ветка.
    # enabled = 0 — место запомнено, но в Telegram не шлём (галочка на сайте снята).
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS tg_routes (
            guild_id INTEGER NOT NULL,
            kind TEXT NOT NULL,
            chat_id INTEGER NOT NULL,
            thread_id INTEGER,
            enabled INTEGER NOT NULL DEFAULT 1,
            set_by TEXT,
            set_at TEXT NOT NULL,
            PRIMARY KEY (guild_id, kind)
        )
    """)
    # Разовый перенос со старой схемы (tg_chats.kinds — виды, включённые /notify в чате).
    for chat_id, guild_id, kinds in cursor.execute(
            "SELECT chat_id, guild_id, kinds FROM tg_chats WHERE kinds != ''").fetchall():
        for kind in kinds.split(","):
            cursor.execute(
                "INSERT OR IGNORE INTO tg_routes (guild_id, kind, chat_id, thread_id, enabled, set_by, set_at)"
                " VALUES (?, ?, ?, NULL, 1, 'notify', datetime('now'))", (guild_id, kind, chat_id))
        cursor.execute("UPDATE tg_chats SET kinds = '' WHERE chat_id = ?", (chat_id,))


def guild_routes(guild_id: int) -> dict[str, dict]:
    """{kind: {chat_id, thread_id, enabled, bound}} — bound: чат всё ещё привязан к гильдии."""
    conn = _connect()
    rows = conn.execute(
        """SELECT r.kind, r.chat_id, r.thread_id, r.enabled, c.chat_id IS NOT NULL FROM tg_routes r
           LEFT JOIN tg_chats c ON c.chat_id = r.chat_id AND c.guild_id = r.guild_id
           WHERE r.guild_id = ?""",
        (int(guild_id),),
    ).fetchall()
    conn.close()
    return {r[0]: {"chat_id": r[1], "thread_id": r[2], "enabled": bool(r[3]), "bound": bool(r[4])} for r in rows}


def set_route(guild_id: int, kind: str, chat_id: int, thread_id: int | None, enabled: bool, set_by: str):
    conn = _connect()
    conn.execute(
        """INSERT INTO tg_routes (guild_id, kind, chat_id, thread_id, enabled, set_by, set_at)
           VALUES (?, ?, ?, ?, ?, ?, datetime('now'))
           ON CONFLICT(guild_id, kind) DO UPDATE SET chat_id = excluded.chat_id, thread_id = excluded.thread_id,
               enabled = excluded.enabled, set_by = excluded.set_by, set_at = excluded.set_at""",
        (int(guild_id), kind, int(chat_id), thread_id, int(bool(enabled)), set_by),
    )
    conn.commit()
    conn.close()


def set_route_enabled(guild_id: int, kind: str, enabled: bool):
    conn = _connect()
    conn.execute("UPDATE tg_routes SET enabled = ? WHERE guild_id = ? AND kind = ?",
                 (int(bool(enabled)), int(guild_id), kind))
    conn.commit()
    conn.close()


def chats_for(guild_id: int, kind: str) -> list[tuple[int, int | None]]:
    """Куда слать вид оповещений: [(chat_id, thread_id)] — пусто, если выключен или чат отвязан."""
    route = guild_routes(guild_id).get(kind)
    if not route or not route["enabled"] or not route["bound"]:
        return []
    return [(route["chat_id"], route["thread_id"])]


def guild_wants(guild_id: int, kind: str) -> bool:
    return bool(chats_for(guild_id, kind))


def enqueue_guild(guild_id: int, kind: str, text: str, attachments: list[dict] | None = None) -> bool:
    if not guild_wants(guild_id, kind):
        return False
    conn = _connect()
    conn.execute(
        "INSERT INTO tg_outbox (guild_id, kind, text, created_at, attachments) VALUES (?, ?, ?, ?, ?)",
        (int(guild_id), kind, text, time.time(),
         json.dumps(attachments, ensure_ascii=False) if attachments else None),
    )
    conn.commit()
    conn.close()
    return True


def destinations(guild_id: int) -> list[dict]:
    """Места, куда можно направить оповещение: общий чат каждой привязанной группы и известные боту темы. {value: "chat_id:thread_id", chat_id, thread_id, label}."""
    conn = _connect()
    chats = conn.execute("SELECT chat_id, title FROM tg_chats WHERE guild_id = ? ORDER BY bound_at",
                         (int(guild_id),)).fetchall()
    conn.close()
    from tg_bot import topics as forum_topics
    out = []
    for chat_id, title in chats:
        title = title or "чат"
        for topic in forum_topics.known(chat_id):
            tid = topic["id"] or None
            out.append({"value": f"{chat_id}:{tid or ''}", "chat_id": chat_id,
                        "thread_id": tid, "label": f"{title} / {topic['title']}" +
                        (" (закрыта)" if topic["closed"] else "")})
    return out


def parse_destination(value: str) -> tuple[int, int | None] | None:
    chat, _, thread = (value or "").partition(":")
    try:
        return int(chat), (int(thread) if thread else None)
    except ValueError:
        return None


def can_dm(owner_id: str, profile: str | None = None) -> bool:
    from tg_bot import store
    return any(u["dm_open"] for u in store.tg_users_for_owner(str(owner_id), profile=profile))


def enqueue_dm(owner_id: str, text: str, attachments: list[dict] | None = None, *, profile: str | None = None, guild_id: int | None = None) -> bool:
    profile = bot_profiles.validate(profile or (bot_profiles.guild_profile(guild_id) if guild_id is not None else bot_profiles.current_profile()))
    if not can_dm(owner_id, profile=profile):
        return False
    conn = _connect()
    conn.execute(
        "INSERT INTO tg_outbox (owner_id, kind, text, created_at, attachments, bot_profile, guild_id) VALUES (?, 'dm', ?, ?, ?, ?, ?)",
        (str(owner_id), text, time.time(), json.dumps(attachments) if attachments else None, profile, guild_id),
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

def pending(limit: int = 20, *, profile: str | None = None) -> list[dict]:
    profile = bot_profiles.validate(profile or bot_profiles.worker_profile() or bot_profiles.runtime_profile())
    # Initialize the guild schema before joining, including on a fresh install.
    database.get_all_guild_configs(active_only=False)
    conn = _connect()
    rows = conn.execute(
        "SELECT o.id, o.guild_id, o.kind, o.owner_id, o.text, o.created_at, o.chat_id, o.thread_id, o.attachments"
        " FROM tg_outbox o LEFT JOIN guilds g ON g.id=o.guild_id"
        " WHERE o.sent_at IS NULL AND COALESCE(g.bot_profile, o.bot_profile)=? ORDER BY o.id LIMIT ?",
        (profile, limit),
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


def route_here(route: dict | None, chat_id: int, thread_id: int | None) -> bool:
    """Включён ли вид оповещений именно в этот чат/ветку (для /notify)."""
    return bool(route and route["enabled"] and route["chat_id"] == chat_id
                and (route["thread_id"] or None) == (thread_id or None))


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


def discord_for_owner(owner_id) -> str | None:
    """Обратное к tg_owner_for: Discord id игрока для тега в Discord. Привязавший Telegram и так
    владелец с Discord id; у "tg:" ищем Discord-регистрацию с тем же кодом союзника."""
    owner_id = str(owner_id or "")
    if not owner_id:
        return None
    if not is_tg_owner(owner_id):
        return owner_id
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT DISTINCT r2.discord_id FROM user_registration r1"
            " JOIN user_registration r2 ON r2.ally_code = r1.ally_code"
            " WHERE r1.discord_id = ? AND r2.discord_id NOT LIKE 'tg:%' LIMIT 1",
            (owner_id,),
        ).fetchone()
    except sqlite3.OperationalError:
        row = None
    conn.close()
    return row[0] if row else None


def guild_owners(guild_id: int) -> list[str]:
    """Все зарегистрированные игроки гильдии (Discord id и "tg:<id>") — для @everyone/@here."""
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT discord_id FROM user_registration WHERE guild_id = ?"
            " GROUP BY discord_id ORDER BY MIN(rowid)",
            (int(guild_id),),
        ).fetchall()
    except sqlite3.OperationalError:
        rows = []
    conn.close()
    return [r[0] for r in rows]


# Направление зеркала: в обе стороны (по умолчанию, просьба 2026-10-29) или только в одну.
DIRECTIONS = {"both": "в обе стороны", "dc_tg": "Discord → Telegram", "tg_dc": "Telegram → Discord"}
TO_TG, TO_DISCORD = ("both", "dc_tg"), ("both", "tg_dc")


def relay_channels(guild_id: int | None = None) -> list[dict]:
    conn = _connect()
    sql = "SELECT id, guild_id, discord_channel_id, label, enabled, direction FROM tg_relay_channels"
    if guild_id is not None:
        rows = conn.execute(sql + " WHERE guild_id = ? ORDER BY id", (int(guild_id),)).fetchall()
    else:
        rows = conn.execute(sql + " ORDER BY id").fetchall()
    conn.close()
    return [{"id": r[0], "guild_id": r[1], "discord_channel_id": r[2], "label": r[3], "enabled": bool(r[4]),
             "direction": r[5] if r[5] in DIRECTIONS else "both"} for r in rows]


def add_relay_channel(guild_id: int, discord_channel_id: str, label: str, created_by: str, direction: str = "both"):
    direction = direction if direction in DIRECTIONS else "both"
    conn = _connect()
    conn.execute(
        """INSERT INTO tg_relay_channels (guild_id, discord_channel_id, label, enabled, created_by, created_at, direction)
           VALUES (?, ?, ?, 1, ?, datetime('now'), ?)
           ON CONFLICT(guild_id, discord_channel_id, label) DO UPDATE SET enabled = 1, direction = excluded.direction""",
        (int(guild_id), str(discord_channel_id), label, created_by, direction),
    )
    conn.commit()
    conn.close()


def set_relay_direction(guild_id: int, relay_id: int, direction: str):
    if direction not in DIRECTIONS:
        return
    conn = _connect()
    conn.execute("UPDATE tg_relay_channels SET direction = ? WHERE id = ? AND guild_id = ?",
                 (direction, int(relay_id), int(guild_id)))
    conn.commit()
    conn.close()


def tg_mirrors_for(chat_id: int, thread_id: int | None) -> list[tuple[int, str]]:
    """Куда в Discord копировать сообщение из этого чата/ветки Telegram: [(guild_id, discord_channel_id)]
    — включённые зеркала «в обе стороны» / «Telegram → Discord», чья метка указывает сюда."""
    conn = _connect()
    rows = conn.execute(
        """SELECT DISTINCT m.guild_id, m.discord_channel_id FROM tg_relay_channels m
           JOIN tg_topics t ON t.guild_id = m.guild_id AND t.label = m.label
           JOIN tg_chats c ON c.chat_id = t.chat_id AND c.guild_id = t.guild_id
           WHERE m.enabled = 1 AND m.direction IN ('both', 'tg_dc')
             AND t.chat_id = ? AND COALESCE(t.thread_id, 0) = COALESCE(?, 0)""",
        (int(chat_id), thread_id),
    ).fetchall()
    conn.close()
    return [(r[0], r[1]) for r in rows]


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


def delivery_error(error: str) -> str:
    """Explain both historical raw API errors and new delivery failures."""
    raw = error or ""
    low = raw.lower()
    if "topic_closed" in low:
        return "Тема в Telegram закрыта. Откройте тему или разрешите боту писать в закрытые темы (права администратора). Затем отправьте сообщение повторно."
    if "thread not found" in low or "topic_deleted" in low:
        return "Тема в Telegram удалена или не найдена. Выберите существующую тему в Настройках → Telegram и отправьте сообщение повторно."
    if any(x in low for x in ("chat not found", "channel_private", "bot was kicked", "not a member")):
        return "Бот не имеет доступа к группе или каналу Telegram. Добавьте нужного бота в чат, проверьте права и выбранное место в Настройках → Telegram. Затем отправьте сообщение повторно."
    if any(x in low for x in ("not enough rights", "chat_write_forbidden", "have no rights", "can't send")):
        return "Боту запрещено отправлять сообщения в Telegram. Разрешите отправку в настройках группы или канала и отправьте сообщение повторно."
    if "too many requests" in low:
        match = re.search(r"retry after (\d+)", low)
        wait = f" через {match.group(1)} сек." if match else " немного позже"
        return f"Telegram ограничил частоту отправки. Повторите отправку{wait}"
    if "устарело" in low:
        return "Сообщение не отправлено: оно ожидало в очереди больше часа. Отправьте его повторно."
    return "Не удалось доставить сообщение в Telegram: " + raw


def latest_delivery(guild_id: int, kind: str) -> dict | None:
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT id, created_at, sent_at, error FROM tg_outbox WHERE guild_id=? AND kind=? ORDER BY id DESC LIMIT 1",
            (guild_id, kind),
        ).fetchone()
    finally:
        conn.close()
    if not row:
        return None
    item_id, created, sent, error = row
    status = "pending" if sent is None else ("error" if error else "sent")
    message = delivery_error(error) if error else ("Ожидает отправки в Telegram…" if sent is None else "Доставлено в Telegram.")
    return {"id": item_id, "status": status, "message": message, "created_at": created}
