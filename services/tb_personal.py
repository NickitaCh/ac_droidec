"""Личные памятки к миссиям ТБ, независимые от текущего события и взводов."""
import sqlite3
from contextlib import closing

import database


def notes(discord_id: str) -> dict:
    with closing(sqlite3.connect(database.DB_NAME)) as conn, conn:
        _ensure(conn)
        return {(p, m): text for p, m, text in conn.execute(
            "SELECT planet, mission, body FROM tb_personal_notes WHERE discord_id=?", (str(discord_id),))}


def _ensure(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS tb_personal_notes (
        discord_id TEXT NOT NULL, planet TEXT NOT NULL, mission TEXT NOT NULL,
        body TEXT NOT NULL, PRIMARY KEY(discord_id, planet, mission))""")


def save(discord_id: str, planet: str, mission: str, body: str):
    with closing(sqlite3.connect(database.DB_NAME)) as conn, conn:
        _ensure(conn)
        if body.strip():
            conn.execute("INSERT OR REPLACE INTO tb_personal_notes VALUES (?, ?, ?, ?)",
                         (str(discord_id), planet, mission, body.strip()[:4000]))
        else:
            conn.execute("DELETE FROM tb_personal_notes WHERE discord_id=? AND planet=? AND mission=?",
                         (str(discord_id), planet, mission))
