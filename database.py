import hashlib
import json
import os
import sqlite3

DB_NAME = "guild_management.db"

# Раз во сколько часов фоново обновлять кэш полного ростера юнитов игроков (см.
# cogs/stat_requirements.py::player_units_sync_loop) — живёт здесь (не в main.py, как
# остальные operator-константы, см. CLAUDE.md), потому что веб-процесс (services/
# dashboard_data.py, оценка "автообновление через X" на /activity) тоже читает это
# значение и не поднимает bot/main.py вообще. main.py импортирует его же, чтобы не
# держать два независимых числа, которые могут разойтись при правке одного и не другого.
PLAYER_STATS_SYNC_HOURS = 1

def init_db():
    """Создает все таблицы в единой БД, если они еще не созданы"""
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()

    # 0. Реестр SWGOH-гильдий, обслуживаемых ботом (мультитенантность).
    #    identity/каналы/роли живут здесь, а не в main.py — их нужно редактировать
    #    без деплоя (через будущий веб-дашборд). См. CLAUDE.md.
    _ensure_guilds_table(cursor)

    # 1. Таблица маппинга пользователей (Discord <-> SWGOH). guild_id — какой
    #    гильдии принадлежит эта строка (роster-кэш ведётся отдельно на гильдию).
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS user_mapping (
            guild_id INTEGER NOT NULL DEFAULT 1,
            discord_id TEXT NOT NULL,
            ally_code TEXT NOT NULL,
            ingame_name TEXT,
            PRIMARY KEY (guild_id, discord_id),
            UNIQUE (guild_id, ally_code)
        )
    """)
    _migrate_user_mapping_guild_id(cursor)
    try:
        cursor.execute("ALTER TABLE user_mapping ADD COLUMN member_level INTEGER")
    except sqlite3.OperationalError:
        pass  # колонка уже добавлена ранее
    try:
        cursor.execute("ALTER TABLE user_mapping ADD COLUMN comlink_player_id TEXT")
    except sqlite3.OperationalError:
        pass  # колонка уже добавлена ранее

    # 2. Таблица нарушений
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS position_warns (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ally_code TEXT NOT NULL,
            category TEXT NOT NULL,
            subcategory TEXT NOT NULL,
            date_str TEXT NOT NULL,
            comment TEXT,
            FOREIGN KEY (ally_code) REFERENCES user_mapping(ally_code)
        )
    """)
    try:
        cursor.execute("ALTER TABLE position_warns ADD COLUMN guild_id INTEGER NOT NULL DEFAULT 1")
    except sqlite3.OperationalError:
        pass  # колонка уже добавлена ранее
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_warns_guild_ally ON position_warns(guild_id, ally_code)")

    # 3. Таблица задач на прокачку
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS tasks (
            task_id INTEGER PRIMARY KEY AUTOINCREMENT,
            ally_code TEXT NOT NULL,
            base_id TEXT NOT NULL,
            target_type TEXT NOT NULL,
            target_value TEXT NOT NULL,
            deadline TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'ACTIVE',
            created_by TEXT,
            date_created TEXT,
            FOREIGN KEY (ally_code) REFERENCES user_mapping(ally_code),
            FOREIGN KEY (base_id) REFERENCES game_units(base_id)
        )
    """)
    try:
        cursor.execute("ALTER TABLE tasks ADD COLUMN guild_id INTEGER NOT NULL DEFAULT 1")
    except sqlite3.OperationalError:
        pass  # колонка уже добавлена ранее
    try:
        # Метка группы задач, поставленных одним действием (массовая постановка в вебе
        # либо "поставить задачи" из гильдийского отчёта по плейту) — позволяет отменить
        # всю группу разом. NULL у задач, поставленных по одной (/задания добавить).
        cursor.execute("ALTER TABLE tasks ADD COLUMN batch_id TEXT")
    except sqlite3.OperationalError:
        pass  # колонка уже добавлена ранее
    try:
        # Когда бот уже отправил напоминание о приближающемся дедлайне — чтобы не
        # слать его повторно на каждом часовом проходе аудита (см. tasks_reminder_loop).
        cursor.execute("ALTER TABLE tasks ADD COLUMN reminder_sent_at TEXT")
    except sqlite3.OperationalError:
        pass  # колонка уже добавлена ранее

    # 4. Справочник игровых юнитов (Персонажи и Корабли) — глобальный, общий
    #    для всех гильдий (игровой каталог SWGOH, не привязан к guild_id).
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS game_units (
            base_id TEXT PRIMARY KEY,
            cached_name TEXT NOT NULL,
            unit_type TEXT NOT NULL DEFAULT 'character'
        )
    """)
    try:
        # Английское имя — только для поиска (data/mod-builder ищет персонажа и на
        # английской раскладке), отображаемое имя везде остаётся cached_name (RUS_RU).
        cursor.execute("ALTER TABLE game_units ADD COLUMN cached_name_en TEXT")
    except sqlite3.OperationalError:
        pass  # колонка уже добавлена ранее
    try:
        # Есть ли у юнита В ИГРЕ хотя бы одна способность на омикрон-тире (skill.tier[].
        # isOmicronTier из Comlink SkillDefinitions) — не зависит от того, разблокировал
        # ли её кто-то в гильдии. Обновляется целиком в services/units_sync.py::sync_units
        # (database.set_omicron_capable_base_ids), на том же цикле, что и весь справочник.
        cursor.execute("ALTER TABLE game_units ADD COLUMN has_omicron INTEGER NOT NULL DEFAULT 0")
    except sqlite3.OperationalError:
        pass  # колонка уже добавлена ранее

    # Создаем индексы для быстродействия
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_warns_ally ON position_warns(ally_code)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_tasks_ally ON tasks(ally_code)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_tasks_guild_status ON tasks(guild_id, status)")

    conn.commit()
    conn.close()
    print("📋 [БД] Инициализация структуры базы данных успешно завершена.")


def _migrate_user_mapping_guild_id(cursor):
    """Миграция с версии таблицы до мультитенантности (PRIMARY KEY только по
    discord_id, без guild_id) — переносим существующие строки в гильдию id=1."""
    cursor.execute("PRAGMA table_info(user_mapping)")
    cols = {row[1] for row in cursor.fetchall()}
    if not cols or "guild_id" in cols:
        return
    cursor.execute("ALTER TABLE user_mapping RENAME TO user_mapping_old")
    cursor.execute("""
        CREATE TABLE user_mapping (
            guild_id INTEGER NOT NULL DEFAULT 1,
            discord_id TEXT NOT NULL,
            ally_code TEXT NOT NULL,
            ingame_name TEXT,
            PRIMARY KEY (guild_id, discord_id),
            UNIQUE (guild_id, ally_code)
        )
    """)
    cursor.execute("""
        INSERT INTO user_mapping (guild_id, discord_id, ally_code, ingame_name)
        SELECT 1, discord_id, ally_code, ingame_name FROM user_mapping_old
    """)
    cursor.execute("DROP TABLE user_mapping_old")
    # Коммитим внутри миграции, а не полагаемся на вызывающую функцию — см. подробное
    # объяснение (и то, чем это грозит) в _ensure_user_registration_table.
    cursor.connection.commit()

# =====================================================================
# РЕЕСТР ГИЛЬДИЙ (мультитенантность): какие SWGOH-гильдии обслуживает бот,
# и их Discord-конфиг (каналы/роли/расписания). Раньше это были одноимённые
# константы в main.py (ALLY_CODE, TB_PLAN_CHANNEL_ID и т.д.) — теперь одна
# строка на гильдию, редактируемая без деплоя. main.py на старте сидирует
# сюда текущую гильдию (AC) через seed_default_guild, если таблица пуста.
# =====================================================================
GUILD_CONFIG_COLUMNS = [
    "name", "ally_code", "swgoh_guild_id", "discord_guild_id",
    "member_role_id", "officer_role_id",
    "ping_channel_id", "ping_role_id", "ping_start_date", "ping_schedule_json",
    "birthday_channel_id", "birthday_role_id",
    "officer_channel_id",
    "tb_plan_channel_id", "tb_order_source_channel_id", "tb_order_role_id",
    "tb_active_plan_id",
    "swgoh_gg_guild_id",
    "omicron_channel_id",
    "tw_guide_forum_channel_id",
    "antispam_enabled", "antispam_alert_channel_id", "antispam_alert_role_id",
    "antispam_alert_message", "antispam_timeout_minutes",
    "tasks_log_channel_id",
    "is_active",
]


def _ensure_guilds_table(cursor):
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS guilds (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            ally_code TEXT NOT NULL,
            swgoh_guild_id TEXT,
            discord_guild_id TEXT NOT NULL,
            member_role_id TEXT NOT NULL,
            officer_role_id TEXT NOT NULL,
            ping_channel_id TEXT,
            ping_role_id TEXT,
            ping_start_date TEXT,
            ping_schedule_json TEXT,
            birthday_channel_id TEXT,
            birthday_role_id TEXT,
            officer_channel_id TEXT,
            tb_plan_channel_id TEXT,
            tb_order_source_channel_id TEXT,
            tb_order_role_id TEXT,
            is_active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL
        )
    """)
    try:
        cursor.execute("ALTER TABLE guilds ADD COLUMN swgoh_gg_guild_id TEXT")
    except sqlite3.OperationalError:
        pass  # колонка уже добавлена ранее
    try:
        cursor.execute("ALTER TABLE guilds ADD COLUMN omicron_channel_id TEXT")
    except sqlite3.OperationalError:
        pass  # колонка уже добавлена ранее
    try:
        cursor.execute("ALTER TABLE guilds ADD COLUMN tw_guide_forum_channel_id TEXT")
    except sqlite3.OperationalError:
        pass  # колонка уже добавлена ранее
    try:
        cursor.execute("ALTER TABLE guilds ADD COLUMN tb_active_plan_id INTEGER")
    except sqlite3.OperationalError:
        pass  # колонка уже добавлена ранее
    try:
        cursor.execute("ALTER TABLE guilds ADD COLUMN antispam_alert_channel_id TEXT")
    except sqlite3.OperationalError:
        pass  # колонка уже добавлена ранее
    try:
        cursor.execute("ALTER TABLE guilds ADD COLUMN antispam_timeout_minutes INTEGER")
    except sqlite3.OperationalError:
        pass  # колонка уже добавлена ранее
    try:
        cursor.execute("ALTER TABLE guilds ADD COLUMN antispam_enabled INTEGER NOT NULL DEFAULT 0")
    except sqlite3.OperationalError:
        pass  # колонка уже добавлена ранее
    try:
        cursor.execute("ALTER TABLE guilds ADD COLUMN antispam_alert_role_id TEXT")
    except sqlite3.OperationalError:
        pass  # колонка уже добавлена ранее
    try:
        cursor.execute("ALTER TABLE guilds ADD COLUMN antispam_alert_message TEXT")
    except sqlite3.OperationalError:
        pass  # колонка уже добавлена ранее
    try:
        cursor.execute("ALTER TABLE guilds ADD COLUMN tasks_log_channel_id TEXT")
    except sqlite3.OperationalError:
        pass  # колонка уже добавлена ранее


def _row_to_guild_dict(row, columns):
    return {columns[i]: row[i] for i in range(len(columns))}


def get_all_guild_configs(active_only: bool = True) -> list:
    """Возвращает все зарегистрированные гильдии как список dict (включая id)."""
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    _ensure_guilds_table(cursor)
    query = "SELECT * FROM guilds"
    if active_only:
        query += " WHERE is_active = 1"
    cursor.execute(query)
    columns = [d[0] for d in cursor.description]
    rows = cursor.fetchall()
    conn.close()
    return [_row_to_guild_dict(r, columns) for r in rows]


def get_guild_config(guild_id: int) -> dict | None:
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    _ensure_guilds_table(cursor)
    cursor.execute("SELECT * FROM guilds WHERE id = ?", (guild_id,))
    columns = [d[0] for d in cursor.description]
    row = cursor.fetchone()
    conn.close()
    return _row_to_guild_dict(row, columns) if row else None


def get_guild_config_by_swgoh_id(swgoh_guild_id: str) -> dict | None:
    """Резолв внутреннего guild_id по игровому ID гильдии (Comlink) — используется
    и самовосстановлением swgoh_guild_id (ViolationsCog.update_roster_cache), и
    регистрацией (services/registration.py), чтобы определить гильдию по ally_code
    без Discord-ролей."""
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    _ensure_guilds_table(cursor)
    cursor.execute("SELECT * FROM guilds WHERE swgoh_guild_id = ? AND is_active = 1", (swgoh_guild_id,))
    columns = [d[0] for d in cursor.description]
    row = cursor.fetchone()
    conn.close()
    return _row_to_guild_dict(row, columns) if row else None


def create_guild(**fields) -> int:
    """fields — любое подмножество GUILD_CONFIG_COLUMNS. name/ally_code/discord_guild_id/
    member_role_id/officer_role_id обязательны (NOT NULL в схеме)."""
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    _ensure_guilds_table(cursor)
    cols = [c for c in GUILD_CONFIG_COLUMNS if c in fields]
    placeholders = ", ".join("?" for _ in cols)
    cursor.execute(
        f"INSERT INTO guilds ({', '.join(cols)}, created_at) VALUES ({placeholders}, datetime('now'))",
        tuple(fields[c] for c in cols)
    )
    conn.commit()
    new_id = cursor.lastrowid
    conn.close()
    return new_id


def update_guild_config(guild_id: int, **fields) -> bool:
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    _ensure_guilds_table(cursor)
    cols = [c for c in GUILD_CONFIG_COLUMNS if c in fields]
    if not cols:
        conn.close()
        return False
    set_clause = ", ".join(f"{c} = ?" for c in cols)
    cursor.execute(
        f"UPDATE guilds SET {set_clause} WHERE id = ?",
        tuple(fields[c] for c in cols) + (guild_id,)
    )
    conn.commit()
    updated = cursor.rowcount > 0
    conn.close()
    return updated


def seed_default_guild(**fields) -> int | None:
    """Идемпотентный сид: если в guilds ещё нет ни одной строки — создаёт первую
    (текущая единственная гильдия, id=1) из переданных main.py-констант. Если
    таблица уже не пуста — ничего не делает и возвращает None."""
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    _ensure_guilds_table(cursor)
    cursor.execute("SELECT COUNT(*) FROM guilds")
    count = cursor.fetchone()[0]
    conn.close()
    if count > 0:
        return None
    return create_guild(**fields)

# =====================================================================
# ЖУРНАЛ СРАБАТЫВАНИЙ АНТИСПАМ-ДЕТЕКТОРА (cogs/antispam.py) — переживший
# рестарт бота лог: кто попал в кросс-постинг-детектор, сколько сообщений
# удалено, выдан ли тайм-аут и на сколько. Сам детектор трекает "кто сейчас
# постит одинаковое" только в памяти процесса (см. docstring antispam.py) —
# это отдельная, постоянная история для команды /настройки антиспам_история.
# =====================================================================
def _ensure_antispam_log_table(cursor):
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS antispam_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id INTEGER NOT NULL,
            discord_user_id TEXT NOT NULL,
            channels TEXT,
            messages_deleted INTEGER NOT NULL DEFAULT 0,
            messages_delete_failed INTEGER NOT NULL DEFAULT 0,
            timeout_minutes INTEGER,
            timeout_applied INTEGER NOT NULL DEFAULT 0,
            content_preview TEXT,
            created_at TEXT NOT NULL
        )
    """)


def log_antispam_incident(
    guild_id: int, discord_user_id, channels: str,
    messages_deleted: int, messages_delete_failed: int,
    timeout_minutes: int | None, timeout_applied: bool, content_preview: str,
) -> None:
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    _ensure_antispam_log_table(cursor)
    cursor.execute("""
        INSERT INTO antispam_log (
            guild_id, discord_user_id, channels, messages_deleted, messages_delete_failed,
            timeout_minutes, timeout_applied, content_preview, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
    """, (
        guild_id, str(discord_user_id), channels, messages_deleted, messages_delete_failed,
        timeout_minutes, int(timeout_applied), content_preview,
    ))
    conn.commit()
    conn.close()


def get_antispam_log(guild_id: int, limit: int = 20) -> list:
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    _ensure_antispam_log_table(cursor)
    cursor.execute("""
        SELECT discord_user_id, channels, messages_deleted, messages_delete_failed,
               timeout_minutes, timeout_applied, content_preview, created_at
        FROM antispam_log
        WHERE guild_id = ?
        ORDER BY id DESC
        LIMIT ?
    """, (guild_id, limit))
    rows = cursor.fetchall()
    conn.close()
    return [
        {
            "discord_user_id": r[0], "channels": r[1], "messages_deleted": r[2],
            "messages_delete_failed": r[3], "timeout_minutes": r[4],
            "timeout_applied": bool(r[5]), "content_preview": r[6], "created_at": r[7],
        }
        for r in rows
    ]

# =====================================================================
# СУПЕР-АДМИНЫ БОТА: полный доступ ко всем гильдиям + управление гильдиями/
# грантами (см. guild_resolver.resolve_access, cogs/admin_management.py).
# Засеивается текущим владельцем бота (main.py::ALLOWED_USER_IDS[0]) при
# старте — см. seed_bot_admin, вызывается из main.py::on_ready.
# =====================================================================
def _ensure_bot_admins_table(cursor):
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS bot_admins (
            discord_id TEXT PRIMARY KEY,
            username TEXT,
            added_by TEXT,
            added_at TEXT NOT NULL
        )
    """)


def add_bot_admin(discord_id: str, username: str = None, added_by: str = None) -> bool:
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    _ensure_bot_admins_table(cursor)
    cursor.execute(
        "INSERT OR IGNORE INTO bot_admins (discord_id, username, added_by, added_at) VALUES (?, ?, ?, datetime('now'))",
        (str(discord_id), username, added_by)
    )
    conn.commit()
    added = cursor.rowcount > 0
    conn.close()
    return added


def remove_bot_admin(discord_id: str) -> bool:
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    _ensure_bot_admins_table(cursor)
    cursor.execute("DELETE FROM bot_admins WHERE discord_id = ?", (str(discord_id),))
    conn.commit()
    removed = cursor.rowcount > 0
    conn.close()
    return removed


def is_bot_admin(discord_id: str) -> bool:
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    _ensure_bot_admins_table(cursor)
    cursor.execute("SELECT 1 FROM bot_admins WHERE discord_id = ?", (str(discord_id),))
    row = cursor.fetchone()
    conn.close()
    return row is not None


def get_all_bot_admins() -> list:
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    _ensure_bot_admins_table(cursor)
    cursor.execute("SELECT discord_id, username, added_by, added_at FROM bot_admins ORDER BY added_at")
    rows = cursor.fetchall()
    conn.close()
    return [
        {"discord_id": r[0], "username": r[1], "added_by": r[2], "added_at": r[3]}
        for r in rows
    ]


def seed_bot_admin(discord_id: str, username: str = None, added_by: str = None) -> bool:
    """Идемпотентный сид (INSERT OR IGNORE) — безопасно звать на каждом старте,
    как seed_default_guild."""
    return add_bot_admin(discord_id, username=username, added_by=added_by)

# =====================================================================
# ЛОГ ВХОДОВ В ВЕБ-ДАШБОРД: пишется на каждый успешный OAuth-коллбэк
# (web/auth.py::callback), сразу после guild_resolver.resolve_access —
# видно и кто зашёл с доступом (tier=officer/member), и кто зашёл без
# доступа (tier=None) — попытка входа без прав. Страница только для
# супер-админов (web/routes/admin.py, /admin/access-log).
# =====================================================================
def _ensure_web_access_log_table(cursor):
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS web_access_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            discord_id TEXT NOT NULL,
            username TEXT,
            guild_id INTEGER,
            tier TEXT,
            is_super_admin INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL
        )
    """)


def log_web_access(discord_id: str, username: str, guild_id: int | None, tier: str | None, is_super_admin: bool) -> None:
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    _ensure_web_access_log_table(cursor)
    cursor.execute("""
        INSERT INTO web_access_log (discord_id, username, guild_id, tier, is_super_admin, created_at)
        VALUES (?, ?, ?, ?, ?, datetime('now'))
    """, (str(discord_id), username, guild_id, tier, int(is_super_admin)))
    conn.commit()
    conn.close()


def get_web_access_log(limit: int = 200) -> list:
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    _ensure_web_access_log_table(cursor)
    cursor.execute("""
        SELECT l.discord_id, l.username, l.guild_id, g.name, l.tier, l.is_super_admin, l.created_at
        FROM web_access_log l
        LEFT JOIN guilds g ON g.id = l.guild_id
        ORDER BY l.id DESC
        LIMIT ?
    """, (limit,))
    rows = cursor.fetchall()
    conn.close()
    return [
        {
            "discord_id": r[0], "username": r[1], "guild_id": r[2], "guild_name": r[3],
            "tier": r[4], "is_super_admin": bool(r[5]), "created_at": r[6],
        }
        for r in rows
    ]

