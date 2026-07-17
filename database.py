import sqlite3

DB_NAME = "guild_management.db"

def init_db():
    """Создает все таблицы в единой БД, если они еще не созданы"""
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()

    # 1. Таблица маппинга пользователей (Discord <-> SWGOH)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS user_mapping (
            discord_id TEXT PRIMARY KEY,
            ally_code TEXT UNIQUE NOT NULL,
            ingame_name TEXT
        )
    """)

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

    # 4. Справочник игровых юнитов (Персонажи и Корабли)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS game_units (
            base_id TEXT PRIMARY KEY,
            cached_name TEXT NOT NULL
        )
    """)

    # Создаем индексы для быстродействия
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_warns_ally ON position_warns(ally_code)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_tasks_ally ON tasks(ally_code)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status)")

    conn.commit()
    conn.close()
    print("📋 [БД] Инициализация структуры базы данных успешно завершена.")

# =====================================================================
# ФУНКЦИИ ДЛЯ РАБОТЫ С НАРУШЕНИЯМИ (WARNS)
# =====================================================================
def add_warn(ally_code, category, subcategory, date_str, comment=None):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO position_warns (ally_code, category, subcategory, date_str, comment)
        VALUES (?, ?, ?, ?, ?)
    """, (ally_code, category, subcategory, date_str, comment))
    conn.commit()
    conn.close()

def get_player_warns(ally_code):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("""
        SELECT category, subcategory, date_str, comment 
        FROM position_warns 
        WHERE ally_code = ? 
        ORDER BY id DESC
    """, (ally_code,))
    rows = cursor.fetchall()
    conn.close()
    return rows

def remove_warn(ally_code, category, subcategory, date_str):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("""
        DELETE FROM position_warns 
        WHERE ally_code = ? AND category = ? AND subcategory = ? AND date_str = ?
    """, (ally_code, category, subcategory, date_str))
    conn.commit()
    conn.close()

def get_all_warns():
    """Возвращает список всех нарушений гильдии для построения общей текстовой таблицы"""
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("SELECT ally_code, category, date_str FROM position_warns")
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
    
    
# ================== Дни рождения ==================

def init_birthday_table():
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS birthdays (
            discord_id TEXT PRIMARY KEY,
            day INTEGER NOT NULL,
            month INTEGER NOT NULL,
            year INTEGER DEFAULT 2000
        )
    """)
    conn.commit()
    conn.close()

def add_birthday(discord_id: str, day: int, month: int, year: int = 2000):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("""
        INSERT OR REPLACE INTO birthdays (discord_id, day, month, year)
        VALUES (?, ?, ?, ?)
    """, (discord_id, day, month, year))
    conn.commit()
    conn.close()

def remove_birthday(discord_id: str):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("DELETE FROM birthdays WHERE discord_id = ?", (discord_id,))
    conn.commit()
    conn.close()

def get_all_birthdays():
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("SELECT discord_id, day, month, year FROM birthdays")
    rows = cursor.fetchall()
    conn.close()
    return rows

def get_birthday_by_discord_id(discord_id: str):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("SELECT day, month, year FROM birthdays WHERE discord_id = ?", (discord_id,))
    row = cursor.fetchone()
    conn.close()
    return row



    
    
def get_allycode_by_discord_id(discord_id: str) -> str | None:
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("SELECT ally_code FROM user_mapping WHERE discord_id = ?", (discord_id,))
    row = cursor.fetchone()
    conn.close()
    return row[0] if row else None

def set_user_mapping(discord_id: str, ally_code: str, ingame_name: str = ""):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("""
        INSERT OR REPLACE INTO user_mapping (discord_id, ally_code, ingame_name)
        VALUES (?, ?, ?)
    """, (discord_id, ally_code, ingame_name))
    conn.commit()
    conn.close()
    
def get_all_user_mappings():
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("SELECT discord_id, ally_code, ingame_name FROM user_mapping")
    rows = cursor.fetchall()
    conn.close()
    return rows

