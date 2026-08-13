import json
import sqlite3

DB_NAME = "guild_management.db"

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

    # 4. Справочник игровых юнитов (Персонажи и Корабли) — глобальный, общий
    #    для всех гильдий (игровой каталог SWGOH, не привязан к guild_id).
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS game_units (
            base_id TEXT PRIMARY KEY,
            cached_name TEXT NOT NULL,
            unit_type TEXT NOT NULL DEFAULT 'character'
        )
    """)

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
        SELECT category, subcategory, date_str, comment
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
    """units_dict: {base_id: (cached_name, unit_type)} — INSERT OR REPLACE, в отличие
    от populate_initial_units обновляет имя/тип при повторной синхронизации."""
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    data = [(base_id, name, unit_type) for base_id, (name, unit_type) in units_dict.items()]
    cursor.executemany("""
        INSERT OR REPLACE INTO game_units (base_id, cached_name, unit_type)
        VALUES (?, ?, ?)
    """, data)
    conn.commit()
    conn.close()


def search_game_units(query: str, limit: int = 25):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    like = f"%{query.lower()}%"
    cursor.execute("""
        SELECT base_id, cached_name FROM game_units
        WHERE LOWER(cached_name) LIKE ? OR LOWER(base_id) LIKE ?
        LIMIT ?
    """, (like, like, limit))
    rows = cursor.fetchall()
    conn.close()
    return rows


def get_game_unit_name(base_id: str) -> str | None:
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("SELECT cached_name FROM game_units WHERE base_id = ?", (base_id,))
    row = cursor.fetchone()
    conn.close()
    return row[0] if row else None

# =====================================================================
# ЗАДАЧИ НА ПРОКАЧКУ (/task_add + часовой аудит выполнения через Comlink)
# =====================================================================
def add_task(ally_code, base_id, target_type, target_value, deadline, created_by, guild_id: int = 1) -> int:
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO tasks (ally_code, base_id, target_type, target_value, deadline, status, created_by, date_created, guild_id)
        VALUES (?, ?, ?, ?, ?, 'ACTIVE', ?, datetime('now'), ?)
    """, (ally_code, base_id, target_type, target_value, deadline, created_by, guild_id))
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


def update_task_status(task_id, status):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("UPDATE tasks SET status = ? WHERE task_id = ?", (status, task_id))
    conn.commit()
    conn.close()

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

def sync_guild_roster(guild_id: int, roster_rows):
    """Полная замена состава гильдии guild_id в user_mapping: roster_rows —
    [(discord_id, ally_code, ingame_name), ...]. Используется часовым рефрешем
    ростер-кэша (ViolationsCog.update_roster_cache) — не трогает другие гильдии."""
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("DELETE FROM user_mapping WHERE guild_id = ?", (guild_id,))
    cursor.executemany(
        "INSERT OR REPLACE INTO user_mapping (guild_id, discord_id, ally_code, ingame_name) VALUES (?, ?, ?, ?)",
        [(guild_id, discord_id, ally_code, ingame_name) for discord_id, ally_code, ingame_name in roster_rows]
    )
    conn.commit()
    conn.close()

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
TB_HISTORY_KEEP = 6

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

def record_tb_event(fingerprint: str, guild_id: int = 1) -> int:
    """Идемпотентно регистрирует ТБ гильдии по отпечатку (fingerprint), возвращает event_id."""
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    _ensure_tb_history_tables(cursor)
    cursor.execute("SELECT id FROM tb_events WHERE guild_id = ? AND fingerprint = ?", (guild_id, fingerprint))
    row = cursor.fetchone()
    if row:
        conn.close()
        return row[0]
    cursor.execute(
        "INSERT INTO tb_events (guild_id, fingerprint, completed_at) VALUES (?, ?, datetime('now'))",
        (guild_id, fingerprint)
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
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_datacron_req_guild_set ON datacron_requirements(guild_id, set_id)")


def add_datacron_requirement(set_id: int, pack: str, level3_value: str, level6_value: str, level9_value: str,
                              comment: str, created_by: str, priority: str = "required", guild_id: int = 1) -> int:
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    _ensure_datacron_requirements_table(cursor)
    cursor.execute("""
        INSERT INTO datacron_requirements
            (set_id, pack, level3_value, level6_value, level9_value, comment, created_by, created_at, priority, guild_id)
        VALUES (?, ?, ?, ?, ?, ?, ?, datetime('now'), ?, ?)
    """, (set_id, pack, level3_value, level6_value, level9_value, comment, created_by, priority, guild_id))
    conn.commit()
    req_id = cursor.lastrowid
    conn.close()
    return req_id


def update_datacron_requirement(req_id: int, set_id: int, pack: str, level3_value: str, level6_value: str,
                                 level9_value: str, comment: str, priority: str, guild_id: int = 1) -> bool:
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    _ensure_datacron_requirements_table(cursor)
    cursor.execute("""
        UPDATE datacron_requirements
        SET set_id = ?, pack = ?, level3_value = ?, level6_value = ?, level9_value = ?, comment = ?, priority = ?
        WHERE id = ? AND guild_id = ?
    """, (set_id, pack, level3_value, level6_value, level9_value, comment, priority, req_id, guild_id))
    conn.commit()
    updated = cursor.rowcount > 0
    conn.close()
    return updated


def delete_datacron_requirement(req_id: int, guild_id: int = 1) -> bool:
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    _ensure_datacron_requirements_table(cursor)
    cursor.execute("DELETE FROM datacron_requirements WHERE id = ? AND guild_id = ?", (req_id, guild_id))
    conn.commit()
    deleted = cursor.rowcount > 0
    conn.close()
    return deleted


def delete_datacron_requirements_by_set(set_id: int, guild_id: int = 1) -> int:
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    _ensure_datacron_requirements_table(cursor)
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
        SELECT id, set_id, pack, level3_value, level6_value, level9_value, comment, created_by, created_at, priority
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
        SELECT id, set_id, pack, level3_value, level6_value, level9_value, comment, created_by, created_at, priority
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
        SELECT id, set_id, pack, level3_value, level6_value, level9_value, comment, created_by, created_at, priority
        FROM datacron_requirements WHERE guild_id = ? ORDER BY set_id, id
    """, (guild_id,))
    rows = cursor.fetchall()
    conn.close()
    return rows


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