# =====================================================================
# ЛОГИН/ПАРОЛЬ ДЛЯ ВЕБ-ДАШБОРДА: второй способ входа рядом с Discord OAuth
# (web/routes/password_auth.py) — заведён для офицеров, которым Discord
# недоступен (блокировки РФ). Учётка ВСЕГДА привязана к discord_id — права
# (tier/is_super_admin) после успешного входа резолвятся так же, как при
# OAuth, через guild_resolver.resolve_access(discord_id), а не хранятся тут.
# Заводится/меняется/удаляется только супер-админом (web/routes/admin.py,
# /admin/web-accounts) — самостоятельной регистрации логин/паролем нет.
# Пароль хранится как PBKDF2-HMAC-SHA256 (соль per-запись, 200k итераций) —
# в requirements.txt нет отдельной hashing-библиотеки, обходимся stdlib.
# =====================================================================
def _ensure_web_credentials_table(cursor):
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS web_credentials (
            login TEXT PRIMARY KEY,
            discord_id TEXT NOT NULL,
            password_hash TEXT NOT NULL,
            password_salt TEXT NOT NULL,
            created_by TEXT,
            created_at TEXT NOT NULL
        )
    """)


def _hash_password(password: str, salt_hex: str) -> str:
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt_hex), 200_000).hex()


def create_web_credential(login: str, discord_id: str, password: str, created_by: str = None) -> bool:
    """False, если такой логин уже занят (используйте set_web_credential_password для смены пароля)."""
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    _ensure_web_credentials_table(cursor)
    salt_hex = os.urandom(16).hex()
    password_hash = _hash_password(password, salt_hex)
    try:
        cursor.execute(
            "INSERT INTO web_credentials (login, discord_id, password_hash, password_salt, created_by, created_at) "
            "VALUES (?, ?, ?, ?, ?, datetime('now'))",
            (login, str(discord_id), password_hash, salt_hex, created_by),
        )
        conn.commit()
        return True
    except sqlite3.IntegrityError:
        return False
    finally:
        conn.close()


def set_web_credential_password(login: str, password: str) -> bool:
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    _ensure_web_credentials_table(cursor)
    salt_hex = os.urandom(16).hex()
    password_hash = _hash_password(password, salt_hex)
    cursor.execute(
        "UPDATE web_credentials SET password_hash = ?, password_salt = ? WHERE login = ?",
        (password_hash, salt_hex, login),
    )
    conn.commit()
    updated = cursor.rowcount > 0
    conn.close()
    return updated


def delete_web_credential(login: str) -> bool:
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    _ensure_web_credentials_table(cursor)
    cursor.execute("DELETE FROM web_credentials WHERE login = ?", (login,))
    conn.commit()
    removed = cursor.rowcount > 0
    conn.close()
    return removed


def get_all_web_credentials() -> list:
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    _ensure_web_credentials_table(cursor)
    cursor.execute("SELECT login, discord_id, created_by, created_at FROM web_credentials ORDER BY created_at")
    rows = cursor.fetchall()
    conn.close()
    return [
        {"login": r[0], "discord_id": r[1], "created_by": r[2], "created_at": r[3]}
        for r in rows
    ]


def verify_web_credential(login: str, password: str) -> str | None:
    """Возвращает discord_id при верных логине/пароле, иначе None."""
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    _ensure_web_credentials_table(cursor)
    cursor.execute("SELECT discord_id, password_hash, password_salt FROM web_credentials WHERE login = ?", (login,))
    row = cursor.fetchone()
    conn.close()
    if row is None:
        return None
    discord_id, password_hash, salt_hex = row
    if _hash_password(password, salt_hex) != password_hash:
        return None
    return discord_id

# =====================================================================
# СЧЁТЧИК ИСПОЛЬЗОВАНИЯ КОМАНД: инкрементится на каждое успешное выполнение
# слэш-команды (main.py::on_slash_command_completion) по "чистому" qualified_name
# (группа+сабкоманда, без параметров вызова — параметры не различаются).
# Просмотр — только для супер-админов (web/routes/admin.py, /admin/command-usage),
# полный список команд для страницы берётся из command_catalog.py, а не отсюда —
# эта таблица содержит только те команды, что реально хоть раз вызывались.
# =====================================================================
def _ensure_command_usage_table(cursor):
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS command_usage (
            command_name TEXT PRIMARY KEY,
            usage_count INTEGER NOT NULL DEFAULT 0,
            last_used_at TEXT
        )
    """)


def log_command_usage(command_name: str) -> None:
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    _ensure_command_usage_table(cursor)
    cursor.execute("""
        INSERT INTO command_usage (command_name, usage_count, last_used_at)
        VALUES (?, 1, datetime('now'))
        ON CONFLICT(command_name) DO UPDATE SET
            usage_count = usage_count + 1,
            last_used_at = excluded.last_used_at
    """, (command_name,))
    conn.commit()
    conn.close()


def get_command_usage_counts() -> dict:
    """{command_name: {"count": int, "last_used_at": str}} — только когда-либо вызывавшиеся команды."""
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    _ensure_command_usage_table(cursor)
    cursor.execute("SELECT command_name, usage_count, last_used_at FROM command_usage")
    rows = cursor.fetchall()
    conn.close()
    return {r[0]: {"count": r[1], "last_used_at": r[2]} for r in rows}


def get_username_for_discord_id(discord_id: str) -> str | None:
    """Лучшее известное отображаемое имя для чужого discord_id (не текущего
    залогиненного user — для него имя уже есть в сессии) — используется в
    веб-таблицах, чтобы показывать имя вместо голого ID (added_by/granted_by
    и т.п.). Источники по приоритету: username супер-админа (задаётся явно
    при выдаче статуса), самое свежее имя из web_access_log (пишется при
    каждом логине), затем discord_member_cache — часовой снимок display_name
    ВСЕХ участников Discord-серверов гильдий бота (см. sync_discord_member_cache),
    покрывающий и тех, кто ни разу не логинился в веб и не регистрировал
    SWGOH-аккаунт (например ДР незарегистрированного игрока). Возвращает None,
    если ни разу не встречался — вызывающий код в этом случае показывает сырой
    ID как есть (например, для сид-меток вида "startup-seed", которые не
    являются discord_id)."""
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    _ensure_bot_admins_table(cursor)
    cursor.execute("SELECT username FROM bot_admins WHERE discord_id = ? AND username IS NOT NULL", (str(discord_id),))
    row = cursor.fetchone()
    if row and row[0]:
        conn.close()
        return row[0]
    _ensure_web_access_log_table(cursor)
    cursor.execute(
        "SELECT username FROM web_access_log WHERE discord_id = ? ORDER BY id DESC LIMIT 1",
        (str(discord_id),)
    )
    row = cursor.fetchone()
    if row and row[0]:
        conn.close()
        return row[0]
    _ensure_discord_member_cache_table(cursor)
    cursor.execute("SELECT display_name FROM discord_member_cache WHERE discord_id = ?", (str(discord_id),))
    row = cursor.fetchone()
    conn.close()
    return row[0] if row and row[0] else None


def _ensure_discord_member_cache_table(cursor):
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS discord_member_cache (
            discord_id TEXT PRIMARY KEY,
            display_name TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
    """)


def sync_discord_member_cache(members: list):
    """Полная перезапись кэша display_name по всем участникам Discord-серверов
    гильдий бота — members: [(discord_id, display_name), ...] сразу со всех
    сконфигурированных гильдий. Единственный способ резолвить discord_id
    в имя там, где нет live-подключения к Discord (веб-дашборд — отдельный
    процесс без gateway), для участников, которые не логинились в веб и не
    проходили /регистрация. См. get_username_for_discord_id (последний фолбэк)
    и cogs/birthday.py::refresh_member_cache_loop (часовое обновление)."""
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    _ensure_discord_member_cache_table(cursor)
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.executemany(
        "INSERT OR REPLACE INTO discord_member_cache (discord_id, display_name, updated_at) VALUES (?, ?, datetime('now'))",
        members
    )
    conn.commit()
    conn.close()

# =====================================================================
# РУЧНЫЕ ГРАНТЫ ДОСТУПА: для игроков ВНЕ участвующих гильдий (или без
# закэшированного игрового ранга) — выдаются только супер-админами, см.
# guild_resolver.resolve_access (фолбэк после регистрации/ростер-кэша),
# cogs/admin_management.py.
# =====================================================================
def _ensure_manual_access_grants_table(cursor):
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS manual_access_grants (
            discord_id TEXT PRIMARY KEY,
            ally_code TEXT,
            guild_id INTEGER,
            tier TEXT NOT NULL CHECK(tier IN ('member', 'officer')),
            granted_by TEXT,
            granted_at TEXT NOT NULL
        )
    """)
    try:
        cursor.execute("ALTER TABLE manual_access_grants ADD COLUMN ingame_name TEXT")
    except sqlite3.OperationalError:
        pass  # колонка уже добавлена ранее


def add_manual_grant(discord_id: str, ally_code: str, guild_id: int, tier: str, granted_by: str = None, ingame_name: str = None) -> None:
    if tier not in ("member", "officer"):
        raise ValueError(f"Неверный уровень доступа: {tier!r} (ожидается 'member' или 'officer')")
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    _ensure_manual_access_grants_table(cursor)
    cursor.execute("""
        INSERT INTO manual_access_grants (discord_id, ally_code, guild_id, tier, granted_by, granted_at, ingame_name)
        VALUES (?, ?, ?, ?, ?, datetime('now'), ?)
        ON CONFLICT(discord_id) DO UPDATE SET
            ally_code = excluded.ally_code,
            guild_id = excluded.guild_id,
            tier = excluded.tier,
            granted_by = excluded.granted_by,
            granted_at = excluded.granted_at,
            ingame_name = excluded.ingame_name
    """, (str(discord_id), ally_code, guild_id, tier, granted_by, ingame_name))
    conn.commit()
    conn.close()


def remove_manual_grant(discord_id: str) -> bool:
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    _ensure_manual_access_grants_table(cursor)
    cursor.execute("DELETE FROM manual_access_grants WHERE discord_id = ?", (str(discord_id),))
    conn.commit()
    removed = cursor.rowcount > 0
    conn.close()
    return removed


def get_manual_grant(discord_id: str) -> dict | None:
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    _ensure_manual_access_grants_table(cursor)
    cursor.execute(
        "SELECT discord_id, ally_code, guild_id, tier, granted_by, granted_at, ingame_name FROM manual_access_grants WHERE discord_id = ?",
        (str(discord_id),)
    )
    row = cursor.fetchone()
    conn.close()
    if not row:
        return None
    return {
        "discord_id": row[0], "ally_code": row[1], "guild_id": row[2], "tier": row[3],
        "granted_by": row[4], "granted_at": row[5], "ingame_name": row[6],
    }


def get_all_manual_grants() -> list:
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    _ensure_manual_access_grants_table(cursor)
    cursor.execute("SELECT discord_id, ally_code, guild_id, tier, granted_by, granted_at, ingame_name FROM manual_access_grants ORDER BY granted_at")
    rows = cursor.fetchall()
    conn.close()
    return [
        {
            "discord_id": r[0], "ally_code": r[1], "guild_id": r[2], "tier": r[3],
            "granted_by": r[4], "granted_at": r[5], "ingame_name": r[6],
        }
        for r in rows
    ]

# =====================================================================
# ФУНКЦИИ ДЛЯ РАБОТЫ С НАРУШЕНИЯМИ (WARNS)
# =====================================================================
def add_warn(ally_code, category, subcategory, date_str, comment=None, guild_id=1):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO position_warns (ally_code, category, subcategory, date_str, comment, guild_id)
        VALUES (?, ?, ?, ?, ?, ?)
    """, (ally_code, category, subcategory, date_str, comment, guild_id))
    conn.commit()
    conn.close()

def get_player_warns(ally_code, guild_id=1):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("""
        SELECT id, category, subcategory, date_str, comment
        FROM position_warns
        WHERE ally_code = ? AND guild_id = ?
        ORDER BY id DESC
    """, (ally_code, guild_id))
    rows = cursor.fetchall()
    conn.close()
    return rows

def remove_warn(ally_code, category, subcategory, date_str, guild_id=1):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("""
        DELETE FROM position_warns
        WHERE ally_code = ? AND category = ? AND subcategory = ? AND date_str = ? AND guild_id = ?
    """, (ally_code, category, subcategory, date_str, guild_id))
    conn.commit()
    conn.close()

def remove_warn_by_id(warn_id: int, guild_id: int = 1):
    """Удаление по id (веб-дашборд) — в отличие от remove_warn, не задевает
    случайно другую строку с тем же составным ключом при дублирующихся нарушениях."""
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("DELETE FROM position_warns WHERE id = ? AND guild_id = ?", (warn_id, guild_id))
    conn.commit()
    conn.close()

def get_all_warns(guild_id=1):
    """Возвращает список всех нарушений гильдии для построения общей текстовой таблицы"""
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("SELECT ally_code, category, date_str FROM position_warns WHERE guild_id = ?", (guild_id,))
    rows = cursor.fetchall()
    conn.close()
    return rows    

# =====================================================================
# ФУНКЦИИ ДЛЯ НАПОЛНЕНИЯ СПРАВОЧНИКА ЮНИТОВ
# =====================================================================
def populate_initial_units(units_dict):
    """Принимает словарь {base_id: имя} и массово загружает в базу"""
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    
    # Подготавливаем данные для INSERT OR IGNORE, чтобы не затирать кастомные имена
    data = [(base_id, name) for base_id, name in units_dict.items()]
    
    cursor.executemany("""
        INSERT OR IGNORE INTO game_units (base_id, cached_name)
        VALUES (?, ?)
    """, data)

    conn.commit()
    conn.close()


def upsert_game_units(units_dict):
    """units_dict: {base_id: (cached_name, unit_type, cached_name_en)} — INSERT OR REPLACE,
    в отличие от populate_initial_units обновляет имя/тип при повторной синхронизации."""
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    data = [(base_id, name, unit_type, name_en) for base_id, (name, unit_type, name_en) in units_dict.items()]
    cursor.executemany("""
        INSERT OR REPLACE INTO game_units (base_id, cached_name, unit_type, cached_name_en)
        VALUES (?, ?, ?, ?)
    """, data)
    conn.commit()
    conn.close()


def prune_game_units(keep_base_ids):
    """Удаляет из game_units все base_id, не попавшие в keep_base_ids — чистит NPC/дубли по
    редкости, оставшиеся от синков до фильтра в services/units_sync.py::sync_units."""
    keep_base_ids = list(keep_base_ids)
    if not keep_base_ids:
        return
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    placeholders = ",".join("?" for _ in keep_base_ids)
    cursor.execute(f"DELETE FROM game_units WHERE base_id NOT IN ({placeholders})", keep_base_ids)
    conn.commit()
    conn.close()


def search_game_units(query: str, limit: int = 25):
    """Ищет и по русскому отображаемому имени, и по английскому (cached_name_en, может
    быть NULL для юнитов, добавленных до этого поля), и по base_id — так можно найти
    персонажа, набрав его имя на английской раскладке (см. web/routes/stat_builder.py).

    Фильтрация в Python, не через SQL LIKE/LOWER: SQLite's LOWER() регистронезависимо
    работает только для ASCII (кириллица не приводится к нижнему регистру вообще), из-за
    чего 'LOWER(cached_name) LIKE %кэл%' не находил бы 'Кэл' — заглавная буква внутри имени
    (не обязательно первая) оставалась как есть и переставала совпадать с уже гарантированно
    lower-регистрным Python-запросом. Таблица небольшая (~1200 юнитов) — full-scan в Python
    (со связностью Unicode-корректным str.lower()) дешевле и надёжнее, чем городить и
    поддерживать в актуальном состоянии отдельные lower-колонки."""
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("SELECT base_id, cached_name, cached_name_en FROM game_units")
    rows = cursor.fetchall()
    conn.close()

    q = query.lower()
    matches = []
    for base_id, name, name_en in rows:
        if q in name.lower() or q in base_id.lower() or (name_en and q in name_en.lower()):
            matches.append((base_id, name))
            if len(matches) >= limit:
                break
    return matches


def resolve_unit_display_names(names: list[str]) -> dict[str, str | None]:
    """Точный (не подстрочный, в отличие от search_game_units выше) поиск base_id по
    отображаемому имени — и по cached_name (RU), и по cached_name_en. Нужен для резолва
    справочника tb_platoon_data.py (имена сняты с EchoBase, на английском) в base_id.
    Для нерезолвленных имён отдаёт None — вызывающая сторона логирует и пропускает,
    не падает (ожидаемо немного непопаданий: '0-0-0', варианты с апострофами и т.п.)."""
    if not names:
        return {}
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("SELECT base_id, cached_name, cached_name_en FROM game_units")
    rows = cursor.fetchall()
    conn.close()

    by_name = {}
    for base_id, name, name_en in rows:
        by_name.setdefault(name.lower(), base_id)
        if name_en:
            by_name.setdefault(name_en.lower(), base_id)

    return {name: by_name.get(name.lower()) for name in names}


def get_game_unit_name(base_id: str) -> str | None:
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("SELECT cached_name FROM game_units WHERE base_id = ?", (base_id,))
    row = cursor.fetchone()
    conn.close()
    return row[0] if row else None


def get_game_unit_names(base_ids: list[str]) -> dict:
    """Батч-версия get_game_unit_name — один запрос вместо одного на каждый base_id
    (нужна там, где имена резолвятся для десятков/сотен строк разом, например /activity)."""
    if not base_ids:
        return {}
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    unique_ids = list(set(base_ids))
    placeholders = ", ".join("?" for _ in unique_ids)
    cursor.execute(f"SELECT base_id, cached_name FROM game_units WHERE base_id IN ({placeholders})", unique_ids)
    result = dict(cursor.fetchall())
    conn.close()
    return result


def get_unit_types(base_ids: list[str]) -> dict:
    """base_id -> "ship"/"character" (game_units.unit_type, см. services/units_sync.py —
    заполняется из Comlink combatType). Нужен конструктору взводов ТБ (tb_platoon_engine.py):
    у кораблей нет реликвии, донат-требование для них — 7★, а не порог реликвии."""
    if not base_ids:
        return {}
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    unique_ids = list(set(base_ids))
    placeholders = ", ".join("?" for _ in unique_ids)
    cursor.execute(f"SELECT base_id, unit_type FROM game_units WHERE base_id IN ({placeholders})", unique_ids)
    result = dict(cursor.fetchall())
    conn.close()
    return result

# =====================================================================
# ЗАДАНИЯ НА ПРОКАЧКУ (/задания + часовой аудит выполнения через Comlink)
# =====================================================================
def add_task(ally_code, base_id, target_type, target_value, deadline, created_by, guild_id: int = 1, batch_id: str = None) -> int:
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO tasks (ally_code, base_id, target_type, target_value, deadline, status, created_by, date_created, guild_id, batch_id)
        VALUES (?, ?, ?, ?, ?, 'ACTIVE', ?, datetime('now'), ?, ?)
    """, (ally_code, base_id, target_type, target_value, deadline, created_by, guild_id, batch_id))
    conn.commit()
    task_id = cursor.lastrowid
    conn.close()
    return task_id


def get_active_tasks(guild_id: int = 1):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("""
        SELECT task_id, ally_code, base_id, target_type, target_value, deadline
        FROM tasks WHERE status = 'ACTIVE' AND guild_id = ?
    """, (guild_id,))
    rows = cursor.fetchall()
    conn.close()
    return rows


def get_all_tasks(guild_id: int = 1):
    """Как get_active_tasks, но без фильтра по статусу — вся история (ACTIVE/
    COMPLETED/FAILED), для веб-дашборда (/tasks), которому нужно показывать
    не только активные задачи. batch_id — последней колонкой (аддитивно,
    существующие вызывающие по индексу 0-6 не ломаются)."""
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("""
        SELECT task_id, ally_code, base_id, target_type, target_value, deadline, status, batch_id
        FROM tasks WHERE guild_id = ? ORDER BY task_id DESC
    """, (guild_id,))
    rows = cursor.fetchall()
    conn.close()
    return rows


def get_tasks_for_ally(ally_code, guild_id: int = 1):
    """Задачи одного игрока (все статусы) — для self-view в /задания отчёт."""
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("""
        SELECT task_id, ally_code, base_id, target_type, target_value, deadline, status
        FROM tasks WHERE guild_id = ? AND ally_code = ? ORDER BY task_id DESC
    """, (guild_id, ally_code))
    rows = cursor.fetchall()
    conn.close()
    return rows


def get_task(task_id: int):
    """Одна задача целиком (со всеми колонками, включая guild_id/batch_id) — для
    проверки владения гильдией перед редактированием/удалением. None, если не найдена."""
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("""
        SELECT task_id, ally_code, base_id, target_type, target_value, deadline, status, guild_id, batch_id
        FROM tasks WHERE task_id = ?
    """, (task_id,))
    row = cursor.fetchone()
    conn.close()
    return row


def update_task_status(task_id, status):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("UPDATE tasks SET status = ? WHERE task_id = ?", (status, task_id))
    conn.commit()
    conn.close()