# =====================================================================
# ПРОИЗВОЛЬНОЕ СОСТОЯНИЕ БОТА (переживает рестарты, key-value)
# =====================================================================
def _ensure_bot_state_table(cursor):
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS bot_state (
            key TEXT PRIMARY KEY,
            value TEXT
        )
    """)

def get_bot_state(key: str) -> str | None:
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    _ensure_bot_state_table(cursor)
    cursor.execute("SELECT value FROM bot_state WHERE key = ?", (key,))
    row = cursor.fetchone()
    conn.close()
    return row[0] if row else None

def set_bot_state(key: str, value: str):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    _ensure_bot_state_table(cursor)
    cursor.execute("""
        INSERT OR REPLACE INTO bot_state (key, value)
        VALUES (?, ?)
    """, (key, value))
    conn.commit()
    conn.close()


# =====================================================================
# ИСТОРИЯ ТБ (последние N событий, для команд compare / player_compare)
# =====================================================================
TB_HISTORY_KEEP = 3

def _ensure_tb_history_tables(cursor):
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS tb_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            fingerprint TEXT UNIQUE NOT NULL,
            completed_at TEXT NOT NULL
        )
    """)
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

def record_tb_event(fingerprint: str) -> int:
    """Идемпотентно регистрирует ТБ по отпечатку (fingerprint), возвращает event_id."""
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    _ensure_tb_history_tables(cursor)
    cursor.execute("SELECT id FROM tb_events WHERE fingerprint = ?", (fingerprint,))
    row = cursor.fetchone()
    if row:
        conn.close()
        return row[0]
    cursor.execute(
        "INSERT INTO tb_events (fingerprint, completed_at) VALUES (?, datetime('now'))",
        (fingerprint,)
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

def prune_tb_events(keep: int = TB_HISTORY_KEEP):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    _ensure_tb_history_tables(cursor)
    _ensure_tb_plan_tables(cursor)
    cursor.execute("SELECT id FROM tb_events ORDER BY id DESC LIMIT -1 OFFSET ?", (keep,))
    old_ids = [r[0] for r in cursor.fetchall()]
    if old_ids:
        placeholders = ",".join("?" * len(old_ids))
        cursor.execute(f"DELETE FROM tb_player_summary WHERE event_id IN ({placeholders})", old_ids)
        cursor.execute(f"DELETE FROM tb_player_detail WHERE event_id IN ({placeholders})", old_ids)
        cursor.execute(f"DELETE FROM tb_event_planet_names WHERE event_id IN ({placeholders})", old_ids)
        cursor.execute(f"DELETE FROM tb_events WHERE id IN ({placeholders})", old_ids)
    conn.commit()
    conn.close()

def get_recent_tb_events(limit: int = TB_HISTORY_KEEP):
    """Возвращает [(event_id, completed_at), ...] от старых к новым (максимум `limit`)."""
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    _ensure_tb_history_tables(cursor)
    cursor.execute("SELECT id, completed_at FROM tb_events ORDER BY id DESC LIMIT ?", (limit,))
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
            phase TEXT NOT NULL,
            conflict_key TEXT NOT NULL,
            planet_name TEXT NOT NULL,
            source TEXT NOT NULL DEFAULT 'manual',
            updated_at TEXT NOT NULL,
            PRIMARY KEY (phase, conflict_key)
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS tb_event_planet_names (
            event_id INTEGER NOT NULL,
            phase TEXT NOT NULL,
            conflict_key TEXT NOT NULL,
            planet_name TEXT NOT NULL,
            PRIMARY KEY (event_id, phase, conflict_key)
        )
    """)

def set_tb_planet_name(phase: str, conflict_key: str, planet_name: str, source: str = "manual"):
    """conflict_key: '01'/'02'/'03' (Light/Dark/Mixed) или 'bonus' для доп. зоны."""
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    _ensure_tb_plan_tables(cursor)
    cursor.execute("""
        INSERT OR REPLACE INTO tb_planet_names (phase, conflict_key, planet_name, source, updated_at)
        VALUES (?, ?, ?, ?, datetime('now'))
    """, (phase, conflict_key, planet_name, source))
    conn.commit()
    conn.close()

def get_tb_planet_names():
    """Текущий (живой) план планет — для отчёта по ещё не заснэпшоченной/последней ТБ."""
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    _ensure_tb_plan_tables(cursor)
    cursor.execute("SELECT phase, conflict_key, planet_name FROM tb_planet_names")
    rows = cursor.fetchall()
    conn.close()
    return {(phase, conflict_key): planet_name for phase, conflict_key, planet_name in rows}

def clear_tb_planet_names():
    """Вызывается при анонсе 1 этапа новой ТБ, чтобы не тащить названия планет прошлой ТБ."""
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    _ensure_tb_plan_tables(cursor)
    cursor.execute("DELETE FROM tb_planet_names")
    conn.commit()
    conn.close()

def snapshot_tb_planet_names(event_id: int):
    """Копирует текущий живой план планет в историю конкретного завершённого события ТБ."""
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    _ensure_tb_plan_tables(cursor)
    cursor.execute("SELECT phase, conflict_key, planet_name FROM tb_planet_names")
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


def get_user_mapping_by_name(name: str):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("SELECT ally_code, ingame_name FROM user_mapping WHERE ingame_name LIKE ?", (f"%{name}%",))
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
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_datacron_req_set ON datacron_requirements(set_id)")


def add_datacron_requirement(set_id: int, pack: str, level3_value: str, level6_value: str, level9_value: str,
                              comment: str, created_by: str, priority: str = "required") -> int:
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    _ensure_datacron_requirements_table(cursor)
    cursor.execute("""
        INSERT INTO datacron_requirements
            (set_id, pack, level3_value, level6_value, level9_value, comment, created_by, created_at, priority)
        VALUES (?, ?, ?, ?, ?, ?, ?, datetime('now'), ?)
    """, (set_id, pack, level3_value, level6_value, level9_value, comment, created_by, priority))
    conn.commit()
    req_id = cursor.lastrowid
    conn.close()
    return req_id


def update_datacron_requirement(req_id: int, set_id: int, pack: str, level3_value: str, level6_value: str,
                                 level9_value: str, comment: str, priority: str) -> bool:
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    _ensure_datacron_requirements_table(cursor)
    cursor.execute("""
        UPDATE datacron_requirements
        SET set_id = ?, pack = ?, level3_value = ?, level6_value = ?, level9_value = ?, comment = ?, priority = ?
        WHERE id = ?
    """, (set_id, pack, level3_value, level6_value, level9_value, comment, priority, req_id))
    conn.commit()
    updated = cursor.rowcount > 0
    conn.close()
    return updated


def delete_datacron_requirement(req_id: int) -> bool:
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    _ensure_datacron_requirements_table(cursor)
    cursor.execute("DELETE FROM datacron_requirements WHERE id = ?", (req_id,))
    conn.commit()
    deleted = cursor.rowcount > 0
    conn.close()
    return deleted


def delete_datacron_requirements_by_set(set_id: int) -> int:
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    _ensure_datacron_requirements_table(cursor)
    cursor.execute("DELETE FROM datacron_requirements WHERE set_id = ?", (set_id,))
    conn.commit()
    deleted = cursor.rowcount
    conn.close()
    return deleted


def count_datacron_requirements_by_set(set_id: int) -> int:
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    _ensure_datacron_requirements_table(cursor)
    cursor.execute("SELECT COUNT(*) FROM datacron_requirements WHERE set_id = ?", (set_id,))
    count = cursor.fetchone()[0]
    conn.close()
    return count


def get_datacron_requirement(req_id: int):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    _ensure_datacron_requirements_table(cursor)
    cursor.execute("""
        SELECT id, set_id, pack, level3_value, level6_value, level9_value, comment, created_by, created_at, priority
        FROM datacron_requirements WHERE id = ?
    """, (req_id,))
    row = cursor.fetchone()
    conn.close()
    return row


def get_datacron_requirements_by_set(set_id: int):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    _ensure_datacron_requirements_table(cursor)
    cursor.execute("""
        SELECT id, set_id, pack, level3_value, level6_value, level9_value, comment, created_by, created_at, priority
        FROM datacron_requirements WHERE set_id = ? ORDER BY id
    """, (set_id,))
    rows = cursor.fetchall()
    conn.close()
    return rows


def get_all_datacron_requirements():
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    _ensure_datacron_requirements_table(cursor)
    cursor.execute("""
        SELECT id, set_id, pack, level3_value, level6_value, level9_value, comment, created_by, created_at, priority
        FROM datacron_requirements ORDER BY set_id, id
    """)
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
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_datacron_focused_req_set ON datacron_focused_requirements(set_id)"
    )


def add_datacron_focused_requirement(set_id: int, pack: str, character_key: str, required_level: int,
                                      comment: str, created_by: str, priority: str = "required") -> int:
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    _ensure_datacron_focused_requirements_table(cursor)
    cursor.execute("""
        INSERT INTO datacron_focused_requirements
            (set_id, pack, character_key, required_level, comment, created_by, created_at, priority)
        VALUES (?, ?, ?, ?, ?, ?, datetime('now'), ?)
    """, (set_id, pack, character_key, required_level, comment, created_by, priority))
    conn.commit()
    req_id = cursor.lastrowid
    conn.close()
    return req_id


def update_datacron_focused_requirement(req_id: int, set_id: int, pack: str, character_key: str,
                                         required_level: int, comment: str, priority: str) -> bool:
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    _ensure_datacron_focused_requirements_table(cursor)
    cursor.execute("""
        UPDATE datacron_focused_requirements
        SET set_id = ?, pack = ?, character_key = ?, required_level = ?, comment = ?, priority = ?
        WHERE id = ?
    """, (set_id, pack, character_key, required_level, comment, priority, req_id))
    conn.commit()
    updated = cursor.rowcount > 0
    conn.close()
    return updated


def delete_datacron_focused_requirement(req_id: int) -> bool:
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    _ensure_datacron_focused_requirements_table(cursor)
    cursor.execute("DELETE FROM datacron_focused_requirements WHERE id = ?", (req_id,))
    conn.commit()
    deleted = cursor.rowcount > 0
    conn.close()
    return deleted


def delete_datacron_focused_requirements_by_set(set_id: int) -> int:
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    _ensure_datacron_focused_requirements_table(cursor)
    cursor.execute("DELETE FROM datacron_focused_requirements WHERE set_id = ?", (set_id,))
    conn.commit()
    deleted = cursor.rowcount
    conn.close()
    return deleted


def count_datacron_focused_requirements_by_set(set_id: int) -> int:
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    _ensure_datacron_focused_requirements_table(cursor)
    cursor.execute("SELECT COUNT(*) FROM datacron_focused_requirements WHERE set_id = ?", (set_id,))
    count = cursor.fetchone()[0]
    conn.close()
    return count


def get_datacron_focused_requirement(req_id: int):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    _ensure_datacron_focused_requirements_table(cursor)
    cursor.execute("""
        SELECT id, set_id, pack, character_key, required_level, comment, created_by, created_at, priority
        FROM datacron_focused_requirements WHERE id = ?
    """, (req_id,))
    row = cursor.fetchone()
    conn.close()
    return row


def get_datacron_focused_requirements_by_set(set_id: int):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    _ensure_datacron_focused_requirements_table(cursor)
    cursor.execute("""
        SELECT id, set_id, pack, character_key, required_level, comment, created_by, created_at, priority
        FROM datacron_focused_requirements WHERE set_id = ? ORDER BY id
    """, (set_id,))
    rows = cursor.fetchall()
    conn.close()
    return rows


def get_all_datacron_focused_requirements():
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    _ensure_datacron_focused_requirements_table(cursor)
    cursor.execute("""
        SELECT id, set_id, pack, character_key, required_level, comment, created_by, created_at, priority
        FROM datacron_focused_requirements ORDER BY set_id, id
    """)
    rows = cursor.fetchall()
    conn.close()
    return rows