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



def init_manual_event_tables():
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS manual_event_scores (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_type TEXT NOT NULL,  -- 'tw' или 'tb'
            discord_id TEXT NOT NULL,
            score INTEGER NOT NULL,
            timestamp TEXT NOT NULL DEFAULT (datetime('now'))
        )
    """)
    conn.commit()
    conn.close()

def add_manual_score(event_type, discord_id, score):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO manual_event_scores (event_type, discord_id, score)
        VALUES (?, ?, ?)
    """, (event_type, discord_id, score))
    conn.commit()
    conn.close()

def get_manual_scores(event_type):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("""
        SELECT discord_id, score FROM manual_event_scores
        WHERE event_type = ?
        ORDER BY score DESC
    """, (event_type,))
    rows = cursor.fetchall()
    conn.close()
    return rows

def clear_manual_scores(event_type):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("DELETE FROM manual_event_scores WHERE event_type = ?", (event_type,))
    conn.commit()
    conn.close()
    
    
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


def get_user_mapping_by_name(name: str):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("SELECT ally_code, ingame_name FROM user_mapping WHERE ingame_name LIKE ?", (f"%{name}%",))
    row = cursor.fetchone()
    conn.close()
    return row if row else None