def update_task(task_id: int, base_id: str = None, target_type: str = None, target_value: str = None, deadline: str = None) -> None:
    """Частичное обновление (только переданные поля) — для редактирования уже
    поставленной задачи в вебе. Не трогает status/batch_id."""
    updates = {}
    if base_id is not None:
        updates["base_id"] = base_id
    if target_type is not None:
        updates["target_type"] = target_type
    if target_value is not None:
        updates["target_value"] = target_value
    if deadline is not None:
        updates["deadline"] = deadline
    if not updates:
        return
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    set_clause = ", ".join(f"{col} = ?" for col in updates)
    cursor.execute(f"UPDATE tasks SET {set_clause} WHERE task_id = ?", (*updates.values(), task_id))
    conn.commit()
    conn.close()


def delete_task(task_id: int) -> None:
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("DELETE FROM tasks WHERE task_id = ?", (task_id,))
    conn.commit()
    conn.close()


def delete_tasks_by_batch(batch_id: str, guild_id: int) -> int:
    """Отменяет разом всю группу задач, поставленных одним массовым действием.
    Возвращает число удалённых строк."""
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("DELETE FROM tasks WHERE batch_id = ? AND guild_id = ?", (batch_id, guild_id))
    conn.commit()
    deleted = cursor.rowcount
    conn.close()
    return deleted


def mark_task_reminder_sent(task_id: int) -> None:
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("UPDATE tasks SET reminder_sent_at = datetime('now') WHERE task_id = ?", (task_id,))
    conn.commit()
    conn.close()


def get_tasks_needing_reminder(guild_id: int, days_before: int):
    """Активные задачи этой гильдии, чей дедлайн наступает в пределах days_before дней,
    и по которым напоминание ещё не отправлялось — для tasks_reminder_loop."""
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("""
        SELECT task_id, ally_code, base_id, target_type, target_value, deadline
        FROM tasks
        WHERE guild_id = ? AND status = 'ACTIVE' AND reminder_sent_at IS NULL
          AND julianday(deadline) - julianday('now') <= ?
    """, (guild_id, days_before))
    rows = cursor.fetchall()
    conn.close()
    return rows


def get_discord_id_for_ally(ally_code: str, guild_id: int = 1):
    """Обратный резолв ally_code -> discord_id основной регистрации в этой гильдии
    (/регистрация) — для личных уведомлений о завершении/провале/дедлайне задачи.
    None, если игрок не регистрировался."""
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    _ensure_user_registration_table(cursor)
    cursor.execute(
        "SELECT discord_id FROM user_registration WHERE guild_id = ? AND ally_code = ? ORDER BY is_main DESC LIMIT 1",
        (guild_id, ally_code),
    )
    row = cursor.fetchone()
    conn.close()
    return row[0] if row else None

# ================== Дни рождения ==================

def init_birthday_table():
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS birthdays (
            guild_id INTEGER NOT NULL DEFAULT 1,
            discord_id TEXT NOT NULL,
            day INTEGER NOT NULL,
            month INTEGER NOT NULL,
            year INTEGER,
            PRIMARY KEY (guild_id, discord_id)
        )
    """)
    # Миграция с версии до мультитенантности (PRIMARY KEY только по discord_id) —
    # человек, отслеживаемый в двух гильдиях, теоретически возможен, поэтому
    # discord_id сам по себе больше не уникальный ключ.
    cursor.execute("PRAGMA table_info(birthdays)")
    cols = {row[1] for row in cursor.fetchall()}
    if cols and "guild_id" not in cols:
        cursor.execute("ALTER TABLE birthdays RENAME TO birthdays_old")
        cursor.execute("""
            CREATE TABLE birthdays (
                guild_id INTEGER NOT NULL DEFAULT 1,
                discord_id TEXT NOT NULL,
                day INTEGER NOT NULL,
                month INTEGER NOT NULL,
                year INTEGER,
                PRIMARY KEY (guild_id, discord_id)
            )
        """)
        cursor.execute("""
            INSERT INTO birthdays (guild_id, discord_id, day, month, year)
            SELECT 1, discord_id, day, month, year FROM birthdays_old
        """)
        cursor.execute("DROP TABLE birthdays_old")
    conn.commit()
    conn.close()

def add_birthday(discord_id: str, day: int, month: int, year: int = None, guild_id: int = 1):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("""
        INSERT OR REPLACE INTO birthdays (guild_id, discord_id, day, month, year)
        VALUES (?, ?, ?, ?, ?)
    """, (guild_id, discord_id, day, month, year))
    conn.commit()
    conn.close()

def remove_birthday(discord_id: str, guild_id: int = 1):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("DELETE FROM birthdays WHERE guild_id = ? AND discord_id = ?", (guild_id, discord_id))
    conn.commit()
    conn.close()

def get_all_birthdays(guild_id: int = 1):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("SELECT discord_id, day, month, year FROM birthdays WHERE guild_id = ?", (guild_id,))
    rows = cursor.fetchall()
    conn.close()
    return rows

def get_birthday_by_discord_id(discord_id: str, guild_id: int = 1):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("SELECT day, month, year FROM birthdays WHERE guild_id = ? AND discord_id = ?", (guild_id, discord_id))
    row = cursor.fetchone()
    conn.close()
    return row



    
    
def get_allycode_by_discord_id(discord_id: str, guild_id: int = 1) -> str | None:
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("SELECT ally_code FROM user_mapping WHERE discord_id = ? AND guild_id = ?", (discord_id, guild_id))
    row = cursor.fetchone()
    conn.close()
    return row[0] if row else None

def set_user_mapping(discord_id: str, ally_code: str, ingame_name: str = "", guild_id: int = 1):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("""
        INSERT OR REPLACE INTO user_mapping (guild_id, discord_id, ally_code, ingame_name)
        VALUES (?, ?, ?, ?)
    """, (guild_id, discord_id, ally_code, ingame_name))
    conn.commit()
    conn.close()

def get_all_user_mappings(guild_id: int = 1):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("SELECT discord_id, ally_code, ingame_name FROM user_mapping WHERE guild_id = ?", (guild_id,))
    rows = cursor.fetchall()
    conn.close()
    return rows


def get_all_user_mappings_with_rank(guild_id: int = 1):
    """Как get_all_user_mappings, но с member_level (см. sync_guild_roster) — отдельная
    функция, а не расширение существующей, чтобы не ломать 3-элементную распаковку у
    её текущих вызывающих (violations.py, tasks.py и т.д.)."""
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute(
        "SELECT ally_code, ingame_name, member_level FROM user_mapping WHERE guild_id = ?", (guild_id,)
    )
    rows = cursor.fetchall()
    conn.close()
    return rows


def get_guild_ids_for_ally_code(ally_code: str) -> set:
    """Все guild_id, где зарегистрирован этот ally_code (обычно один, но игрок теоретически
    может состоять в нескольких зарегистрированных гильдиях сразу — см. player_units_sync_loop
    в cogs/stat_requirements.py, тот же паттерн). Используется ручным синком с /activity,
    чтобы не потерять события для "чужой" гильдии, если игрок состоит сразу в нескольких."""
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("SELECT DISTINCT guild_id FROM user_mapping WHERE ally_code = ?", (ally_code,))
    rows = {r[0] for r in cursor.fetchall()}
    conn.close()
    return rows

def sync_guild_roster(guild_id: int, roster_rows):
    """Полная замена состава гильдии guild_id в user_mapping: roster_rows —
    [(discord_id, ally_code, ingame_name, member_level, comlink_player_id), ...]. member_level —
    сырое значение Comlink (4=лидер, 3=офицер, 2=рядовой участник), используется
    guild_resolver.resolve_access для прав по игровому рангу. comlink_player_id — сырой Comlink
    playerId, хранится отдельно от ally_code, чтобы при следующем синке можно было опознать
    того же игрока и подставить его последние известные имя/код союзника, если Comlink в этот
    раз не отдал allyCode/playerName (см. get_roster_by_player_id). Используется 15-минутным
    рефрешем ростер-кэша (ViolationsCog.update_roster_cache) — не трогает другие гильдии."""
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("DELETE FROM user_mapping WHERE guild_id = ?", (guild_id,))
    cursor.executemany(
        "INSERT OR REPLACE INTO user_mapping (guild_id, discord_id, ally_code, ingame_name, member_level, comlink_player_id) VALUES (?, ?, ?, ?, ?, ?)",
        [(guild_id, discord_id, ally_code, ingame_name, member_level, comlink_player_id)
         for discord_id, ally_code, ingame_name, member_level, comlink_player_id in roster_rows]
    )
    conn.commit()
    conn.close()


def get_roster_by_player_id(guild_id: int) -> dict:
    """{comlink_player_id: (ally_code, ingame_name)} для текущего состава guild_id —
    снимок ДО следующего sync_guild_roster, используется им же, чтобы не перетереть
    хорошо известного игрока мусором при временном сбое Comlink (см. sync_guild_roster)."""
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute(
        "SELECT comlink_player_id, ally_code, ingame_name FROM user_mapping WHERE guild_id = ? AND comlink_player_id IS NOT NULL",
        (guild_id,)
    )
    rows = {pid: (ally_code, ingame_name) for pid, ally_code, ingame_name in cursor.fetchall()}
    conn.close()
    return rows


def get_member_level(guild_id: int, ally_code: str) -> int | None:
    """Закэшированный игровой ранг (memberLevel из Comlink) игрока в гильдии —
    4=лидер, 3=офицер, 2=рядовой участник. None, если ally_code не найден в
    последнем ростер-кэше этой гильдии (не в гильдии/ещё не обновлялось)."""
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute(
        "SELECT member_level FROM user_mapping WHERE guild_id = ? AND ally_code = ?",
        (guild_id, ally_code)
    )
    row = cursor.fetchone()
    conn.close()
    return row[0] if row and row[0] is not None else None

def get_user_mapping_for_name(guild_id: int, name: str):
    """Точное совпадение по ingame_name в пределах гильдии — (ally_code, ingame_name) или None."""
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute(
        "SELECT ally_code, ingame_name FROM user_mapping WHERE guild_id = ? AND ingame_name = ?",
        (guild_id, name)
    )
    row = cursor.fetchone()
    conn.close()
    return row

# =====================================================================
# САМОСТОЯТЕЛЬНАЯ РЕГИСТРАЦИЯ ИГРОКОВ (/регистрация): discord_id -> ally_code,
# отдельно от user_mapping — ту таблицу каждый час полностью перезаписывает
# ViolationsCog.update_roster_cache (см. cogs/violations.py), так что реальная
# привязка к Discord ID там не переживёт следующий проход ростер-кэша.
# Один Discord-аккаунт может иметь несколько связанных ally_code (альты) —
# is_main отмечает, какой из них берётся по умолчанию командами вида "мой персонаж".
# =====================================================================
def _ensure_user_registration_table(cursor):
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS user_registration (
            guild_id INTEGER NOT NULL DEFAULT 1,
            discord_id TEXT NOT NULL,
            ally_code TEXT NOT NULL,
            ingame_name TEXT,
            is_main INTEGER NOT NULL DEFAULT 1,
            registered_at TEXT NOT NULL,
            PRIMARY KEY (guild_id, discord_id, ally_code)
        )
    """)
    # Миграция с версии до мультитенантности (PRIMARY KEY (discord_id, ally_code),
    # без guild_id) — переносим существующие записи в гильдию id=1. "Основной аккаунт"
    # раньше был глобальным фактом про discord_id, теперь — фактом в контексте
    # конкретной гильдии (игрок может быть основным в одной и альтом в другой).
    cursor.execute("PRAGMA table_info(user_registration)")
    cols = {row[1] for row in cursor.fetchall()}
    if cols and "guild_id" not in cols:
        cursor.execute("ALTER TABLE user_registration RENAME TO user_registration_old")
        cursor.execute("""
            CREATE TABLE user_registration (
                guild_id INTEGER NOT NULL DEFAULT 1,
                discord_id TEXT NOT NULL,
                ally_code TEXT NOT NULL,
                ingame_name TEXT,
                is_main INTEGER NOT NULL DEFAULT 1,
                registered_at TEXT NOT NULL,
                PRIMARY KEY (guild_id, discord_id, ally_code)
            )
        """)
        cursor.execute("""
            INSERT INTO user_registration (guild_id, discord_id, ally_code, ingame_name, is_main, registered_at)
            SELECT 1, discord_id, ally_code, ingame_name, is_main, registered_at FROM user_registration_old
        """)
        cursor.execute("DROP TABLE user_registration_old")
        # КРИТИЧНО: INSERT — это DML, а не DDL. Многие вызывающие функции здесь
        # read-only и никогда не делают conn.commit() перед conn.close() — без
        # явного commit тут скопированные строки тихо откатываются при закрытии
        # соединения (DROP TABLE ниже — DDL и НЕ коммитит предыдущий INSERT сам
        # по себе, проверено эмпирически). Коммитим прямо внутри миграции, не
        # полагаясь на вызывающую функцию.
        cursor.connection.commit()


def set_user_registration(discord_id: str, ally_code: str, ingame_name: str = "", is_main: bool = True, guild_id: int = 1):
    """Привязывает ally_code к discord_id в пределах гильдии. Если is_main=True, снимает
    флаг "основной" со всех остальных аккаунтов этого discord_id В ЭТОЙ ЖЕ гильдии
    (основной может быть только один на гильдию, но разный в разных гильдиях)."""
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    _ensure_user_registration_table(cursor)
    if is_main:
        cursor.execute(
            "UPDATE user_registration SET is_main = 0 WHERE guild_id = ? AND discord_id = ? AND ally_code != ?",
            (guild_id, discord_id, ally_code),
        )
    cursor.execute("""
        INSERT INTO user_registration (guild_id, discord_id, ally_code, ingame_name, is_main, registered_at)
        VALUES (?, ?, ?, ?, ?, datetime('now'))
        ON CONFLICT(guild_id, discord_id, ally_code) DO UPDATE SET
            ingame_name = excluded.ingame_name,
            is_main = excluded.is_main,
            registered_at = excluded.registered_at
    """, (guild_id, discord_id, ally_code, ingame_name, 1 if is_main else 0))
    conn.commit()
    conn.close()


def get_user_registration(discord_id: str, guild_id: int = 1):
    """Возвращает (ally_code, ingame_name) ОСНОВНОГО аккаунта в этой гильдии либо None,
    если игрок не регистрировался. Если основной почему-то не отмечен (не должно
    случаться), берёт любую привязанную запись, чтобы не отказывать без необходимости."""
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    _ensure_user_registration_table(cursor)
    cursor.execute(
        "SELECT ally_code, ingame_name FROM user_registration WHERE guild_id = ? AND discord_id = ? ORDER BY is_main DESC LIMIT 1",
        (guild_id, discord_id)
    )
    row = cursor.fetchone()
    conn.close()
    return row if row else None


def get_user_registrations(discord_id: str, guild_id: int = 1):
    """Возвращает все привязанные в этой гильдии аккаунты: [(ally_code, ingame_name, is_main), ...],
    основной — первым."""
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    _ensure_user_registration_table(cursor)
    cursor.execute(
        "SELECT ally_code, ingame_name, is_main FROM user_registration WHERE guild_id = ? AND discord_id = ? ORDER BY is_main DESC, registered_at",
        (guild_id, discord_id)
    )
    rows = cursor.fetchall()
    conn.close()
    return rows


def get_all_main_registrations(guild_id: int = 1):
    """Возвращает [(discord_id, ally_code, ingame_name), ...] по всем основным
    (is_main=1) регистрациям этой гильдии — для отчёта "кто зарегистрирован по гильдии"."""
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    _ensure_user_registration_table(cursor)
    cursor.execute("SELECT discord_id, ally_code, ingame_name FROM user_registration WHERE guild_id = ? AND is_main = 1", (guild_id,))
    rows = cursor.fetchall()
    conn.close()
    return rows


def get_all_registrations(guild_id: int = 1):
    """Возвращает [(discord_id, ally_code, ingame_name, is_main), ...] по вообще всем
    привязкам этой гильдии (и основным, и альтам) — для отчёта, различающего
    "не привязан" от "привязан как альт"."""
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    _ensure_user_registration_table(cursor)
    cursor.execute("SELECT discord_id, ally_code, ingame_name, is_main FROM user_registration WHERE guild_id = ?", (guild_id,))
    rows = cursor.fetchall()
    conn.close()
    return rows


# =====================================================================
# ПРОИЗВОЛЬНОЕ СОСТОЯНИЕ БОТА (переживает рестарты, key-value)
# =====================================================================
def _ensure_bot_state_table(cursor):
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS bot_state (
            guild_id INTEGER NOT NULL DEFAULT 1,
            key TEXT NOT NULL,
            value TEXT,
            PRIMARY KEY (guild_id, key)
        )
    """)
    # Миграция с версии до мультитенантности (PRIMARY KEY только по key).
    cursor.execute("PRAGMA table_info(bot_state)")
    cols = {row[1] for row in cursor.fetchall()}
    if cols and "guild_id" not in cols:
        cursor.execute("ALTER TABLE bot_state RENAME TO bot_state_old")
        cursor.execute("""
            CREATE TABLE bot_state (
                guild_id INTEGER NOT NULL DEFAULT 1,
                key TEXT NOT NULL,
                value TEXT,
                PRIMARY KEY (guild_id, key)
            )
        """)
        cursor.execute("INSERT INTO bot_state (guild_id, key, value) SELECT 1, key, value FROM bot_state_old")
        cursor.execute("DROP TABLE bot_state_old")
        cursor.connection.commit()  # см. объяснение в _ensure_user_registration_table

def get_bot_state(key: str, guild_id: int = 1) -> str | None:
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    _ensure_bot_state_table(cursor)
    cursor.execute("SELECT value FROM bot_state WHERE guild_id = ? AND key = ?", (guild_id, key))
    row = cursor.fetchone()
    conn.close()
    return row[0] if row else None

def set_bot_state(key: str, value: str, guild_id: int = 1):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    _ensure_bot_state_table(cursor)
    cursor.execute("""
        INSERT OR REPLACE INTO bot_state (guild_id, key, value)
        VALUES (?, ?, ?)
    """, (guild_id, key, value))
    conn.commit()
    conn.close()


# =====================================================================
# ИСТОРИЯ ТБ (последние N событий, для команд compare / player_compare)
# =====================================================================
TB_HISTORY_KEEP = 10  # было 6 — увеличено, чтобы график ТБ на дашборде вмещал больше истории

def _ensure_tb_history_tables(cursor):
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS tb_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id INTEGER NOT NULL DEFAULT 1,
            fingerprint TEXT NOT NULL,
            completed_at TEXT NOT NULL,
            UNIQUE (guild_id, fingerprint)
        )
    """)
    # Миграция с версии до мультитенантности (UNIQUE был только по fingerprint).
    cursor.execute("PRAGMA table_info(tb_events)")
    cols = {row[1] for row in cursor.fetchall()}
    if cols and "guild_id" not in cols:
        cursor.execute("ALTER TABLE tb_events RENAME TO tb_events_old")
        cursor.execute("""
            CREATE TABLE tb_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL DEFAULT 1,
                fingerprint TEXT NOT NULL,
                completed_at TEXT NOT NULL,
                UNIQUE (guild_id, fingerprint)
            )
        """)
        cursor.execute("""
            INSERT INTO tb_events (id, guild_id, fingerprint, completed_at)
            SELECT id, 1, fingerprint, completed_at FROM tb_events_old
        """)
        cursor.execute("DROP TABLE tb_events_old")
        cursor.connection.commit()  # см. объяснение в _ensure_user_registration_table
    try:
        # totalStars — реальное поле recentTerritoryBattleResult[0] от Comlink (guild.get(...)),
        # ранее не сохранялось нигде (код читал только finalStat); для событий, записанных
        # до этой колонки, останется NULL — старую ТБ Comlink не переспросишь задним числом.
        cursor.execute("ALTER TABLE tb_events ADD COLUMN stars INTEGER")
    except sqlite3.OperationalError:
        pass
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS tb_player_summary (
            event_id INTEGER NOT NULL,
            member_id TEXT NOT NULL,
            player_name TEXT NOT NULL,
            summary INTEGER NOT NULL,
            unit_donated INTEGER NOT NULL,
            covert_attempt INTEGER NOT NULL,
            strike_encounter INTEGER NOT NULL,
            strike_attempt INTEGER NOT NULL,
            PRIMARY KEY (event_id, member_id)
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS tb_player_detail (
            event_id INTEGER NOT NULL,
            member_id TEXT NOT NULL,
            player_name TEXT NOT NULL,
            zone_data_json TEXT NOT NULL,
            global_totals_json TEXT NOT NULL,
            round_totals_json TEXT NOT NULL,
            raw_keys_json TEXT NOT NULL,
            PRIMARY KEY (event_id, member_id)
        )
    """)

def record_tb_event(fingerprint: str, guild_id: int = 1, stars: int | None = None) -> int:
    """Идемпотентно регистрирует ТБ гильдии по отпечатку (fingerprint), возвращает event_id.
    stars — totalStars из recentTerritoryBattleResult (см. cogs/guild_events.py::_store_tb_history);
    если событие уже было записано без stars (monitor_loop дошёл раньше, чем появилась эта колонка,
    либо предыдущий тик не смог его распарсить) — дозаписываем, только если ещё NULL."""
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    _ensure_tb_history_tables(cursor)
    cursor.execute("SELECT id FROM tb_events WHERE guild_id = ? AND fingerprint = ?", (guild_id, fingerprint))
    row = cursor.fetchone()
    if row:
        if stars is not None:
            cursor.execute(
                "UPDATE tb_events SET stars = ? WHERE id = ? AND stars IS NULL", (stars, row[0])
            )
            conn.commit()
        conn.close()
        return row[0]
    cursor.execute(
        "INSERT INTO tb_events (guild_id, fingerprint, completed_at, stars) VALUES (?, ?, datetime('now'), ?)",
        (guild_id, fingerprint, stars)
    )
    event_id = cursor.lastrowid
    conn.commit()
    conn.close()
    return event_id

def save_tb_player_summary(rows):
    """rows: [(event_id, member_id, player_name, summary, unit_donated, covert_attempt, strike_encounter, strike_attempt), ...]"""
    if not rows:
        return
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    _ensure_tb_history_tables(cursor)
    cursor.executemany("""
        INSERT OR REPLACE INTO tb_player_summary
        (event_id, member_id, player_name, summary, unit_donated, covert_attempt, strike_encounter, strike_attempt)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, rows)
    conn.commit()
    conn.close()

