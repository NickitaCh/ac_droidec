"""One-use Telegram website handoff, separate from Discord linking tokens."""
import hashlib
import secrets
import sqlite3
import time
from urllib.parse import urlencode

import database
import guild_resolver
from services import game_account
from tg_bot import store


def _db():
    conn = sqlite3.connect(database.DB_NAME, timeout=15)
    conn.execute("""CREATE TABLE IF NOT EXISTS telegram_web_logins (
        digest TEXT PRIMARY KEY, tg_id INTEGER NOT NULL, owner TEXT NOT NULL,
        profile TEXT NOT NULL, destination TEXT NOT NULL, expires REAL NOT NULL)""")
    conn.commit()
    return conn


def _digest(token):
    return hashlib.sha256(token.encode()).hexdigest()


def create_url(tg_id, owner, profile, destination, site_url):
    access = guild_resolver.resolve_access(owner)
    game_account.require_account_read(owner, access.get("guild_id"), profile, access.get("tier"))
    if not destination.startswith("/game-account/link?"):
        raise game_account.AccountError("Недопустимая страница подключения.")
    token = secrets.token_urlsafe(32)
    conn = _db()
    try:
        with conn:
            conn.execute("DELETE FROM telegram_web_logins WHERE expires<? OR tg_id=?", (time.time(), tg_id))
            conn.execute("INSERT INTO telegram_web_logins VALUES (?,?,?,?,?,?)",
                         (_digest(token), tg_id, str(owner), profile, destination, time.time() + 600))
    finally:
        conn.close()
    return site_url.rstrip("/") + "/login/telegram?" + urlencode({"token": token})


def lookup(token, *, consume=False):
    if not token or len(token) > 100:
        return None
    # Ensure tg_users exists before opening the atomic transaction.
    setup = store._connect()
    setup.close()
    conn = _db()
    try:
        with conn:
            if consume:
                conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT l.owner,l.profile,l.destination,u.full_name FROM telegram_web_logins l "
                "JOIN tg_users u ON u.tg_user_id=l.tg_id AND u.owner_id=l.owner "
                "WHERE digest=? AND expires>?", (_digest(token), time.time())).fetchone()
            if row and consume:
                conn.execute("DELETE FROM telegram_web_logins WHERE digest=?", (_digest(token),))
        return row
    finally:
        conn.close()
