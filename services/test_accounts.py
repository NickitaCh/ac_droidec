"""Explicit operator grants for test accounts outside the real guild roster.

These records never grant roles, change a game guild or enter user_mapping.
Only an operator may add one, with the owner's authorization.
"""
import sqlite3
from contextlib import contextmanager

import database


@contextmanager
def _db():
    conn = sqlite3.connect(database.DB_NAME, timeout=15)
    try:
        conn.execute("""CREATE TABLE IF NOT EXISTS test_account_links (
            guild_id INTEGER NOT NULL, owner TEXT NOT NULL, ally_code TEXT NOT NULL,
            player_id TEXT NOT NULL, name TEXT NOT NULL,
            PRIMARY KEY (guild_id, owner, ally_code))""")
        conn.commit()
        with conn:
            yield conn
    finally:
        conn.close()


def player_id(owner, guild_id, ally_code):
    with _db() as conn:
        row = conn.execute("SELECT player_id FROM test_account_links WHERE owner=? AND guild_id=? AND ally_code=?",
                           (str(owner), guild_id, str(ally_code))).fetchone()
    return row[0] if row else None


def visible(owner, guild_id, ally_code, profile=None):
    from services import bot_profiles
    return (profile or bot_profiles.current_profile()) == "test" or player_id(owner, guild_id, ally_code) is None


def registrations(owner):
    """Virtual alts: only new TEST code sees them; old PROD images see no rows."""
    from services import bot_profiles
    if bot_profiles.current_profile() != "test":
        return []
    conn = sqlite3.connect(database.DB_NAME, timeout=15)
    try:
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='test_account_links'").fetchone():
            return []
        return [(gid, code, name, 0) for gid, code, name in conn.execute(
            "SELECT guild_id, ally_code, name FROM test_account_links WHERE owner=? ORDER BY guild_id, ally_code", (str(owner),))
                if bot_profiles.guild_profile(gid) == "test"]
    finally:
        conn.close()


def revoke_registration(owner, guild_id, ally_code):
    from services import bot_profiles
    if bot_profiles.current_profile() != "test":
        return
    conn = sqlite3.connect(database.DB_NAME, timeout=15)
    try:
        if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='test_account_links'").fetchone():
            conn.execute("DELETE FROM test_account_links WHERE owner=? AND guild_id=? AND ally_code=?", (str(owner), guild_id, str(ally_code)))
            conn.commit()
    finally:
        conn.close()


def grant(owner, guild_id, ally_code, expected_player_id, name):
    from services import bot_profiles, game_account
    game_account.require_pilot(owner, guild_id, "test")
    if bot_profiles.guild_profile(guild_id) != "test":
        raise ValueError("Тестовый аккаунт доступен только в тестовой версии.")
    if not database.get_user_registration(str(owner), guild_id=guild_id):
        raise ValueError("Сначала привяжите основной аккаунт.")
    if str(database.get_user_registration(str(owner), guild_id=guild_id)[0]) == str(ally_code):
        raise ValueError("Основной аккаунт нельзя заменить тестовым альтом.")
    registrations = database.get_ally_code_registrations(str(ally_code))
    if any(str(other) != str(owner) for _gid, other in registrations):
        raise ValueError("Аккаунт уже принадлежит другому пользователю.")
    with _db() as conn:
        conn.execute("INSERT OR REPLACE INTO test_account_links VALUES (?, ?, ?, ?, ?)",
                     (guild_id, str(owner), str(ally_code), expected_player_id, name))