def save_tb_player_detail(rows):
    """rows: [(event_id, member_id, player_name, zone_data_json, global_totals_json, round_totals_json, raw_keys_json), ...]"""
    if not rows:
        return
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    _ensure_tb_history_tables(cursor)
    cursor.executemany("""
        INSERT OR REPLACE INTO tb_player_detail
        (event_id, member_id, player_name, zone_data_json, global_totals_json, round_totals_json, raw_keys_json)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, rows)
    conn.commit()
    conn.close()

def prune_tb_events(keep: int = TB_HISTORY_KEEP, guild_id: int = 1):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    _ensure_tb_history_tables(cursor)
    _ensure_tb_plan_tables(cursor)
    cursor.execute("SELECT id FROM tb_events WHERE guild_id = ? ORDER BY id DESC LIMIT -1 OFFSET ?", (guild_id, keep))
    old_ids = [r[0] for r in cursor.fetchall()]
    if old_ids:
        placeholders = ",".join("?" * len(old_ids))
        cursor.execute(f"DELETE FROM tb_player_summary WHERE event_id IN ({placeholders})", old_ids)
        cursor.execute(f"DELETE FROM tb_player_detail WHERE event_id IN ({placeholders})", old_ids)
        cursor.execute(f"DELETE FROM tb_event_planet_names WHERE event_id IN ({placeholders})", old_ids)
        cursor.execute(f"DELETE FROM tb_events WHERE id IN ({placeholders})", old_ids)
    conn.commit()
    conn.close()

def get_recent_tb_events(limit: int = TB_HISTORY_KEEP, guild_id: int = 1):
    """Возвращает [(event_id, completed_at), ...] от старых к новым (максимум `limit`) для этой гильдии."""
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    _ensure_tb_history_tables(cursor)
    cursor.execute("SELECT id, completed_at FROM tb_events WHERE guild_id = ? ORDER BY id DESC LIMIT ?", (guild_id, limit))
    rows = cursor.fetchall()
    conn.close()
    return list(reversed(rows))

def get_tb_event_stars(event_ids: list) -> dict:
    """{event_id: stars}, только для событий, где totalStars реально сохранён (см.
    record_tb_event) — старые события до этой колонки в словаре не появятся,
    вызывающий код должен использовать .get(event_id) и обрабатывать отсутствие."""
    if not event_ids:
        return {}
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    _ensure_tb_history_tables(cursor)
    placeholders = ",".join("?" * len(event_ids))
    cursor.execute(f"SELECT id, stars FROM tb_events WHERE id IN ({placeholders}) AND stars IS NOT NULL", event_ids)
    rows = cursor.fetchall()
    conn.close()
    return {eid: stars for eid, stars in rows}

def get_tb_player_summary_for_events(event_ids):
    if not event_ids:
        return []
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    _ensure_tb_history_tables(cursor)
    placeholders = ",".join("?" * len(event_ids))
    cursor.execute(f"""
        SELECT event_id, member_id, player_name, summary, unit_donated, covert_attempt, strike_encounter, strike_attempt
        FROM tb_player_summary WHERE event_id IN ({placeholders})
    """, event_ids)
    rows = cursor.fetchall()
    conn.close()
    return rows

def get_tb_player_detail(event_id, member_id):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    _ensure_tb_history_tables(cursor)
    cursor.execute("""
        SELECT zone_data_json, global_totals_json, round_totals_json, raw_keys_json
        FROM tb_player_detail WHERE event_id = ? AND member_id = ?
    """, (event_id, member_id))
    row = cursor.fetchone()
    conn.close()
    return row


# =====================================================================
# ИСТОРИЯ ВГ (Territory War): comlink отдаёт только последние ~8 завершённых
# ВГ на гильдию (recentTerritoryWarResult, только гильдийский уровень —
# нет пер-игрока разбивки, см. память project_territory_war_report_gap), так
# что мы сохраняем их себе, чтобы история не терялась после того, как окно
# comlink укатится дальше. territory_war_id ("tw01A"-"tw01D") — это код
# матчап-банда, а НЕ уникальный id конкретной ВГ (повторяется у разных ВГ) —
# дедуп идёт по (guild_id, start_time), start_time уникален на инстанс войны.
# =====================================================================
def _ensure_tw_events_table(cursor):
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS tw_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id INTEGER NOT NULL,
            territory_war_id TEXT,
            own_score INTEGER,
            opponent_score INTEGER,
            own_power INTEGER,
            opponent_name TEXT,
            opponent_guild_id TEXT,
            opponent_gp INTEGER,
            start_time INTEGER,
            end_time INTEGER,
            result TEXT,
            recorded_at TEXT DEFAULT (datetime('now')),
            UNIQUE (guild_id, start_time)
        )
    """)


def upsert_tw_event(guild_id: int, territory_war_id: str, own_score: int, opponent_score: int,
                     own_power: int, opponent_name: str, opponent_guild_id: str, opponent_gp: int,
                     start_time: int, end_time: int, result: str):
    """INSERT OR IGNORE по (guild_id, start_time) — вызывается на каждом тике monitor_loop
    (см. cogs/guild_events.py::generate_tw_report) со всем recentTerritoryWarResult разом,
    поэтому одновременно и подхватывает новые завершённые ВГ, и на первом же тике после
    деплоя фичи бэкфиллит все ~8 доступных в comlink историй без отдельного скрипта."""
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    _ensure_tw_events_table(cursor)
    cursor.execute("""
        INSERT OR IGNORE INTO tw_events
            (guild_id, territory_war_id, own_score, opponent_score, own_power,
             opponent_name, opponent_guild_id, opponent_gp, start_time, end_time, result)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (guild_id, territory_war_id, own_score, opponent_score, own_power,
          opponent_name, opponent_guild_id, opponent_gp, start_time, end_time, result))
    conn.commit()
    conn.close()


def get_recent_tw_events(guild_id: int, limit: int | None = 10):
    """[(territory_war_id, own_score, opponent_score, own_power, opponent_name,
    opponent_guild_id, opponent_gp, start_time, end_time, result), ...] от новых к старым.
    limit=None — вся накопленная история без обрезки (нужно для статистики винрейта
    по периодам, см. dashboard_data.get_tw_stats)."""
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    _ensure_tw_events_table(cursor)
    query = """
        SELECT territory_war_id, own_score, opponent_score, own_power, opponent_name,
               opponent_guild_id, opponent_gp, start_time, end_time, result
        FROM tw_events WHERE guild_id = ? ORDER BY start_time DESC
    """
    params = [guild_id]
    if limit is not None:
        query += " LIMIT ?"
        params.append(limit)
    cursor.execute(query, params)
    rows = cursor.fetchall()
    conn.close()
    return rows


# =====================================================================
# ПЛАН ТБ: реальные названия планет по фазам/веткам (авто-парсинг анонсов
# офицеров в #ac-тб-оповещения + ручная команда /тб_отчет план как фолбэк)
# =====================================================================
def _ensure_tb_plan_tables(cursor):
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS tb_planet_names (
            guild_id INTEGER NOT NULL DEFAULT 1,
            phase TEXT NOT NULL,
            conflict_key TEXT NOT NULL,
            planet_name TEXT NOT NULL,
            source TEXT NOT NULL DEFAULT 'manual',
            updated_at TEXT NOT NULL,
            PRIMARY KEY (guild_id, phase, conflict_key)
        )
    """)
    # Миграция с версии до мультитенантности (PRIMARY KEY (phase, conflict_key),
    # единый "живой план" на всех — с двумя гильдиями это был бы общий стейт,
    # который они бы друг другу затирали.
    cursor.execute("PRAGMA table_info(tb_planet_names)")
    cols = {row[1] for row in cursor.fetchall()}
    if cols and "guild_id" not in cols:
        cursor.execute("ALTER TABLE tb_planet_names RENAME TO tb_planet_names_old")
        cursor.execute("""
            CREATE TABLE tb_planet_names (
                guild_id INTEGER NOT NULL DEFAULT 1,
                phase TEXT NOT NULL,
                conflict_key TEXT NOT NULL,
                planet_name TEXT NOT NULL,
                source TEXT NOT NULL DEFAULT 'manual',
                updated_at TEXT NOT NULL,
                PRIMARY KEY (guild_id, phase, conflict_key)
            )
        """)
        cursor.execute("""
            INSERT INTO tb_planet_names (guild_id, phase, conflict_key, planet_name, source, updated_at)
            SELECT 1, phase, conflict_key, planet_name, source, updated_at FROM tb_planet_names_old
        """)
        cursor.execute("DROP TABLE tb_planet_names_old")
        cursor.connection.commit()  # см. объяснение в _ensure_user_registration_table
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS tb_event_planet_names (
            event_id INTEGER NOT NULL,
            phase TEXT NOT NULL,
            conflict_key TEXT NOT NULL,
            planet_name TEXT NOT NULL,
            PRIMARY KEY (event_id, phase, conflict_key)
        )
    """)

def set_tb_planet_name(phase: str, conflict_key: str, planet_name: str, source: str = "manual", guild_id: int = 1):
    """conflict_key: '01'/'02'/'03' (Light/Dark/Mixed) или 'bonus' для доп. зоны."""
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    _ensure_tb_plan_tables(cursor)
    cursor.execute("""
        INSERT OR REPLACE INTO tb_planet_names (guild_id, phase, conflict_key, planet_name, source, updated_at)
        VALUES (?, ?, ?, ?, ?, datetime('now'))
    """, (guild_id, phase, conflict_key, planet_name, source))
    conn.commit()
    conn.close()

def get_tb_planet_names(guild_id: int = 1):
    """Текущий (живой) план планет гильдии — для отчёта по ещё не заснэпшоченной/последней ТБ."""
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    _ensure_tb_plan_tables(cursor)
    cursor.execute("SELECT phase, conflict_key, planet_name FROM tb_planet_names WHERE guild_id = ?", (guild_id,))
    rows = cursor.fetchall()
    conn.close()
    return {(phase, conflict_key): planet_name for phase, conflict_key, planet_name in rows}

def clear_tb_planet_names(guild_id: int = 1):
    """Вызывается при анонсе 1 этапа новой ТБ, чтобы не тащить названия планет прошлой ТБ."""
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    _ensure_tb_plan_tables(cursor)
    cursor.execute("DELETE FROM tb_planet_names WHERE guild_id = ?", (guild_id,))
    conn.commit()
    conn.close()

def snapshot_tb_planet_names(event_id: int, guild_id: int = 1):
    """Копирует текущий живой план планет гильдии в историю конкретного завершённого события ТБ."""
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    _ensure_tb_plan_tables(cursor)
    cursor.execute("SELECT phase, conflict_key, planet_name FROM tb_planet_names WHERE guild_id = ?", (guild_id,))
    rows = cursor.fetchall()
    if rows:
        cursor.executemany("""
            INSERT OR REPLACE INTO tb_event_planet_names (event_id, phase, conflict_key, planet_name)
            VALUES (?, ?, ?, ?)
        """, [(event_id, phase, conflict_key, planet_name) for phase, conflict_key, planet_name in rows])
    conn.commit()
    conn.close()

def get_tb_event_planet_names(event_id: int):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    _ensure_tb_plan_tables(cursor)
    cursor.execute(
        "SELECT phase, conflict_key, planet_name FROM tb_event_planet_names WHERE event_id = ?", (event_id,)
    )
    rows = cursor.fetchall()
    conn.close()
    return {(phase, conflict_key): planet_name for phase, conflict_key, planet_name in rows}


# =====================================================================
# СОХРАНЁННЫЕ ПЛАНЫ ОРДЕРА ТБ: именованные ссылки на ветки, где /тб_ордер_из_
# картинки уже собрал готовый ордер на 6 этапов. Одна из них помечается
# "активной" (guilds.tb_active_plan_id) — именно её тред tb_order_loop
# (cogs/guild_events.py) читает при ежедневной публикации вместо статического
# guilds.tb_order_source_channel_id — так офицер может завести вторую ветку
# под новый план ТБ и переключиться на неё одной командой, не трогая настройки.
# =====================================================================
def _ensure_tb_saved_plans_table(cursor):
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS tb_saved_plans (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            thread_id TEXT NOT NULL,
            total_stars INTEGER,
            created_by TEXT,
            created_at TEXT NOT NULL
        )
    """)


def save_tb_plan(guild_id: int, name: str, thread_id: int, total_stars: int, created_by: str = None) -> int:
    """Upsert по (guild_id, name) — повторный прогон /тб_ордер_из_картинки под тем
    же названием (например, план поправили и перепубликовали в ту же ветку с
    принудительно=True) обновляет запись, а не плодит дубли."""
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    _ensure_tb_saved_plans_table(cursor)
    cursor.execute("SELECT id FROM tb_saved_plans WHERE guild_id = ? AND name = ?", (guild_id, name))
    row = cursor.fetchone()
    if row:
        plan_id = row[0]
        cursor.execute("""
            UPDATE tb_saved_plans SET thread_id = ?, total_stars = ?, created_by = ?, created_at = datetime('now')
            WHERE id = ?
        """, (str(thread_id), total_stars, created_by, plan_id))
    else:
        cursor.execute("""
            INSERT INTO tb_saved_plans (guild_id, name, thread_id, total_stars, created_by, created_at)
            VALUES (?, ?, ?, ?, ?, datetime('now'))
        """, (guild_id, name, str(thread_id), total_stars, created_by))
        plan_id = cursor.lastrowid
    conn.commit()
    conn.close()
    return plan_id


def get_tb_saved_plans(guild_id: int) -> list:
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    _ensure_tb_saved_plans_table(cursor)
    cursor.execute(
        "SELECT id, name, thread_id, total_stars, created_at FROM tb_saved_plans WHERE guild_id = ? ORDER BY created_at DESC",
        (guild_id,)
    )
    rows = cursor.fetchall()
    conn.close()
    return [
        {"id": r[0], "name": r[1], "thread_id": r[2], "total_stars": r[3], "created_at": r[4]}
        for r in rows
    ]


def get_tb_saved_plan(plan_id: int) -> dict | None:
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    _ensure_tb_saved_plans_table(cursor)
    cursor.execute(
        "SELECT id, guild_id, name, thread_id, total_stars, created_at FROM tb_saved_plans WHERE id = ?", (plan_id,)
    )
    row = cursor.fetchone()
    conn.close()
    if not row:
        return None
    return {"id": row[0], "guild_id": row[1], "name": row[2], "thread_id": row[3], "total_stars": row[4], "created_at": row[5]}


def get_tb_saved_plan_by_name(guild_id: int, name: str) -> dict | None:
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    _ensure_tb_saved_plans_table(cursor)
    cursor.execute(
        "SELECT id, guild_id, name, thread_id, total_stars, created_at FROM tb_saved_plans WHERE guild_id = ? AND name = ?",
        (guild_id, name)
    )
    row = cursor.fetchone()
    conn.close()
    if not row:
        return None
    return {"id": row[0], "guild_id": row[1], "name": row[2], "thread_id": row[3], "total_stars": row[4], "created_at": row[5]}


def delete_tb_saved_plan(guild_id: int, name: str) -> bool:
    """Если удаляемый план был активным (guilds.tb_active_plan_id) — сбрасывает
    указатель, чтобы tb_order_loop не ссылался на несуществующую запись."""
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    _ensure_tb_saved_plans_table(cursor)
    cursor.execute("SELECT id FROM tb_saved_plans WHERE guild_id = ? AND name = ?", (guild_id, name))
    row = cursor.fetchone()
    if not row:
        conn.close()
        return False
    plan_id = row[0]
    cursor.execute("DELETE FROM tb_saved_plans WHERE id = ?", (plan_id,))
    cursor.execute("UPDATE guilds SET tb_active_plan_id = NULL WHERE tb_active_plan_id = ?", (plan_id,))
    conn.commit()
    conn.close()
    return True


# =====================================================================
# НАЗНАЧЕНИЯ ИГРОКОВ НА СЛОТЫ ВЗВОДОВ ТБ: закрепление конкретного игрока
# гильдии за конкретным донат-слотом конкретной планеты/операции конкретного
# сохранённого плана (web/routes/guild_dashboard.py::tb_platoons) — аналог
# "Assign Players" в HotUtils. Привязано к plan_id (не только к planet/round),
# т.к. один и тот же план — это конкретный прогон ТБ с конкретным, статичным
# сопоставлением этап->планета (тред плана уже опубликован и не меняется);
# переключение вида на другой сохранённый план должно показывать его
# СОБСТВЕННЫЕ назначения, а не унаследованные от активного.
#
# Слот НЕ привязан к номеру этапа (round_num) — по прямому запросу пользователя
# 2026-08-29: если планета не зачищена целиком и висит 2+ этапа подряд (тот же физический
# донат-слот в игре, TB_PLANET_CONFLICT/tb_platoon_data.py об этом уже предупреждали), это
# ОДИН и тот же слот на всех этих этапах, а не разные. До этой правки round_num входил в
# UNIQUE-ключ — донат, вписанный при просмотре этапа 3, был не виден на этапе 4 для той же
# планеты (слот выглядел пустым), а фильтр "не более N юнитов на планету от игрока" в
# принципе не мог посчитать верно (переносящаяся планета считалась как две разных). round_num
# в схеме остаётся — на identity слота НЕ влияет (не входит в UNIQUE), но с 2026-08-30
# используется как "round-aware" маркер отображения: слот считается занятым на этапе R,
# только если round_num <= R (см. set_tb_platoon_assignment — round_num ОБНОВЛЯЕТСЯ при
# каждом вызове, и web/routes/guild_dashboard.py::tb_platoons/_build_round_platoon_assignments,
# tb_platoon_autofill.py — все фильтруют по этому условию). Это позволяет автозаполнению
# "откладывать" последний слот многоэтапной планеты на её ПОСЛЕДНИЙ этап, не убирая донора
# совсем: с более раннего этапа слот выглядит пустым (донат туда ещё не сделан "по времени"),
# а с этапа, на который донат отнесён — занятым. См. get_tb_platoon_assignments/set_/clear_ —
# все три не принимают/не возвращают round_num как часть КЛЮЧА слота (идентичность слота —
# planet+operation+slot_index), но round_num остаётся значимым для отображения/экспорта.
# =====================================================================
def _ensure_tb_platoon_assignments_table(cursor):
    # Миграция со старой схемы (UNIQUE включал round_num) — переносим существующие
    # назначения: группируем по (guild_id, plan_id, planet, operation, slot_index), если
    # один и тот же слот успели заполнить под двумя разными round_num (до этой правки такое
    # могло случиться на планете, растянутой на 2+ этапа) — берём запись с максимальным
    # assigned_at как самую свежую, round_num — минимальный среди дублей ("впервые вписан").
    cursor.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='tb_platoon_assignments'")
    row = cursor.fetchone()
    needs_migration = row is not None and "UNIQUE(guild_id, plan_id, round_num, planet, operation, slot_index)" in (row[0] or "")
    if needs_migration:
        cursor.execute("ALTER TABLE tb_platoon_assignments RENAME TO tb_platoon_assignments_old_round_scoped")

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS tb_platoon_assignments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id INTEGER NOT NULL,
            plan_id INTEGER NOT NULL,
            round_num INTEGER NOT NULL,
            planet TEXT NOT NULL,
            operation INTEGER NOT NULL,
            slot_index INTEGER NOT NULL,
            ally_code TEXT NOT NULL,
            assigned_by TEXT,
            assigned_at TEXT NOT NULL,
            UNIQUE(guild_id, plan_id, planet, operation, slot_index)
        )
    """)

    if needs_migration:
        cursor.execute("""
            INSERT INTO tb_platoon_assignments
                (guild_id, plan_id, round_num, planet, operation, slot_index, ally_code, assigned_by, assigned_at)
            SELECT o.guild_id, o.plan_id, MIN(o.round_num), o.planet, o.operation, o.slot_index, o.ally_code, o.assigned_by, o.assigned_at
            FROM tb_platoon_assignments_old_round_scoped o
            WHERE o.assigned_at = (
                SELECT MAX(o2.assigned_at) FROM tb_platoon_assignments_old_round_scoped o2
                WHERE o2.guild_id = o.guild_id AND o2.plan_id = o.plan_id AND o2.planet = o.planet
                  AND o2.operation = o.operation AND o2.slot_index = o.slot_index
            )
            GROUP BY o.guild_id, o.plan_id, o.planet, o.operation, o.slot_index
        """)
        cursor.execute("DROP TABLE tb_platoon_assignments_old_round_scoped")


def set_tb_platoon_assignment(
    guild_id: int, plan_id: int, round_num: int, planet: str, operation: int, slot_index: int,
    ally_code: str, assigned_by: str = None,
) -> None:
    """round_num — этап, к которому сейчас отнесён донат — определяет, на каких этапах слот
    показывается как занятый (см. "round-aware" фильтр в web/routes/guild_dashboard.py и
    tb_platoon_autofill.py, 2026-08-30): слот считается занятым на этапе R, только если
    assignment.round_num <= R — донат, отнесённый к более позднему этапу (например,
    автозаполнение специально отложило последний слот многоэтапной планеты на её
    завершающий этап), на более раннем этапе показывается как ещё пустой. round_num
    ОБНОВЛЯЕТСЯ при каждом вызове (в т.ч. при переназначении слота другому игроку) — и
    ручное переназначение, и решение автозаполнения "к какому этапу отнести донат" должны
    сразу отражаться на том, где слот считается занятым."""
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    _ensure_tb_platoon_assignments_table(cursor)
    cursor.execute("""
        INSERT INTO tb_platoon_assignments
            (guild_id, plan_id, round_num, planet, operation, slot_index, ally_code, assigned_by, assigned_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
        ON CONFLICT(guild_id, plan_id, planet, operation, slot_index)
        DO UPDATE SET ally_code = excluded.ally_code, round_num = excluded.round_num, assigned_by = excluded.assigned_by, assigned_at = excluded.assigned_at
    """, (guild_id, plan_id, round_num, planet, operation, slot_index, ally_code, assigned_by))
    conn.commit()
    conn.close()


def clear_tb_platoon_assignment(
    guild_id: int, plan_id: int, planet: str, operation: int, slot_index: int,
) -> None:
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    _ensure_tb_platoon_assignments_table(cursor)
    cursor.execute("""
        DELETE FROM tb_platoon_assignments
        WHERE guild_id = ? AND plan_id = ? AND planet = ? AND operation = ? AND slot_index = ?
    """, (guild_id, plan_id, planet, operation, slot_index))
    conn.commit()
    conn.close()


def clear_tb_platoon_assignments_for_operation(guild_id: int, plan_id: int, planet: str, operation: int) -> None:
    """Кнопка «очистить операцию» на /tb/platoons."""
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    _ensure_tb_platoon_assignments_table(cursor)
    cursor.execute("""
        DELETE FROM tb_platoon_assignments WHERE guild_id = ? AND plan_id = ? AND planet = ? AND operation = ?
    """, (guild_id, plan_id, planet, operation))
    conn.commit()
    conn.close()


def clear_tb_platoon_assignments_for_planet(guild_id: int, plan_id: int, planet: str) -> None:
    """Кнопка «очистить планету» на /tb/platoons — все операции разом. Планета не привязана
    к round_num (см. комментарий над _ensure_tb_platoon_assignments_table), поэтому чистит
    её целиком независимо от того, на скольких этапах она сейчас показана."""
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    _ensure_tb_platoon_assignments_table(cursor)
    cursor.execute("""
        DELETE FROM tb_platoon_assignments WHERE guild_id = ? AND plan_id = ? AND planet = ?
    """, (guild_id, plan_id, planet))
    conn.commit()
    conn.close()


def clear_tb_platoon_assignments_for_plan(guild_id: int, plan_id: int) -> None:
    """Кнопка «очистить весь план» на /tb/platoons — все планеты, все этапы."""
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    _ensure_tb_platoon_assignments_table(cursor)
    cursor.execute("DELETE FROM tb_platoon_assignments WHERE guild_id = ? AND plan_id = ?", (guild_id, plan_id))
    conn.commit()
    conn.close()


def get_tb_platoon_assignments(guild_id: int, plan_id: int) -> dict:
    """(planet, operation, slot_index) -> {"ally_code", "assigned_by", "round_num"} — все
    назначения плана разом (round_num — только для отображения "впервые вписан на этапе N",
    не часть идентичности слота, см. комментарий над _ensure_tb_platoon_assignments_table)."""
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    _ensure_tb_platoon_assignments_table(cursor)
    cursor.execute("""
        SELECT planet, operation, slot_index, ally_code, assigned_by, round_num
        FROM tb_platoon_assignments WHERE guild_id = ? AND plan_id = ?
    """, (guild_id, plan_id))
    rows = cursor.fetchall()
    conn.close()
    return {
        (planet, operation, slot_index): {"ally_code": ally_code, "assigned_by": assigned_by, "round_num": round_num}
        for planet, operation, slot_index, ally_code, assigned_by, round_num in rows
    }


# =====================================================================
# "ДЕРЖИМ" ФЛАГ: офицер намеренно не добивает звёзды на планете в конкретном
# КАЛЕНДАРНОМ этапе просмотра плана (см. web/routes/guild_dashboard.py::tb_platoons,
# tb_platoon_autofill.py) — автозаполнение не должно закрывать 100% ни одной операции
# такой планеты. В отличие от tb_platoon_assignments (см. комментарий выше — там
# round_num вынесен из идентичности слота ради переноса планеты через этапы), здесь
# round_num ОСТАЁТСЯ частью ключа: "держим" — решение конкретно для одного этапа
# просмотра, та же планета вполне может быть "держим" на этапе 3 и "добиваем" на этапе 4.
# =====================================================================
def _ensure_tb_platoon_hold_flags_table(cursor):
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS tb_platoon_hold_flags (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id INTEGER NOT NULL,
            plan_id INTEGER NOT NULL,
            round_num INTEGER NOT NULL,
            planet TEXT NOT NULL,
            held INTEGER NOT NULL,
            set_by TEXT,
            set_at TEXT NOT NULL,
            UNIQUE(guild_id, plan_id, round_num, planet)
        )
    """)


def set_tb_platoon_hold(guild_id: int, plan_id: int, round_num: int, planet: str, held: bool, set_by: str = None) -> None:
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    _ensure_tb_platoon_hold_flags_table(cursor)
    cursor.execute("""
        INSERT INTO tb_platoon_hold_flags (guild_id, plan_id, round_num, planet, held, set_by, set_at)
        VALUES (?, ?, ?, ?, ?, ?, datetime('now'))
        ON CONFLICT(guild_id, plan_id, round_num, planet)
        DO UPDATE SET held = excluded.held, set_by = excluded.set_by, set_at = excluded.set_at
    """, (guild_id, plan_id, round_num, planet, int(held), set_by))
    conn.commit()
    conn.close()


def get_tb_platoon_holds(guild_id: int, plan_id: int) -> dict:
    """(round_num, planet) -> True, только для held=1 (планет, не отмеченных "держим",
    в словаре просто нет — вызывающая сторона использует .get(..., False) по месту)."""
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    _ensure_tb_platoon_hold_flags_table(cursor)
    cursor.execute("""
        SELECT round_num, planet, held FROM tb_platoon_hold_flags WHERE guild_id = ? AND plan_id = ?
    """, (guild_id, plan_id))
    rows = cursor.fetchall()
    conn.close()
    return {(round_num, planet): True for round_num, planet, held in rows if held}


# =====================================================================
# ФИЛЬТРЫ АВТОЗАПОЛНЕНИЯ ВЗВОДОВ: сырой текст правил на языке из tb_platoon_filters.py,
# один ряд на гильдию (не построчные CRUD-записи — веб-страница /tb/platoons/filters
# сохраняет/перечитывает textarea целиком, разбор строк — в tb_platoon_filters.py::parse_rules,
# не здесь). См. план "Автозаполнение взводов ТБ + фильтры" от 2026-08-29.
# =====================================================================
def _ensure_tb_platoon_filter_rules_table(cursor):
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS tb_platoon_filter_rules (
            guild_id INTEGER PRIMARY KEY,
            rules_text TEXT NOT NULL,
            updated_by TEXT,
            updated_at TEXT NOT NULL
        )
    """)


def get_tb_platoon_filter_rules(guild_id: int) -> str:
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    _ensure_tb_platoon_filter_rules_table(cursor)
    cursor.execute("SELECT rules_text FROM tb_platoon_filter_rules WHERE guild_id = ?", (guild_id,))
    row = cursor.fetchone()
    conn.close()
    return row[0] if row else ""


def set_tb_platoon_filter_rules(guild_id: int, rules_text: str, updated_by: str = None) -> None:
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    _ensure_tb_platoon_filter_rules_table(cursor)
    cursor.execute("""
        INSERT INTO tb_platoon_filter_rules (guild_id, rules_text, updated_by, updated_at)
        VALUES (?, ?, ?, datetime('now'))
        ON CONFLICT(guild_id) DO UPDATE SET rules_text = excluded.rules_text, updated_by = excluded.updated_by, updated_at = excluded.updated_at
    """, (guild_id, rules_text, updated_by))
    conn.commit()
    conn.close()


def get_user_mapping_by_name(name: str, guild_id: int = 1):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute(
        "SELECT ally_code, ingame_name FROM user_mapping WHERE guild_id = ? AND ingame_name LIKE ?",
        (guild_id, f"%{name}%")
    )
    row = cursor.fetchone()
    conn.close()
    return row if row else None


# =====================================================================
# ТРЕБОВАНИЯ К ДАТАКРОНАМ: список нужных билдов (уровни 3/6/9) по сезонам
# =====================================================================
def _ensure_datacron_requirements_table(cursor):
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS datacron_requirements (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            set_id INTEGER NOT NULL,
            level3_value TEXT NOT NULL,
            level6_value TEXT NOT NULL,
            level9_value TEXT NOT NULL,
            comment TEXT,
            created_by TEXT,
            created_at TEXT NOT NULL
        )
    """)
    try:
        cursor.execute("ALTER TABLE datacron_requirements ADD COLUMN pack TEXT")
    except sqlite3.OperationalError:
        pass  # колонка уже добавлена ранее
    try:
        cursor.execute("ALTER TABLE datacron_requirements ADD COLUMN priority TEXT NOT NULL DEFAULT 'required'")
    except sqlite3.OperationalError:
        pass  # колонка уже добавлена ранее
    try:
        cursor.execute("ALTER TABLE datacron_requirements ADD COLUMN guild_id INTEGER NOT NULL DEFAULT 1")
    except sqlite3.OperationalError:
        pass  # колонка уже добавлена ранее
    try:
        cursor.execute("ALTER TABLE datacron_requirements ADD COLUMN stats TEXT")
    except sqlite3.OperationalError:
        pass  # колонка уже добавлена ранее
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_datacron_req_guild_set ON datacron_requirements(guild_id, set_id)")


def add_datacron_requirement(set_id: int, pack: str, level3_value: str, level6_value: str, level9_value: str,
                              comment: str, created_by: str, priority: str = "required", guild_id: int = 1,
                              stats: str = None) -> int:
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    _ensure_datacron_requirements_table(cursor)
    cursor.execute("""
        INSERT INTO datacron_requirements
            (set_id, pack, level3_value, level6_value, level9_value, comment, created_by, created_at, priority, guild_id, stats)
        VALUES (?, ?, ?, ?, ?, ?, ?, datetime('now'), ?, ?, ?)
    """, (set_id, pack, level3_value, level6_value, level9_value, comment, created_by, priority, guild_id, stats))
    conn.commit()
    req_id = cursor.lastrowid
    conn.close()
    return req_id


def update_datacron_requirement(req_id: int, set_id: int, pack: str, level3_value: str, level6_value: str,
                                 level9_value: str, comment: str, priority: str, guild_id: int = 1,
                                 stats: str = None) -> bool:
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    _ensure_datacron_requirements_table(cursor)
    cursor.execute("""
        UPDATE datacron_requirements
        SET set_id = ?, pack = ?, level3_value = ?, level6_value = ?, level9_value = ?, comment = ?, priority = ?, stats = ?
        WHERE id = ? AND guild_id = ?
    """, (set_id, pack, level3_value, level6_value, level9_value, comment, priority, stats, req_id, guild_id))
    conn.commit()
    updated = cursor.rowcount > 0
    conn.close()
    return updated


def delete_datacron_requirement(req_id: int, guild_id: int = 1) -> bool:
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    _ensure_datacron_requirements_table(cursor)
    _ensure_datacron_requirement_stats_table(cursor)
    cursor.execute("DELETE FROM datacron_requirement_stats WHERE req_id = ? AND guild_id = ?", (req_id, guild_id))
    cursor.execute("DELETE FROM datacron_requirements WHERE id = ? AND guild_id = ?", (req_id, guild_id))
    conn.commit()
    deleted = cursor.rowcount > 0
    conn.close()
    return deleted


def delete_datacron_requirements_by_set(set_id: int, guild_id: int = 1) -> int:
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    _ensure_datacron_requirements_table(cursor)
    _ensure_datacron_requirement_stats_table(cursor)
    cursor.execute(
        "DELETE FROM datacron_requirement_stats WHERE guild_id = ? AND req_id IN "
        "(SELECT id FROM datacron_requirements WHERE set_id = ? AND guild_id = ?)",
        (guild_id, set_id, guild_id),
    )
    cursor.execute("DELETE FROM datacron_requirements WHERE set_id = ? AND guild_id = ?", (set_id, guild_id))
    conn.commit()
    deleted = cursor.rowcount
    conn.close()
    return deleted


def count_datacron_requirements_by_set(set_id: int, guild_id: int = 1) -> int:
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    _ensure_datacron_requirements_table(cursor)
    cursor.execute("SELECT COUNT(*) FROM datacron_requirements WHERE set_id = ? AND guild_id = ?", (set_id, guild_id))
    count = cursor.fetchone()[0]
    conn.close()
    return count


def get_datacron_requirement(req_id: int, guild_id: int = 1):
    """Требование по id — только если оно принадлежит указанной гильдии (предотвращает
    просмотр/редактирование чужого требования по угаданному числовому id)."""
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    _ensure_datacron_requirements_table(cursor)
    cursor.execute("""
        SELECT id, set_id, pack, level3_value, level6_value, level9_value, comment, created_by, created_at, priority, stats
        FROM datacron_requirements WHERE id = ? AND guild_id = ?
    """, (req_id, guild_id))
    row = cursor.fetchone()
    conn.close()
    return row


def get_datacron_requirements_by_set(set_id: int, guild_id: int = 1):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    _ensure_datacron_requirements_table(cursor)
    cursor.execute("""
        SELECT id, set_id, pack, level3_value, level6_value, level9_value, comment, created_by, created_at, priority, stats
        FROM datacron_requirements WHERE set_id = ? AND guild_id = ? ORDER BY id
    """, (set_id, guild_id))
    rows = cursor.fetchall()
    conn.close()
    return rows


def get_all_datacron_requirements(guild_id: int = 1):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    _ensure_datacron_requirements_table(cursor)
    cursor.execute("""
        SELECT id, set_id, pack, level3_value, level6_value, level9_value, comment, created_by, created_at, priority, stats
        FROM datacron_requirements WHERE guild_id = ? ORDER BY set_id, id
    """, (guild_id,))
    rows = cursor.fetchall()
    conn.close()
    return rows


# =====================================================================
# СТАТ-ТРЕБОВАНИЯ К ОБЫЧНЫМ ДАТАКРОНАМ: до 5 пар (stat_id из UnitStat-каталога,
# минимальный % — мягкая проверка "больше или равно"). Отдельная дочерняя таблица,
# а не колонка в datacron_requirements — на одно требование может быть до 5 строк.
# stat_id — числовой код (см. cogs/datacron_requirements.py::DATACRON_STAT_LABELS),
# смысл проверять/показывать имена берёт вызывающий код, здесь просто хранится пара.
# =====================================================================
def _ensure_datacron_requirement_stats_table(cursor):
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS datacron_requirement_stats (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            req_id INTEGER NOT NULL,
            guild_id INTEGER NOT NULL DEFAULT 1,
            stat_id INTEGER NOT NULL,
            min_value REAL NOT NULL
        )
    """)
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_datacron_req_stats_req ON datacron_requirement_stats(req_id, guild_id)")


def set_datacron_requirement_stats(req_id: int, stat_pairs, guild_id: int = 1):
    """Полностью заменяет набор стат-требований для req_id — удаляет старые строки и
    вставляет новые. stat_pairs: [(stat_id:int, min_value:float), ...], максимум 5
    (ограничение проверяет вызывающий код, здесь не дублируется)."""
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    _ensure_datacron_requirement_stats_table(cursor)
    cursor.execute("DELETE FROM datacron_requirement_stats WHERE req_id = ? AND guild_id = ?", (req_id, guild_id))
    cursor.executemany(
        "INSERT INTO datacron_requirement_stats (req_id, guild_id, stat_id, min_value) VALUES (?, ?, ?, ?)",
        [(req_id, guild_id, stat_id, min_value) for stat_id, min_value in stat_pairs],
    )
    conn.commit()
    conn.close()


def get_datacron_requirement_stats(req_id: int, guild_id: int = 1):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    _ensure_datacron_requirement_stats_table(cursor)
    cursor.execute(
        "SELECT stat_id, min_value FROM datacron_requirement_stats WHERE req_id = ? AND guild_id = ? ORDER BY id",
        (req_id, guild_id),
    )
    rows = cursor.fetchall()
    conn.close()
    return rows


def get_datacron_requirement_stats_by_set(set_id: int, guild_id: int = 1):
    """req_id -> [(stat_id, min_value), ...] для всех требований сезона одним запросом
    (без N+1) — используется /дк_требования список и проверить."""
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    _ensure_datacron_requirements_table(cursor)
    _ensure_datacron_requirement_stats_table(cursor)
    cursor.execute("""
        SELECT s.req_id, s.stat_id, s.min_value
        FROM datacron_requirement_stats s
        JOIN datacron_requirements r ON r.id = s.req_id AND r.guild_id = s.guild_id
        WHERE r.set_id = ? AND s.guild_id = ?
        ORDER BY s.req_id, s.id
    """, (set_id, guild_id))
    rows = cursor.fetchall()
    conn.close()
    result = {}
    for req_id, stat_id, min_value in rows:
        result.setdefault(req_id, []).append((stat_id, min_value))
    return result


# =====================================================================
# ТРЕБОВАНИЯ К СПЕЦ. (ФОКУСНЫМ) ДАТАКРОНАМ: персонаж + нужный уровень
# прокачки (1-9, у некоторых персонажей больше). У фокусных ДК нет случайных
# веток — бонус на каждом уровне фиксирован для конкретного персонажа.
# =====================================================================
def _ensure_datacron_focused_requirements_table(cursor):
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS datacron_focused_requirements (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            set_id INTEGER NOT NULL,
            character_key TEXT NOT NULL,
            required_level INTEGER NOT NULL,
            comment TEXT,
            created_by TEXT,
            created_at TEXT NOT NULL
        )
    """)
    try:
        cursor.execute("ALTER TABLE datacron_focused_requirements ADD COLUMN pack TEXT")
    except sqlite3.OperationalError:
        pass  # колонка уже добавлена ранее
    try:
        cursor.execute("ALTER TABLE datacron_focused_requirements ADD COLUMN priority TEXT NOT NULL DEFAULT 'required'")
    except sqlite3.OperationalError:
        pass  # колонка уже добавлена ранее
    try:
        cursor.execute("ALTER TABLE datacron_focused_requirements ADD COLUMN guild_id INTEGER NOT NULL DEFAULT 1")
    except sqlite3.OperationalError:
        pass  # колонка уже добавлена ранее
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_datacron_focused_req_guild_set ON datacron_focused_requirements(guild_id, set_id)"
    )


def add_datacron_focused_requirement(set_id: int, pack: str, character_key: str, required_level: int,
                                      comment: str, created_by: str, priority: str = "required", guild_id: int = 1) -> int:
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    _ensure_datacron_focused_requirements_table(cursor)
    cursor.execute("""
        INSERT INTO datacron_focused_requirements
            (set_id, pack, character_key, required_level, comment, created_by, created_at, priority, guild_id)
        VALUES (?, ?, ?, ?, ?, ?, datetime('now'), ?, ?)
    """, (set_id, pack, character_key, required_level, comment, created_by, priority, guild_id))
    conn.commit()
    req_id = cursor.lastrowid
    conn.close()
    return req_id


def update_datacron_focused_requirement(req_id: int, set_id: int, pack: str, character_key: str,
                                         required_level: int, comment: str, priority: str, guild_id: int = 1) -> bool:
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    _ensure_datacron_focused_requirements_table(cursor)
    cursor.execute("""
        UPDATE datacron_focused_requirements
        SET set_id = ?, pack = ?, character_key = ?, required_level = ?, comment = ?, priority = ?
        WHERE id = ? AND guild_id = ?
    """, (set_id, pack, character_key, required_level, comment, priority, req_id, guild_id))
    conn.commit()
    updated = cursor.rowcount > 0
    conn.close()
    return updated


def delete_datacron_focused_requirement(req_id: int, guild_id: int = 1) -> bool:
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    _ensure_datacron_focused_requirements_table(cursor)
    cursor.execute("DELETE FROM datacron_focused_requirements WHERE id = ? AND guild_id = ?", (req_id, guild_id))
    conn.commit()
    deleted = cursor.rowcount > 0
    conn.close()
    return deleted


def delete_datacron_focused_requirements_by_set(set_id: int, guild_id: int = 1) -> int:
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    _ensure_datacron_focused_requirements_table(cursor)
    cursor.execute("DELETE FROM datacron_focused_requirements WHERE set_id = ? AND guild_id = ?", (set_id, guild_id))
    conn.commit()
    deleted = cursor.rowcount
    conn.close()
    return deleted


def count_datacron_focused_requirements_by_set(set_id: int, guild_id: int = 1) -> int:
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    _ensure_datacron_focused_requirements_table(cursor)
    cursor.execute("SELECT COUNT(*) FROM datacron_focused_requirements WHERE set_id = ? AND guild_id = ?", (set_id, guild_id))
    count = cursor.fetchone()[0]
    conn.close()
    return count


def get_datacron_focused_requirement(req_id: int, guild_id: int = 1):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    _ensure_datacron_focused_requirements_table(cursor)
    cursor.execute("""
        SELECT id, set_id, pack, character_key, required_level, comment, created_by, created_at, priority
        FROM datacron_focused_requirements WHERE id = ? AND guild_id = ?
    """, (req_id, guild_id))
    row = cursor.fetchone()
    conn.close()
    return row


def get_datacron_focused_requirements_by_set(set_id: int, guild_id: int = 1):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    _ensure_datacron_focused_requirements_table(cursor)
    cursor.execute("""
        SELECT id, set_id, pack, character_key, required_level, comment, created_by, created_at, priority
        FROM datacron_focused_requirements WHERE set_id = ? AND guild_id = ? ORDER BY id
    """, (set_id, guild_id))
    rows = cursor.fetchall()
    conn.close()
    return rows


def get_all_datacron_focused_requirements(guild_id: int = 1):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    _ensure_datacron_focused_requirements_table(cursor)
    cursor.execute("""
        SELECT id, set_id, pack, character_key, required_level, comment, created_by, created_at, priority
        FROM datacron_focused_requirements WHERE guild_id = ? ORDER BY set_id, id
    """, (guild_id,))
    rows = cursor.fetchall()
    conn.close()
    return rows


# =====================================================================
# ТРЕБОВАНИЯ К СТАТАМ: пороги по HotUtils-плейтам (плейт + персонаж + стат
# + порог). 'Relic' — специальное значение stat_name для требования к уровню
# реликвии самому по себе (используется /статы как целевой релик для прогноза).
# =====================================================================
def _ensure_stat_requirements_table(cursor):
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS stat_requirements (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            plate_name TEXT NOT NULL,
            character_key TEXT NOT NULL,
            stat_name TEXT NOT NULL,
            operator TEXT NOT NULL,
            threshold_value REAL NOT NULL,
            priority TEXT NOT NULL DEFAULT 'required',
            raw_text TEXT,
            comment TEXT,
            created_by TEXT,
            created_at TEXT NOT NULL
        )
    """)
    try:
        cursor.execute("ALTER TABLE stat_requirements ADD COLUMN guild_id INTEGER NOT NULL DEFAULT 1")
    except sqlite3.OperationalError:
        pass  # колонка уже добавлена ранее
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_stat_req_guild_plate_char ON stat_requirements(guild_id, plate_name, character_key)"
    )


def _ensure_stat_plates_table(cursor):
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS stat_plates (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id INTEGER NOT NULL DEFAULT 1,
            name TEXT NOT NULL,
            description TEXT,
            created_by TEXT,
            created_at TEXT NOT NULL,
            UNIQUE (guild_id, name)
        )
    """)
    # Миграция с версии до мультитенантности (UNIQUE был только по name, без guild_id).
    cursor.execute("PRAGMA table_info(stat_plates)")
    cols = {row[1] for row in cursor.fetchall()}
    if cols and "guild_id" not in cols:
        cursor.execute("ALTER TABLE stat_plates RENAME TO stat_plates_old")
        cursor.execute("""
            CREATE TABLE stat_plates (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL DEFAULT 1,
                name TEXT NOT NULL,
                description TEXT,
                created_by TEXT,
                created_at TEXT NOT NULL,
                UNIQUE (guild_id, name)
            )
        """)
        cursor.execute("""
            INSERT INTO stat_plates (guild_id, name, description, created_by, created_at)
            SELECT 1, name, description, created_by, created_at FROM stat_plates_old
        """)
        cursor.execute("DROP TABLE stat_plates_old")
    # Плейты изначально создавались неявно — первым /статы_требования добавить с новым
    # именем. Подтягиваем такие "исторические" плейты в таблицу, иначе они не попадут
    # в список/переименование/удаление, хоть и продолжат работать в автодополнении.
    # (stat_requirements уже должна быть мигрирована на guild_id к этому моменту —
    # см. порядок вызовов _ensure_stat_requirements_table перед _ensure_stat_plates_table.)
    cursor.execute("""
        INSERT OR IGNORE INTO stat_plates (guild_id, name, description, created_by, created_at)
        SELECT DISTINCT guild_id, plate_name, NULL, NULL, datetime('now') FROM stat_requirements
    """)
    # КРИТИЧНО: оба INSERT выше — DML, не DDL. Большинство вызывающих функций здесь
    # read-only и не делают conn.commit() перед conn.close() — без явного commit тут
    # эти строки тихо откатываются при закрытии соединения (проверено эмпирически:
    # DROP TABLE — DDL и не коммитит предыдущий INSERT сам по себе). Коммитим прямо
    # внутри миграции, не полагаясь на вызывающую функцию — см. тот же фикс в
    # _ensure_user_registration_table.
    cursor.connection.commit()


def create_stat_plate(name: str, description: str, created_by: str, guild_id: int = 1) -> bool:
    """Возвращает False, если плейт с таким именем уже существует в этой гильдии."""
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    _ensure_stat_requirements_table(cursor)
    _ensure_stat_plates_table(cursor)
    try:
        cursor.execute(
            "INSERT INTO stat_plates (guild_id, name, description, created_by, created_at) VALUES (?, ?, ?, ?, datetime('now'))",
            (guild_id, name, description, created_by),
        )
        conn.commit()
        return True
    except sqlite3.IntegrityError:
        return False
    finally:
        conn.close()


def rename_stat_plate(old_name: str, new_name: str, guild_id: int = 1) -> bool:
    """Возвращает False, если old_name не найден либо new_name уже занят другим плейтом этой гильдии."""
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    _ensure_stat_requirements_table(cursor)
    _ensure_stat_plates_table(cursor)
    try:
        cursor.execute("UPDATE stat_plates SET name = ? WHERE guild_id = ? AND name = ?", (new_name, guild_id, old_name))
        if cursor.rowcount == 0:
            conn.rollback()
            return False
        cursor.execute(
            "UPDATE stat_requirements SET plate_name = ? WHERE guild_id = ? AND plate_name = ?",
            (new_name, guild_id, old_name)
        )
        conn.commit()
        return True
    except sqlite3.IntegrityError:
        conn.rollback()
        return False
    finally:
        conn.close()


def count_stat_requirements_by_plate(plate_name: str, guild_id: int = 1) -> int:
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    _ensure_stat_requirements_table(cursor)
    cursor.execute("SELECT COUNT(*) FROM stat_requirements WHERE guild_id = ? AND plate_name = ?", (guild_id, plate_name))
    count = cursor.fetchone()[0]
    conn.close()
    return count


def count_stat_requirements_by_character(plate_name: str, character_key: str, guild_id: int = 1) -> int:
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    _ensure_stat_requirements_table(cursor)
    cursor.execute(
        "SELECT COUNT(*) FROM stat_requirements WHERE guild_id = ? AND plate_name = ? AND character_key = ?",
        (guild_id, plate_name, character_key),
    )
    count = cursor.fetchone()[0]
    conn.close()
    return count


def delete_stat_requirements_by_character(plate_name: str, character_key: str, guild_id: int = 1) -> int:
    """Удаляет все требования одного персонажа в плейте (сам плейт остаётся, даже если
    после этого в нём не остаётся ни одного персонажа), возвращает количество удалённых."""
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    _ensure_stat_requirements_table(cursor)
    cursor.execute(
        "DELETE FROM stat_requirements WHERE guild_id = ? AND plate_name = ? AND character_key = ?",
        (guild_id, plate_name, character_key),
    )
    deleted = cursor.rowcount
    conn.commit()
    conn.close()
    return deleted


def delete_stat_plate(name: str, guild_id: int = 1) -> int:
    """Удаляет плейт и все его требования (в пределах гильдии), возвращает количество удалённых требований."""
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    _ensure_stat_requirements_table(cursor)
    _ensure_stat_plates_table(cursor)
    cursor.execute("DELETE FROM stat_requirements WHERE guild_id = ? AND plate_name = ?", (guild_id, name))
    deleted = cursor.rowcount
    cursor.execute("DELETE FROM stat_plates WHERE guild_id = ? AND name = ?", (guild_id, name))
    conn.commit()
    conn.close()
    return deleted


def get_stat_plate(name: str, guild_id: int = 1):
    """Возвращает (name, description, created_by, created_at) либо None."""
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    _ensure_stat_requirements_table(cursor)
    _ensure_stat_plates_table(cursor)
    cursor.execute(
        "SELECT name, description, created_by, created_at FROM stat_plates WHERE guild_id = ? AND name = ?",
        (guild_id, name)
    )
    row = cursor.fetchone()
    conn.close()
    return row


def get_all_stat_plates_detailed(guild_id: int = 1):
    """Возвращает список (name, description, character_count, requirement_count) по всем плейтам
    гильдии, включая пустые (без единого требования), отсортированный по имени."""
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    _ensure_stat_requirements_table(cursor)
    _ensure_stat_plates_table(cursor)
    cursor.execute("""
        SELECT p.name, p.description,
               COUNT(DISTINCT r.character_key) AS char_count,
               COUNT(r.id) AS req_count
        FROM stat_plates p
        LEFT JOIN stat_requirements r ON r.plate_name = p.name AND r.guild_id = p.guild_id
        WHERE p.guild_id = ?
        GROUP BY p.name
        ORDER BY p.name
    """, (guild_id,))
    rows = cursor.fetchall()
    conn.close()
    return rows


def add_stat_requirement(plate_name: str, character_key: str, stat_name: str, operator: str,
                          threshold_value: float, priority: str, raw_text: str, comment: str,
                          created_by: str, guild_id: int = 1) -> int:
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    _ensure_stat_requirements_table(cursor)
    cursor.execute("""
        INSERT INTO stat_requirements
            (plate_name, character_key, stat_name, operator, threshold_value, priority, raw_text, comment, created_by, created_at, guild_id)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'), ?)
    """, (plate_name, character_key, stat_name, operator, threshold_value, priority, raw_text, comment, created_by, guild_id))
    conn.commit()
    req_id = cursor.lastrowid
    conn.close()
    return req_id


def update_stat_requirement(req_id: int, plate_name: str, character_key: str, stat_name: str, operator: str,
                             threshold_value: float, priority: str, comment: str, guild_id: int = 1) -> bool:
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    _ensure_stat_requirements_table(cursor)
    cursor.execute("""
        UPDATE stat_requirements
        SET plate_name = ?, character_key = ?, stat_name = ?, operator = ?, threshold_value = ?, priority = ?, comment = ?
        WHERE id = ? AND guild_id = ?
    """, (plate_name, character_key, stat_name, operator, threshold_value, priority, comment, req_id, guild_id))
    conn.commit()
    updated = cursor.rowcount > 0
    conn.close()
    return updated


def delete_stat_requirement(req_id: int, guild_id: int = 1) -> bool:
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    _ensure_stat_requirements_table(cursor)
    cursor.execute("DELETE FROM stat_requirements WHERE id = ? AND guild_id = ?", (req_id, guild_id))
    conn.commit()
    deleted = cursor.rowcount > 0
    conn.close()
    return deleted


def get_stat_requirement(req_id: int, guild_id: int = 1):
    """Требование по id — только если оно принадлежит указанной гильдии (предотвращает
    просмотр/редактирование чужого требования по угаданному числовому id)."""
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    _ensure_stat_requirements_table(cursor)
    cursor.execute("""
        SELECT id, plate_name, character_key, stat_name, operator, threshold_value, priority, raw_text, comment, created_by, created_at
        FROM stat_requirements WHERE id = ? AND guild_id = ?
    """, (req_id, guild_id))
    row = cursor.fetchone()
    conn.close()
    return row


def get_stat_requirements(plate_name: str, character_key: str, guild_id: int = 1):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    _ensure_stat_requirements_table(cursor)
    cursor.execute("""
        SELECT id, plate_name, character_key, stat_name, operator, threshold_value, priority, raw_text, comment, created_by, created_at
        FROM stat_requirements WHERE guild_id = ? AND plate_name = ? AND character_key = ? ORDER BY id
    """, (guild_id, plate_name, character_key))
    rows = cursor.fetchall()
    conn.close()
    return rows


def get_all_stat_requirement_plates(guild_id: int = 1):
    """Все известные плейты гильдии — и зарегистрированные через /статы_требования создать
    (даже пустые), и "исторические" (существующие только как plate_name у требований)."""
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    _ensure_stat_requirements_table(cursor)
    _ensure_stat_plates_table(cursor)
    cursor.execute("""
        SELECT name FROM stat_plates WHERE guild_id = ?
        UNION
        SELECT DISTINCT plate_name FROM stat_requirements WHERE guild_id = ?
        ORDER BY 1
    """, (guild_id, guild_id))
    rows = [r[0] for r in cursor.fetchall()]
    conn.close()
    return rows


def get_stat_requirement_characters(plate_name: str, guild_id: int = 1):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    _ensure_stat_requirements_table(cursor)
    cursor.execute(
        "SELECT DISTINCT character_key FROM stat_requirements WHERE guild_id = ? AND plate_name = ? ORDER BY character_key",
        (guild_id, plate_name)
    )
    rows = [r[0] for r in cursor.fetchall()]
    conn.close()
    return rows


def get_all_stat_requirements(guild_id: int = 1):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    _ensure_stat_requirements_table(cursor)
    cursor.execute("""
        SELECT id, plate_name, character_key, stat_name, operator, threshold_value, priority, raw_text, comment, created_by, created_at
        FROM stat_requirements WHERE guild_id = ? ORDER BY plate_name, character_key, id
    """, (guild_id,))
    rows = cursor.fetchall()
    conn.close()
    return rows


# =====================================================================
# КОНСТРУКТОР ГИПОТЕТИЧЕСКИХ СБОРОК МОДОВ (веб-only, /mod-builder): пресеты
# (сет + вручную заданные суммы статов от модов, переиспользуемые на любом персонаже)
# и история последних расчётов. sets_json/stats_json хранятся как есть (JSON-блоб в
# одной строке, как unit_json в player_unit_cache ниже) — сетов/статов мало, отдельные
# дочерние таблицы дали бы только лишние join'ы и каскадные удаления без пользы.
# =====================================================================
def _ensure_stat_mod_presets_table(cursor):
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS stat_mod_presets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id INTEGER NOT NULL DEFAULT 1,
            name TEXT NOT NULL,
            sets_json TEXT NOT NULL,
            stats_json TEXT NOT NULL,
            created_by TEXT,
            created_at TEXT NOT NULL,
            UNIQUE (guild_id, name)
        )
    """)
    try:
        # primaries_json: {slot_key: unit_stat_id} — какой primary-стат выбран на каждом
        # из 6 слотов (см. stat_engine.MOD_PRIMARY_OPTIONS). Добавлено отдельной колонкой
        # позже sets_json/stats_json — тот же ALTER-паттерн, что и везде в этом файле.
        cursor.execute("ALTER TABLE stat_mod_presets ADD COLUMN primaries_json TEXT NOT NULL DEFAULT '{}'")
    except sqlite3.OperationalError:
        pass  # колонка уже добавлена ранее


def create_stat_mod_preset(name: str, sets: dict, primaries: dict, stats: dict, created_by: str, guild_id: int = 1) -> bool:
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    _ensure_stat_mod_presets_table(cursor)
    try:
        cursor.execute(
            "INSERT INTO stat_mod_presets (guild_id, name, sets_json, primaries_json, stats_json, created_by, created_at) VALUES (?, ?, ?, ?, ?, ?, datetime('now'))",
            (guild_id, name, json.dumps(sets), json.dumps(primaries), json.dumps(stats), created_by),
        )
        conn.commit()
        return True
    except sqlite3.IntegrityError:
        return False
    finally:
        conn.close()


def get_all_stat_mod_presets(guild_id: int = 1):
    """Возвращает [(id, name, sets, primaries, stats, created_by, created_at), ...] отсортированные
    по имени, sets/primaries/stats уже разобраны из JSON."""
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    _ensure_stat_mod_presets_table(cursor)
    cursor.execute(
        "SELECT id, name, sets_json, primaries_json, stats_json, created_by, created_at FROM stat_mod_presets WHERE guild_id = ? ORDER BY name",
        (guild_id,)
    )
    rows = cursor.fetchall()
    conn.close()
    return [(pid, name, json.loads(sets_json), json.loads(primaries_json), json.loads(stats_json), created_by, created_at)
            for pid, name, sets_json, primaries_json, stats_json, created_by, created_at in rows]


def get_stat_mod_preset(preset_id: int, guild_id: int = 1):
    """Возвращает (id, name, sets, primaries, stats, created_by, created_at) либо None."""
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    _ensure_stat_mod_presets_table(cursor)
    cursor.execute(
        "SELECT id, name, sets_json, primaries_json, stats_json, created_by, created_at FROM stat_mod_presets WHERE id = ? AND guild_id = ?",
        (preset_id, guild_id)
    )
    row = cursor.fetchone()
    conn.close()
    if not row:
        return None
    pid, name, sets_json, primaries_json, stats_json, created_by, created_at = row
    return pid, name, json.loads(sets_json), json.loads(primaries_json), json.loads(stats_json), created_by, created_at


def delete_stat_mod_preset(preset_id: int, guild_id: int = 1) -> bool:
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    _ensure_stat_mod_presets_table(cursor)
    cursor.execute("DELETE FROM stat_mod_presets WHERE id = ? AND guild_id = ?", (preset_id, guild_id))
    conn.commit()
    deleted = cursor.rowcount > 0
    conn.close()
    return deleted


STAT_HYPOTHETICAL_HISTORY_KEEP = 20


def _ensure_stat_hypothetical_history_table(cursor):
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS stat_hypothetical_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id INTEGER NOT NULL DEFAULT 1,
            character_key TEXT NOT NULL,
            relic INTEGER NOT NULL,
            sets_json TEXT NOT NULL,
            stats_json TEXT NOT NULL,
            created_by TEXT,
            created_at TEXT NOT NULL
        )
    """)
    try:
        cursor.execute("ALTER TABLE stat_hypothetical_history ADD COLUMN primaries_json TEXT NOT NULL DEFAULT '{}'")
    except sqlite3.OperationalError:
        pass  # колонка уже добавлена ранее
    try:
        # DEFAULT 7 — старые записи, до появления настраиваемой звёздности, всегда считались
        # на 7★ (см. stat_engine.build_hypothetical_unit до 2026-08-24).
        cursor.execute("ALTER TABLE stat_hypothetical_history ADD COLUMN rarity INTEGER NOT NULL DEFAULT 7")
    except sqlite3.OperationalError:
        pass  # колонка уже добавлена ранее


def add_stat_hypothetical_history(character_key: str, relic: int, sets: dict, primaries: dict, stats: dict, created_by: str,
                                   guild_id: int = 1, keep: int = STAT_HYPOTHETICAL_HISTORY_KEEP, rarity: int = 7):
    """Пишет запись и сразу обрезает историю гильдии до последних `keep` (тот же приём,
    что prune_tb_events, но без дочерних таблиц — тут нечего каскадно удалять)."""
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    _ensure_stat_hypothetical_history_table(cursor)
    cursor.execute(
        "INSERT INTO stat_hypothetical_history (guild_id, character_key, relic, sets_json, primaries_json, stats_json, created_by, created_at, rarity) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, datetime('now'), ?)",
        (guild_id, character_key, relic, json.dumps(sets), json.dumps(primaries), json.dumps(stats), created_by, rarity),
    )
    cursor.execute("SELECT id FROM stat_hypothetical_history WHERE guild_id = ? ORDER BY id DESC LIMIT -1 OFFSET ?", (guild_id, keep))
    old_ids = [r[0] for r in cursor.fetchall()]
    if old_ids:
        placeholders = ",".join("?" * len(old_ids))
        cursor.execute(f"DELETE FROM stat_hypothetical_history WHERE id IN ({placeholders})", old_ids)
    conn.commit()
    conn.close()


def get_stat_hypothetical_history(guild_id: int = 1, limit: int = STAT_HYPOTHETICAL_HISTORY_KEEP):
    """Возвращает [(id, character_key, relic, sets, primaries, stats, created_by, created_at, rarity), ...]
    от новых к старым, sets/primaries/stats уже разобраны из JSON."""
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    _ensure_stat_hypothetical_history_table(cursor)
    cursor.execute(
        "SELECT id, character_key, relic, sets_json, primaries_json, stats_json, created_by, created_at, rarity "
        "FROM stat_hypothetical_history WHERE guild_id = ? ORDER BY id DESC LIMIT ?",
        (guild_id, limit)
    )
    rows = cursor.fetchall()
    conn.close()
    return [(hid, char_key, relic, json.loads(sets_json), json.loads(primaries_json), json.loads(stats_json), created_by, created_at, rarity)
            for hid, char_key, relic, sets_json, primaries_json, stats_json, created_by, created_at, rarity in rows]


def get_stat_hypothetical_history_entry(history_id: int, guild_id: int = 1):
    """Возвращает (id, character_key, relic, sets, primaries, stats, created_by, created_at, rarity) либо None."""
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    _ensure_stat_hypothetical_history_table(cursor)
    cursor.execute(
        "SELECT id, character_key, relic, sets_json, primaries_json, stats_json, created_by, created_at, rarity "
        "FROM stat_hypothetical_history WHERE id = ? AND guild_id = ?",
        (history_id, guild_id)
    )
    row = cursor.fetchone()
    conn.close()
    if not row:
        return None
    hid, char_key, relic, sets_json, primaries_json, stats_json, created_by, created_at, rarity = row
    return hid, char_key, relic, json.loads(sets_json), json.loads(primaries_json), json.loads(stats_json), created_by, created_at, rarity


# =====================================================================
# КЭШ ЮНИТОВ ИГРОКОВ: сырой rosterUnit из comlink.get_player (для локального
# расчёта статов через StatCalc — хранится как есть, без разбора по колонкам,
# т.к. StatCalc.calc_char_stats принимает этот формат напрямую, см. cogs/stat_engine.py).
# =====================================================================
def _ensure_player_unit_cache_table(cursor):
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS player_unit_cache (
            ally_code TEXT NOT NULL,
            base_id TEXT NOT NULL,
            unit_json TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (ally_code, base_id)
        )
    """)


def upsert_player_units(ally_code: str, units: dict):
    """units: {base_id: сырой rosterUnit-словарь из comlink.get_player}"""
    if not units:
        return
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    _ensure_player_unit_cache_table(cursor)
    rows = [(ally_code, base_id, json.dumps(unit_dict)) for base_id, unit_dict in units.items()]
    cursor.executemany("""
        INSERT OR REPLACE INTO player_unit_cache (ally_code, base_id, unit_json, updated_at)
        VALUES (?, ?, ?, datetime('now'))
    """, rows)
    conn.commit()
    conn.close()


def get_player_unit(ally_code: str, base_id: str):
    """Возвращает (unit_dict, updated_at) либо None, если юнита нет в кэше."""
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    _ensure_player_unit_cache_table(cursor)
    cursor.execute(
        "SELECT unit_json, updated_at FROM player_unit_cache WHERE ally_code = ? AND base_id = ?",
        (ally_code, base_id)
    )
    row = cursor.fetchone()
    conn.close()
    if not row:
        return None
    return json.loads(row[0]), row[1]


def get_player_units(ally_code: str) -> dict:
    """base_id -> сырой rosterUnit словарь, весь закэшированный ростер игрока разом
    (для диффа активности в player_units_sync_loop — см. services/activity_diff.py)."""
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    _ensure_player_unit_cache_table(cursor)
    cursor.execute("SELECT base_id, unit_json FROM player_unit_cache WHERE ally_code = ?", (ally_code,))
    rows = cursor.fetchall()
    conn.close()
    return {base_id: json.loads(unit_json) for base_id, unit_json in rows}


def get_player_units_last_sync(ally_codes: list) -> str | None:
    """MAX(updated_at) по кэшу для набора ally_code — "когда последний раз обновлялся
    ростер этой гильдии" для панели статуса на /activity. Не разделяет ручной/авто-синк —
    оба пишут через upsert_player_units одинаково."""
    if not ally_codes:
        return None
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    _ensure_player_unit_cache_table(cursor)
    placeholders = ",".join("?" for _ in ally_codes)
    cursor.execute(
        f"SELECT MAX(updated_at) FROM player_unit_cache WHERE ally_code IN ({placeholders})",
        ally_codes,
    )
    row = cursor.fetchone()
    conn.close()
    return row[0] if row and row[0] else None


def get_player_unit_owners(base_id: str) -> list[dict]:
    """Обратный запрос к get_player_units — все ally_code, у кого есть этот base_id, с сырым
    unit_json (для извлечения реликвии через stat_engine.get_current_relic_level на
    вызывающей стороне — держим stat_engine вне database.py, см. web/routes/stat_builder.py
    за прецедентом такого разделения). Один base_id за раз — для страницы целиком (много
    разных юнитов сразу) см. get_player_unit_owners_bulk ниже."""
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    _ensure_player_unit_cache_table(cursor)
    cursor.execute("SELECT ally_code, unit_json FROM player_unit_cache WHERE base_id = ?", (base_id,))
    rows = cursor.fetchall()
    conn.close()
    return [{"ally_code": ally_code, "unit": json.loads(unit_json)} for ally_code, unit_json in rows]


def get_player_unit_owners_bulk(ally_codes: list, base_ids: list) -> list[dict]:
    """Как get_player_unit_owners, но одним запросом сразу на несколько base_id и
    ограниченным набором ally_code (ростер гильдии) — используется конструктором взводов
    ТБ (web/routes/guild_dashboard.py::tb_platoons), где на одной странице может быть до
    ~350 юнит-слотов с массой повторов, и по одному запросу на base_id было бы N+1."""
    if not ally_codes or not base_ids:
        return []
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    _ensure_player_unit_cache_table(cursor)
    ally_placeholders = ",".join("?" for _ in ally_codes)
    base_placeholders = ",".join("?" for _ in base_ids)
    cursor.execute(f"""
        SELECT ally_code, base_id, unit_json FROM player_unit_cache
        WHERE ally_code IN ({ally_placeholders}) AND base_id IN ({base_placeholders})
    """, [*ally_codes, *base_ids])
    rows = cursor.fetchall()
    conn.close()
    return [
        {"ally_code": ally_code, "base_id": base_id, "unit": json.loads(unit_json)}
        for ally_code, base_id, unit_json in rows
    ]


def set_omicron_capable_base_ids(base_ids) -> None:
    """Перезаписывает game_units.has_omicron целиком по актуальному набору base_id —
    вызывается из services/units_sync.py::sync_units на каждом цикле синхронизации
    справочника юнитов (при старте и раз в час, тот же цикл, что обновляет имена).
    Раньше "есть ли омикрон" считалось по player_unit_cache (кто в гильдии уже
    разблокировал) — заменено на игровой каталог (Comlink SkillDefinitions), т.к.
    персонаж может иметь омикрон в игре, даже если в конкретной гильдии его никто
    ещё не выдал (см. обсуждение в разговоре 2026-08-19)."""
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("UPDATE game_units SET has_omicron = 0")
    if base_ids:
        placeholders = ", ".join("?" for _ in base_ids)
        cursor.execute(f"UPDATE game_units SET has_omicron = 1 WHERE base_id IN ({placeholders})", list(base_ids))
    conn.commit()
    conn.close()


def get_all_omicron_capable_units() -> list:
    """[(base_id, name), ...] — все персонажи/корабли, у которых В ИГРЕ есть омикрон,
    по алфавиту отображаемого имени. Используется для фильтрации автокомплита в
    cogs/stat_requirements.py (/омикрон_текст фраза) и web/routes/admin.py
    (/omicron/phrases) — не весь справочник game_units, где омикрона у
    большинства юнитов нет вообще."""
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("SELECT base_id, cached_name FROM game_units WHERE has_omicron = 1 ORDER BY cached_name")
    rows = cursor.fetchall()
    conn.close()
    return rows


def _ensure_unit_omicron_skills_table(cursor):
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS unit_omicron_skills (
            base_id TEXT NOT NULL,
            skill_id TEXT NOT NULL,
            PRIMARY KEY (base_id, skill_id)
        )
    """)


def set_unit_omicron_skills(mapping: dict) -> None:
    """Перезаписывает unit_omicron_skills целиком: {base_id: [skill_id, ...]}. Те же
    исходные данные (comlink SkillDefinitions.tier[].isOmicronTier + unit.skillReference),
    что уже дают game_units.has_omicron (см. set_omicron_capable_base_ids), но здесь —
    какие именно skill_id, а не просто факт "есть омикрон": персонаж может иметь больше
    одного омикрона, /omicron/phrases показывает и позволяет задать фразу на
    каждый из них по отдельности. Вызывается из того же цикла sync_units
    (services/units_sync.py), что и set_omicron_capable_base_ids/set_skill_tier_thresholds —
    без лишних запросов к Comlink."""
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    _ensure_unit_omicron_skills_table(cursor)
    cursor.execute("DELETE FROM unit_omicron_skills")
    rows = [(base_id, skill_id) for base_id, skill_ids in mapping.items() for skill_id in skill_ids]
    if rows:
        cursor.executemany("INSERT OR IGNORE INTO unit_omicron_skills (base_id, skill_id) VALUES (?, ?)", rows)
    conn.commit()
    conn.close()


def get_all_unit_omicron_skills() -> dict:
    """{base_id: [skill_id, ...]} — весь справочник разом, для отображения "персонаж +
    все его омикроны" на /omicron/phrases (см. set_unit_omicron_skills)."""
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    _ensure_unit_omicron_skills_table(cursor)
    cursor.execute("SELECT base_id, skill_id FROM unit_omicron_skills")
    rows = cursor.fetchall()
    conn.close()
    result = {}
    for base_id, skill_id in rows:
        result.setdefault(base_id, []).append(skill_id)
    return result


def _ensure_skill_tier_thresholds_table(cursor):
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS skill_tier_thresholds (
            skill_id TEXT PRIMARY KEY,
            zeta_tier INTEGER,
            omicron_tier INTEGER,
            name TEXT,
            ability_id TEXT
        )
    """)
    # name/ability_id добавлены 2026-08-24 (человекочитаемые имена зет/омикронов + ссылка
    # на swgoh.gg на /activity), ability_type/omicron_mode — 2026-08-25 (тип способности +
    # игровой режим для текста объявления о выдаче омикрона) — ALTER для баз, где таблица
    # уже была создана без них.
    for column in ("name TEXT", "ability_id TEXT", "ability_type TEXT", "omicron_mode TEXT"):
        try:
            cursor.execute(f"ALTER TABLE skill_tier_thresholds ADD COLUMN {column}")
        except sqlite3.OperationalError:
            pass


def set_skill_tier_thresholds(thresholds: dict) -> None:
    """thresholds: {skill_id: (zeta_tier|None, omicron_tier|None, name, ability_id, ability_type,
    omicron_mode)} — zeta_tier/omicron_tier: индекс (0-based, тот же, что player
    rosterUnit.skill[].tier) ступени способности, помеченной isZetaTier/isOmicronTier=True в
    comlink SkillDefinitions. Число ступеней и позиция зета/омикрона свои у каждой способности
    (подтверждено живыми данными 2026-08-21) — единого порога вроде "tier >= 8" не существует,
    поэтому services/activity_diff.py сравнивает per-skill, а не с константой.
    name/ability_id — человекочитаемое имя способности и её id в каталоге AbilityDefinitions
    (используется как слаг ссылки на swgoh.gg); ability_type/omicron_mode — "Лидерка"/"Базовая"/
    "Особая"/"Уникальная N" и режим игры ("ВГ"/"ТБ"/"рейд"/"ВА"/...), только для омикронов —
    см. services/units_sync.py::_skill_tier_thresholds. Перезаписывается целиком раз в час из
    services/units_sync.py::sync_units (тот же цикл, что game_units.has_omicron)."""
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    _ensure_skill_tier_thresholds_table(cursor)
    cursor.execute("DELETE FROM skill_tier_thresholds")
    if thresholds:
        rows = [
            (skill_id, zeta_tier, omicron_tier, name, ability_id, ability_type, omicron_mode)
            for skill_id, (zeta_tier, omicron_tier, name, ability_id, ability_type, omicron_mode) in thresholds.items()
        ]
        cursor.executemany(
            "INSERT INTO skill_tier_thresholds "
            "(skill_id, zeta_tier, omicron_tier, name, ability_id, ability_type, omicron_mode) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            rows,
        )
    conn.commit()
    conn.close()


def get_all_skill_tier_thresholds() -> dict:
    """{skill_id: (zeta_tier|None, omicron_tier|None)}, весь справочник разом — грузится один
    раз за цикл синка (не по разу на игрока), см. cogs/stat_requirements.py::player_units_sync_loop
    и web/routes/guild_dashboard.py::activity_sync."""
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    _ensure_skill_tier_thresholds_table(cursor)
    cursor.execute("SELECT skill_id, zeta_tier, omicron_tier FROM skill_tier_thresholds")
    rows = cursor.fetchall()
    conn.close()
    return {skill_id: (zeta_tier, omicron_tier) for skill_id, zeta_tier, omicron_tier in rows}


def get_skill_display_info(skill_ids: list[str]) -> dict:
    """{skill_id: (name, ability_id, ability_type, omicron_mode)} для отображения зет/омикронов
    на /activity и в Discord-объявлениях о выдаче омикрона — имя способности вместо сырого
    skill_id + ability_id для ссылки на swgoh.gg
    (https://swgoh.gg/units/{base_id}/ability/{ability_id}/1/) + тип способности
    ("Лидерка"/"Базовая"/"Особая"/"Уникальная N") и игровой режим омикрона ("ВГ"/"ТБ"/...,
    пусто для зет — см. services/units_sync.py::_skill_tier_thresholds). См.
    services/dashboard_data.py::get_guild_activity и cogs/stat_requirements.py::_announce_omicrons."""
    if not skill_ids:
        return {}
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    _ensure_skill_tier_thresholds_table(cursor)
    placeholders = ",".join("?" * len(skill_ids))
    cursor.execute(
        f"SELECT skill_id, name, ability_id, ability_type, omicron_mode "
        f"FROM skill_tier_thresholds WHERE skill_id IN ({placeholders})",
        skill_ids,
    )
    rows = cursor.fetchall()
    conn.close()
    return {skill_id: (name, ability_id, ability_type, omicron_mode) for skill_id, name, ability_id, ability_type, omicron_mode in rows}


# =====================================================================
# ПРИОРИТЕТ ОМИКРОНОВ ДЛЯ ВГ (веб-конструктор /omicrons/priority) — см. план "Приоритеты
# омикронов для ВГ" (~/.claude/plans/lively-noodling-moler.md). Не хранит имя юнита/омикрона,
# только ссылку на skill_id из глобального каталога skill_tier_thresholds — имя резолвится на
# чтении через unit_omicron_skills/skill_tier_thresholds/game_units (см. get_skill_display_info,
# get_all_unit_omicron_skills выше). Требования — по образцу tb_platoon_filter_rules ниже.
# =====================================================================
def _ensure_guild_omicron_priority_table(cursor):
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS guild_omicron_priority (
            guild_id INTEGER NOT NULL,
            skill_id TEXT NOT NULL,
            priority INTEGER NOT NULL,
            PRIMARY KEY (guild_id, skill_id)
        )
    """)


def get_guild_omicron_priority(guild_id: int) -> list:
    """[(skill_id, priority), ...] по возрастанию priority (0 — самый важный)."""
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    _ensure_guild_omicron_priority_table(cursor)
    cursor.execute(
        "SELECT skill_id, priority FROM guild_omicron_priority WHERE guild_id = ? ORDER BY priority",
        (guild_id,),
    )
    rows = cursor.fetchall()
    conn.close()
    return rows


def set_guild_omicron_priority(guild_id: int, ordered_skill_ids: list) -> None:
    """Перезаписывает весь приоритетный список гильдии целиком (порядок в списке = приоритет) —
    так же просто, как set_unit_omicron_skills/set_skill_tier_thresholds перезаписывают
    справочники целиком, вместо построчного CRUD (список маленький, десятки записей)."""
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    _ensure_guild_omicron_priority_table(cursor)
    cursor.execute("DELETE FROM guild_omicron_priority WHERE guild_id = ?", (guild_id,))
    if ordered_skill_ids:
        rows = [(guild_id, skill_id, priority) for priority, skill_id in enumerate(ordered_skill_ids)]
        cursor.executemany(
            "INSERT INTO guild_omicron_priority (guild_id, skill_id, priority) VALUES (?, ?, ?)", rows,
        )
    conn.commit()
    conn.close()


def search_omicron_catalog_for_priority(query: str, guild_id: int, modes: tuple | None = ("ВГ",), limit: int = 20) -> list:
    """Поиск омикронов для добавления в приоритетный список (/omicrons/api/search, и весь
    каталог разом — query="", limit=большой — для выпадающего списка на /omicrons/priority)
    — join skill_tier_thresholds (omicron_tier IS NOT NULL) x unit_omicron_skills x
    game_units, минус то, что уже в списке гильдии. modes=None снимает фильтр по игровому
    режиму (по умолчанию только "ВГ" — по прямому запросу пользователя 2026-09-02
    "оставить только те, что ВГшные", сузили с изначального ВГ+ТБ; ручной поиск всё равно не
    заблокирован, если понадобится что-то ещё, см. план). Регистронезависимый Unicode-поиск
    в Python — та же причина, что и в search_game_units (SQLite LOWER() не работает для
    кириллицы). Отсортировано по имени юнита/способности — предсказуемый порядок в
    выпадающем списке. ability_type — "Лидерка"/"Базовая"/"Особая"/"Уникальная N" (по
    прямому запросу пользователя 2026-09-02 "допишем какой это скил")."""
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    _ensure_skill_tier_thresholds_table(cursor)
    _ensure_unit_omicron_skills_table(cursor)
    _ensure_guild_omicron_priority_table(cursor)
    cursor.execute("""
        SELECT t.skill_id, t.name, t.omicron_mode, t.ability_type, u.base_id, g.cached_name
        FROM skill_tier_thresholds t
        JOIN unit_omicron_skills u ON u.skill_id = t.skill_id
        JOIN game_units g ON g.base_id = u.base_id
        WHERE t.omicron_tier IS NOT NULL
        ORDER BY g.cached_name, t.name
    """)
    rows = cursor.fetchall()
    cursor.execute("SELECT skill_id FROM guild_omicron_priority WHERE guild_id = ?", (guild_id,))
    already = {r[0] for r in cursor.fetchall()}
    conn.close()

    q = query.strip().lower()
    matches = []
    for skill_id, skill_name, omicron_mode, ability_type, base_id, unit_name in rows:
        if skill_id in already:
            continue
        if modes and omicron_mode not in modes:
            continue
        haystack = f"{unit_name} {skill_name or ''}".lower()
        if q and q not in haystack:
            continue
        matches.append({
            "skill_id": skill_id,
            "unit_name": unit_name,
            "base_id": base_id,
            "skill_name": skill_name or "",
            "omicron_mode": omicron_mode or "",
            "ability_type": ability_type or "",
        })
        if len(matches) >= limit:
            break
    return matches


def _ensure_guild_omicron_requirement_rules_table(cursor):
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS guild_omicron_requirement_rules (
            guild_id INTEGER PRIMARY KEY,
            rules_text TEXT NOT NULL,
            updated_by TEXT,
            updated_at TEXT NOT NULL
        )
    """)


def get_guild_omicron_requirement_rules(guild_id: int) -> str:
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    _ensure_guild_omicron_requirement_rules_table(cursor)
    cursor.execute("SELECT rules_text FROM guild_omicron_requirement_rules WHERE guild_id = ?", (guild_id,))
    row = cursor.fetchone()
    conn.close()
    return row[0] if row else ""


def set_guild_omicron_requirement_rules(guild_id: int, rules_text: str, updated_by: str = None) -> None:
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    _ensure_guild_omicron_requirement_rules_table(cursor)
    cursor.execute("""
        INSERT INTO guild_omicron_requirement_rules (guild_id, rules_text, updated_by, updated_at)
        VALUES (?, ?, ?, datetime('now'))
        ON CONFLICT(guild_id) DO UPDATE SET rules_text = excluded.rules_text, updated_by = excluded.updated_by, updated_at = excluded.updated_at
    """, (guild_id, rules_text, updated_by))
    conn.commit()
    conn.close()


# =====================================================================
# АКТИВНОСТЬ ГИЛЬДИИ (скрапинг swgoh.gg/g/<hash>/activity/, cogs/gohgg_activity.py)
# =====================================================================
def _ensure_guild_activity_events_table(cursor):
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS guild_activity_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id INTEGER NOT NULL,
            ally_code TEXT NOT NULL,
            base_id TEXT NOT NULL,
            action_type TEXT NOT NULL,
            old_value TEXT,
            new_value TEXT NOT NULL,
            event_date TEXT NOT NULL,
            scraped_at TEXT NOT NULL,
            UNIQUE(guild_id, ally_code, base_id, action_type, old_value, new_value, event_date)
        )
    """)
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_guild_activity_guild_date ON guild_activity_events(guild_id, event_date)")
    # announced — только для action_type='omicron' (см. add_guild_activity_event): пока запись
    # в БД (упреждает будущие диффы) и Discord-объявление были одним шагом в конце часового
    # цикла (cogs/stat_requirements.py::player_units_sync_loop), рестарт бота между "уже
    # записали в БД" и "успели объявить" НАВСЕГДА терял объявление — событие уже задиффено,
    # повторно не всплывёт. DEFAULT 1 нужен, чтобы при первом ALTER все существующие строки
    # (в т.ч. старые omicron-события) не считались "ожидающими" и не устроили залповую рассылку
    # объявлений о протухших событиях — по умолчанию считаем "уже решено", 0 явно ставит только
    # add_guild_activity_event для новых omicron-строк.
    try:
        cursor.execute("ALTER TABLE guild_activity_events ADD COLUMN announced INTEGER NOT NULL DEFAULT 1")
    except sqlite3.OperationalError:
        pass


def add_guild_activity_event(guild_id: int, ally_code: str, base_id: str, action_type: str,
                              old_value: str | None, new_value: str, event_date: str) -> int | None:
    """Возвращает id новой строки, если она реально добавлена, иначе None (уже была,
    INSERT OR IGNORE проглотил дубль) — id нужен вызывающему коду (services/activity_diff.py)
    для omicron-событий, чтобы потом отметить их объявленными через mark_activity_event_announced."""
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    _ensure_guild_activity_events_table(cursor)
    announced = 0 if action_type == "omicron" else 1
    cursor.execute("""
        INSERT OR IGNORE INTO guild_activity_events
            (guild_id, ally_code, base_id, action_type, old_value, new_value, event_date, scraped_at, announced)
        VALUES (?, ?, ?, ?, ?, ?, ?, datetime('now'), ?)
    """, (guild_id, ally_code, base_id, action_type, old_value, new_value, event_date, announced))
    conn.commit()
    event_id = cursor.lastrowid if cursor.rowcount > 0 else None
    conn.close()
    return event_id


def get_unannounced_omicron_events() -> list[tuple]:
    """[(id, guild_id, ally_code, base_id, skill_id), ...] — omicron-события, записанные в БД,
    но ещё не объявленные в Discord (см. announced в _ensure_guild_activity_events_table).
    Вызывается в начале каждого player_units_sync_loop — подбирает и хвосты после рестарта
    бота между записью и объявлением, и объявления, которые упали на временной ошибке отправки."""
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    _ensure_guild_activity_events_table(cursor)
    cursor.execute("""
        SELECT id, guild_id, ally_code, base_id, new_value
        FROM guild_activity_events
        WHERE action_type = 'omicron' AND announced = 0
        ORDER BY id
    """)
    rows = cursor.fetchall()
    conn.close()
    return rows


def mark_activity_event_announced(event_id: int) -> None:
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    _ensure_guild_activity_events_table(cursor)
    cursor.execute("UPDATE guild_activity_events SET announced = 1 WHERE id = ?", (event_id,))
    conn.commit()
    conn.close()


def _guild_activity_events_filter_sql(guild_id: int, ally_code: str | None, action_type: str | None,
                                       date_from: str | None, date_to: str | None):
    """Общий WHERE для get_guild_activity_events/get_guild_activity_events_count — чтобы
    подсчёт страниц (COUNT) и сама выборка (SELECT ... LIMIT/OFFSET) всегда фильтровали
    одинаково, иначе номера страниц разъедутся с реальным числом строк."""
    clauses = ["guild_id = ?"]
    params = [guild_id]
    if ally_code:
        clauses.append("ally_code = ?")
        params.append(ally_code)
    if action_type:
        clauses.append("action_type = ?")
        params.append(action_type)
    if date_from:
        clauses.append("event_date >= ?")
        params.append(date_from)
    if date_to:
        clauses.append("event_date <= ?")
        params.append(date_to)
    return " AND ".join(clauses), params


def get_guild_activity_events(guild_id: int, ally_code: str | None = None, action_type: str | None = None,
                               limit: int = 300, offset: int = 0,
                               date_from: str | None = None, date_to: str | None = None):
    """Возвращает [(ally_code, base_id, action_type, old_value, new_value, event_date, scraped_at), ...],
    новые сначала (по id, что совпадает с порядком скрапинга — самые свежие странице 1 вставляются первыми).
    date_from/date_to — включительно, формат event_date (YYYY-MM-DD)."""
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    _ensure_guild_activity_events_table(cursor)
    where_sql, params = _guild_activity_events_filter_sql(guild_id, ally_code, action_type, date_from, date_to)
    cursor.execute(f"""
        SELECT ally_code, base_id, action_type, old_value, new_value, event_date, scraped_at
        FROM guild_activity_events WHERE {where_sql}
        ORDER BY id DESC LIMIT ? OFFSET ?
    """, params + [limit, offset])
    rows = cursor.fetchall()
    conn.close()
    return rows


def get_guild_activity_events_count(guild_id: int, ally_code: str | None = None, action_type: str | None = None,
                                     date_from: str | None = None, date_to: str | None = None) -> int:
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    _ensure_guild_activity_events_table(cursor)
    where_sql, params = _guild_activity_events_filter_sql(guild_id, ally_code, action_type, date_from, date_to)
    cursor.execute(f"SELECT COUNT(*) FROM guild_activity_events WHERE {where_sql}", params)
    count = cursor.fetchone()[0]
    conn.close()
    return count


def get_guild_activity_distinct_dates(guild_id: int, ally_code: str | None = None, action_type: str | None = None,
                                       date_from: str | None = None, date_to: str | None = None) -> list[str]:
    """Отсортированные по убыванию (свежие первыми) даты, в которые по текущему фильтру
    есть хоть одно событие — основа постраничной навигации на /activity "1 страница = 1 день"
    (см. web/routes/guild_dashboard.py::activity)."""
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    _ensure_guild_activity_events_table(cursor)
    where_sql, params = _guild_activity_events_filter_sql(guild_id, ally_code, action_type, date_from, date_to)
    cursor.execute(f"""
        SELECT DISTINCT event_date FROM guild_activity_events WHERE {where_sql}
        ORDER BY event_date DESC
    """, params)
    dates = [r[0] for r in cursor.fetchall()]
    conn.close()
    return dates


def get_guild_activity_type_counts(guild_id: int, ally_code: str | None = None,
                                    date_from: str | None = None, date_to: str | None = None):
    """[(action_type, count), ...] по игроку/периоду, БЕЗ фильтра по типу события —
    это данные для панели "по типу изменения", которая должна показывать полную картину
    независимо от того, каким типом сейчас отфильтрована сама лента."""
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    _ensure_guild_activity_events_table(cursor)
    where_sql, params = _guild_activity_events_filter_sql(guild_id, ally_code, None, date_from, date_to)
    cursor.execute(f"""
        SELECT action_type, COUNT(*) FROM guild_activity_events WHERE {where_sql}
        GROUP BY action_type
    """, params)
    rows = cursor.fetchall()
    conn.close()
    return rows


def get_guild_activity_player_type_counts(guild_id: int, date_from: str | None = None,
                                           date_to: str | None = None):
    """[(ally_code, action_type, count), ...] по всем игрокам сразу за период — основа
    сводной таблицы "статистика по игрокам" (/activity/players), одним запросом вместо
    N обращений get_guild_activity_type_counts(ally_code=...) по игроку."""
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    _ensure_guild_activity_events_table(cursor)
    where_sql, params = _guild_activity_events_filter_sql(guild_id, None, None, date_from, date_to)
    cursor.execute(f"""
        SELECT ally_code, action_type, COUNT(*) FROM guild_activity_events WHERE {where_sql}
        GROUP BY ally_code, action_type
    """, params)
    rows = cursor.fetchall()
    conn.close()
    return rows


def get_guild_activity_player_codes(guild_id: int) -> list:
    """Все ally_code, у которых есть хотя бы одно событие активности в этой гильдии —
    независимо от лимита/фильтра get_guild_activity_events, чтобы список для фильтра
    на веб-странице не схлопывался до одного игрока при уже применённом фильтре."""
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    _ensure_guild_activity_events_table(cursor)
    cursor.execute("SELECT DISTINCT ally_code FROM guild_activity_events WHERE guild_id = ?", (guild_id,))
    rows = [r[0] for r in cursor.fetchall()]
    conn.close()
    return rows


# =====================================================================
# ФРАЗЫ-ПРИПИСКИ ДЛЯ ОБЪЯВЛЕНИЙ О ВЫДАЧЕ ОМИКРОНОВ (cogs/stat_requirements.py)
# Глобальный справочник (не per-guild) — персонаж редко зависит от того, в какой
# из обслуживаемых гильдий выдан омикрон, а управляется он супер-админами через
# веб (web/routes/omicron_phrases.py), а не гильдийскими офицерами.
# =====================================================================
def _ensure_omicron_phrases_table(cursor):
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS omicron_phrases (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            character_key TEXT NOT NULL,
            skill_id TEXT NOT NULL DEFAULT '',
            phrase TEXT NOT NULL,
            updated_by TEXT,
            updated_at TEXT NOT NULL,
            UNIQUE(character_key, skill_id)
        )
    """)
    # Миграция со старой схемы (до 2026-08-31: character_key TEXT UNIQUE, без skill_id,
    # ровно одна фраза на персонажа сразу на все его омикроны). Расширено под запрос
    # "выбрать, для какого конкретно омикрона нужна фраза" — skill_id='' теперь значит
    # "фраза по умолчанию на все омикроны персонажа", непустой skill_id — override на
    # конкретный омикрон (см. get_omicron_phrase). Старый UNIQUE(character_key) не даёт
    # добавить вторую строку на того же персонажа, поэтому не ALTER, а пересоздание
    # таблицы — определяем старую схему по отсутствию колонки skill_id.
    cursor.execute("PRAGMA table_info(omicron_phrases)")
    columns = {row[1] for row in cursor.fetchall()}
    if "skill_id" not in columns:
        cursor.execute("ALTER TABLE omicron_phrases RENAME TO omicron_phrases_old")
        cursor.execute("""
            CREATE TABLE omicron_phrases (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                character_key TEXT NOT NULL,
                skill_id TEXT NOT NULL DEFAULT '',
                phrase TEXT NOT NULL,
                updated_by TEXT,
                updated_at TEXT NOT NULL,
                UNIQUE(character_key, skill_id)
            )
        """)
        cursor.execute("""
            INSERT INTO omicron_phrases (character_key, skill_id, phrase, updated_by, updated_at)
            SELECT character_key, '', phrase, updated_by, updated_at FROM omicron_phrases_old
        """)
        cursor.execute("DROP TABLE omicron_phrases_old")
        # Обязательный commit прямо здесь: INSERT — DML, sqlite3 открывает под него
        # implicit-транзакцию, а вызывающие read-only функции (get_all_omicron_phrases и
        # т.п.) закрывают соединение без conn.commit() — без этой строки перенос старых
        # фраз в новую таблицу молча откатывался бы при conn.close() (найдено локальным
        # тестом миграции 2026-08-31, реального инцидента на проде не было).
        cursor.connection.commit()


def set_omicron_phrase(character_key: str, phrase: str, updated_by: str = None, skill_id: str = "") -> None:
    """Добавляет фразу либо (при уже существующей записи на тот же character_key+skill_id)
    заменяет её. skill_id='' (по умолчанию) — фраза на все омикроны персонажа; конкретный
    skill_id — override только на этот омикрон (см. get_omicron_phrase)."""
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    _ensure_omicron_phrases_table(cursor)
    cursor.execute("""
        INSERT OR REPLACE INTO omicron_phrases (character_key, skill_id, phrase, updated_by, updated_at)
        VALUES (?, ?, ?, ?, datetime('now'))
    """, (character_key, skill_id, phrase, updated_by))
    conn.commit()
    conn.close()


def get_omicron_phrase(character_key: str, skill_id: str | None = None) -> str | None:
    """Фраза для конкретного омикрона (skill_id), если она задана отдельно; иначе фраза
    "по умолчанию" на все омикроны персонажа (skill_id=''); None, если не задано ни то,
    ни другое. skill_id=None — то же самое, что не передавать конкретный омикрон вовсе
    (сразу фраза по умолчанию). См. cogs/stat_requirements.py::_announce_omicrons."""
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    _ensure_omicron_phrases_table(cursor)
    if skill_id:
        cursor.execute(
            "SELECT phrase FROM omicron_phrases WHERE character_key = ? AND skill_id = ?",
            (character_key, skill_id),
        )
        row = cursor.fetchone()
        if row:
            conn.close()
            return row[0]
    cursor.execute(
        "SELECT phrase FROM omicron_phrases WHERE character_key = ? AND skill_id = ''",
        (character_key,),
    )
    row = cursor.fetchone()
    conn.close()
    return row[0] if row else None


def get_all_omicron_phrases() -> list:
    """[(id, character_key, skill_id, phrase, updated_by, updated_at), ...] по алфавиту
    персонажа. skill_id='' — фраза по умолчанию на все омикроны персонажа; непустой —
    override на конкретный омикрон (см. set_omicron_phrase)."""
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    _ensure_omicron_phrases_table(cursor)
    cursor.execute("SELECT id, character_key, skill_id, phrase, updated_by, updated_at FROM omicron_phrases ORDER BY character_key")
    rows = cursor.fetchall()
    conn.close()
    return rows


def delete_omicron_phrase(character_key: str, skill_id: str = "") -> bool:
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    _ensure_omicron_phrases_table(cursor)
    cursor.execute("DELETE FROM omicron_phrases WHERE character_key = ? AND skill_id = ?", (character_key, skill_id))
    conn.commit()
    deleted = cursor.rowcount > 0
    conn.close()
    return deleted


# =====================================================================
# КОНТРЫ ПО ВГ: зеркало форум-канала гайдов (guilds.tw_guide_forum_channel_id) —
# каждый тред форума = один вражеский пак, каждое сообщение внутри = один вариант
# контры на этот пак (или его конкретный вариант, если заголовок сообщения '## ...'
# отличается от названия треда — см. пример "Хондо" / "Хондо с вейнокроном").
# Наполняется периодическим синком (cogs/tw_counters.py::sync_loop), не руками —
# см. _parse_counter_message для формата, который парсится в структурированные поля.
# =====================================================================
def _ensure_tw_counter_tables(cursor):
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS tw_counter_threads (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id INTEGER NOT NULL DEFAULT 1,
            thread_id TEXT NOT NULL,
            enemy_label TEXT NOT NULL,
            tag TEXT,
            updated_at TEXT NOT NULL,
            UNIQUE(guild_id, thread_id)
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS tw_counters (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id INTEGER NOT NULL DEFAULT 1,
            thread_id TEXT NOT NULL,
            message_id TEXT NOT NULL,
            enemy_variant TEXT NOT NULL,
            counter_leader TEXT,
            composition TEXT,
            datacron_note TEXT,
            battle_plan TEXT,
            team_code TEXT,
            video_url TEXT,
            parsed_ok INTEGER NOT NULL DEFAULT 0,
            raw_text TEXT,
            author TEXT,
            posted_at TEXT,
            updated_at TEXT NOT NULL,
            UNIQUE(guild_id, message_id)
        )
    """)
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_tw_counters_guild_variant ON tw_counters(guild_id, enemy_variant)")


def upsert_tw_counter_thread(guild_id: int, thread_id: str, enemy_label: str, tag: str = None) -> None:
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    _ensure_tw_counter_tables(cursor)
    cursor.execute("""
        INSERT INTO tw_counter_threads (guild_id, thread_id, enemy_label, tag, updated_at)
        VALUES (?, ?, ?, ?, datetime('now'))
        ON CONFLICT(guild_id, thread_id) DO UPDATE SET
            enemy_label = excluded.enemy_label,
            tag = excluded.tag,
            updated_at = excluded.updated_at
    """, (guild_id, thread_id, enemy_label, tag))
    conn.commit()
    conn.close()


def upsert_tw_counter(guild_id: int, thread_id: str, message_id: str, enemy_variant: str,
                       counter_leader: str = None, composition: str = None, datacron_note: str = None,
                       battle_plan: str = None, team_code: str = None, video_url: str = None,
                       parsed_ok: bool = False, raw_text: str = None, author: str = None,
                       posted_at: str = None) -> None:
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    _ensure_tw_counter_tables(cursor)
    cursor.execute("""
        INSERT INTO tw_counters (
            guild_id, thread_id, message_id, enemy_variant, counter_leader, composition,
            datacron_note, battle_plan, team_code, video_url, parsed_ok, raw_text, author,
            posted_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
        ON CONFLICT(guild_id, message_id) DO UPDATE SET
            enemy_variant = excluded.enemy_variant,
            counter_leader = excluded.counter_leader,
            composition = excluded.composition,
            datacron_note = excluded.datacron_note,
            battle_plan = excluded.battle_plan,
            team_code = excluded.team_code,
            video_url = excluded.video_url,
            parsed_ok = excluded.parsed_ok,
            raw_text = excluded.raw_text,
            author = excluded.author,
            posted_at = excluded.posted_at,
            updated_at = excluded.updated_at
    """, (guild_id, thread_id, message_id, enemy_variant, counter_leader, composition, datacron_note,
          battle_plan, team_code, video_url, int(parsed_ok), raw_text, author, posted_at))
    conn.commit()
    conn.close()


def get_tw_counter_packs(guild_id: int, query: str = "") -> list:
    """Уникальные названия паков (enemy_variant) для автокомплита — с фильтром по подстроке."""
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    _ensure_tw_counter_tables(cursor)
    cursor.execute("""
        SELECT DISTINCT enemy_variant FROM tw_counters
        WHERE guild_id = ? AND enemy_variant LIKE ?
        ORDER BY enemy_variant
    """, (guild_id, f"%{query}%"))
    rows = [r[0] for r in cursor.fetchall()]
    conn.close()
    return rows


def get_tw_counters_for_pack(guild_id: int, enemy_variant: str) -> list:
    """Все известные контры на конкретный пак — (id, counter_leader, composition, datacron_note,
    battle_plan, team_code, video_url, parsed_ok)."""
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    _ensure_tw_counter_tables(cursor)
    cursor.execute("""
        SELECT id, counter_leader, composition, datacron_note, battle_plan, team_code, video_url, parsed_ok, raw_text
        FROM tw_counters WHERE guild_id = ? AND enemy_variant = ?
        ORDER BY parsed_ok DESC, id
    """, (guild_id, enemy_variant))
    rows = cursor.fetchall()
    conn.close()
    return rows


def get_tw_counter_by_id(guild_id: int, counter_id: int):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    _ensure_tw_counter_tables(cursor)
    cursor.execute("""
        SELECT id, enemy_variant, counter_leader, composition, datacron_note, battle_plan, team_code, video_url, parsed_ok, raw_text
        FROM tw_counters WHERE guild_id = ? AND id = ?
    """, (guild_id, counter_id))
    row = cursor.fetchone()
    conn.close()
    return row


def delete_tw_counter(guild_id: int, message_id: str) -> None:
    """Используется синком при повторном ресинке, если сообщение при разборе
    оказалось мусорным дублем заголовка треда (см. cogs/tw_counters.py) — убирает
    ранее сохранённую по этому message_id запись, если она была."""
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    _ensure_tw_counter_tables(cursor)
    cursor.execute("DELETE FROM tw_counters WHERE guild_id = ? AND message_id = ?", (guild_id, message_id))
    conn.commit()
    conn.close()


def count_tw_counters(guild_id: int) -> int:
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    _ensure_tw_counter_tables(cursor)
    cursor.execute("SELECT COUNT(*) FROM tw_counters WHERE guild_id = ?", (guild_id,))
    count = cursor.fetchone()[0]
    conn.close()
    return count


# =====================================================================
# QA-ЧЕКЛИСТ ПО СТРАНИЦАМ ВЕБ-ДАШБОРДА (временная страница, web/routes/qa_checklist.py)
# — офицеры отмечают, какие страницы уже проверены/отполированы. Список
# страниц сам по себе хардкожен в роуте (не в БД); тут хранится только
# состояние галочки по page_key.
# =====================================================================
def _ensure_qa_checklist_table(cursor):
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS qa_checklist (
            page_key TEXT PRIMARY KEY,
            checked INTEGER NOT NULL DEFAULT 0,
            checked_by TEXT,
            checked_at TEXT
        )
    """)


def get_qa_checklist_state() -> dict:
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    _ensure_qa_checklist_table(cursor)
    cursor.execute("SELECT page_key, checked, checked_by, checked_at FROM qa_checklist")
    rows = cursor.fetchall()
    conn.close()
    return {
        r[0]: {"checked": bool(r[1]), "checked_by": r[2], "checked_at": r[3]}
        for r in rows
    }


def set_qa_checklist_item(page_key: str, checked: bool, checked_by: str) -> None:
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    _ensure_qa_checklist_table(cursor)
    cursor.execute("""
        INSERT INTO qa_checklist (page_key, checked, checked_by, checked_at)
        VALUES (?, ?, ?, datetime('now'))
        ON CONFLICT(page_key) DO UPDATE SET checked = excluded.checked, checked_by = excluded.checked_by, checked_at = excluded.checked_at
    """, (page_key, int(checked), checked_by))
    conn.commit()
    conn.close()