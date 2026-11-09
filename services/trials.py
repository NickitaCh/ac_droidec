"""One promotional 30-day trial per SWGOH guild, only on the public pair."""
import re
import sqlite3
from contextlib import contextmanager

import database


class TrialError(ValueError):
    pass


@contextmanager
def _db():
    c = sqlite3.connect(database.DB_NAME, timeout=20)
    c.row_factory = sqlite3.Row
    try:
        c.execute('''CREATE TABLE IF NOT EXISTS trial_promos (
            code TEXT PRIMARY KEY, max_uses INTEGER NOT NULL, uses INTEGER NOT NULL DEFAULT 0,
            expires_at TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 1)''')
        c.execute('''CREATE TABLE IF NOT EXISTS guild_trials (
            swgoh_guild_id TEXT PRIMARY KEY, guild_id INTEGER NOT NULL,
            activated_by TEXT NOT NULL, promo_code TEXT,
            started_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, expires_at TEXT NOT NULL)''')
        c.commit()
        c.execute('BEGIN IMMEDIATE')
        yield c
        c.commit()
    except BaseException:
        c.rollback()
        raise
    finally:
        c.close()


def campaign_enabled():
    return database.get_bot_state('trial_campaign_enabled', guild_id=0) != '0'


def activate(gid, actor, promo=''):
    promo = promo.strip().upper()
    # All eligibility, redemption and activation writes share one transaction.
    with _db() as c:
        g = c.execute('SELECT * FROM guilds WHERE id=?', (gid,)).fetchone()
        if not g or g['bot_profile'] != 'prod' or not g['swgoh_guild_id']:
            raise TrialError('Пробный период доступен только новым гильдиям публичного бота.')
        if g['is_active'] or g['subscription_expires_at'] or g['subscription_source'] != 'paid':
            raise TrialError('Гильдия уже пользовалась подключением. Доступно платное продление.')
        if c.execute('SELECT 1 FROM guild_trials WHERE swgoh_guild_id=?', (g['swgoh_guild_id'],)).fetchone():
            raise TrialError('Пробный период этой гильдии уже использован.')
        if c.execute("SELECT 1 FROM guilds WHERE swgoh_guild_id=? AND id<>? AND (is_active=1 OR subscription_expires_at IS NOT NULL OR subscription_source='manual')", (g['swgoh_guild_id'], gid)).fetchone():
            raise TrialError('Гильдия уже пользовалась подключением. Доступно платное продление.')
        if c.execute("SELECT 1 FROM sqlite_master WHERE name='payments'").fetchone():
            if c.execute("SELECT 1 FROM payments p JOIN guilds g ON g.id=p.guild_id WHERE g.swgoh_guild_id=? AND p.status='succeeded'", (g['swgoh_guild_id'],)).fetchone():
                raise TrialError('Гильдия уже оплачивала подписку. Доступно платное продление.')
        if promo:
            row = c.execute("SELECT * FROM trial_promos WHERE code=? AND enabled=1 AND uses<max_uses AND expires_at>datetime('now')", (promo,)).fetchone()
            if not row:
                raise TrialError('Промокод не найден, истёк или исчерпан.')
            c.execute('UPDATE trial_promos SET uses=uses+1 WHERE code=?', (promo,))
        else:
            row = c.execute("SELECT value FROM bot_state WHERE guild_id=0 AND key='trial_campaign_enabled'").fetchone() if c.execute("SELECT 1 FROM sqlite_master WHERE name='bot_state'").fetchone() else None
            if row and row[0] == '0':
                raise TrialError('Открытая акция завершена. Введите промокод или выберите оплату.')
        c.execute("INSERT INTO guild_trials(swgoh_guild_id,guild_id,activated_by,promo_code,expires_at) VALUES(?,?,?,?,datetime('now','+30 days'))",
                  (g['swgoh_guild_id'], gid, str(actor), promo or None))
        expires = c.execute('SELECT expires_at FROM guild_trials WHERE swgoh_guild_id=?', (g['swgoh_guild_id'],)).fetchone()[0]
        c.execute("UPDATE guilds SET is_active=1,subscription_source='trial',subscription_expires_at=? WHERE id=?", (expires, gid))
        return expires


def save_promo(code, max_uses, expires):
    code = code.strip().upper()
    if not re.fullmatch(r'[A-Z0-9_-]{3,40}', code):
        raise TrialError('Промокод: 3–40 латинских букв, цифр, дефисов или подчёркиваний.')
    if not 1 <= max_uses <= 100000:
        raise TrialError('Количество активаций: от 1 до 100000.')
    from datetime import datetime, timezone
    try:
        parsed = datetime.strptime(expires, '%Y-%m-%d').replace(tzinfo=timezone.utc)
    except ValueError:
        raise TrialError('Укажите дату окончания промокода.') from None
    if parsed <= datetime.now(timezone.utc):
        raise TrialError('Дата окончания должна быть в будущем.')
    with _db() as c:
        c.execute('INSERT INTO trial_promos(code,max_uses,expires_at) VALUES(?,?,?) ON CONFLICT(code) DO UPDATE SET max_uses=excluded.max_uses,expires_at=excluded.expires_at,enabled=1',
                  (code, max_uses, expires + ' 00:00:00'))


def promos():
    with _db() as c:
        return [dict(r) for r in c.execute('SELECT * FROM trial_promos ORDER BY code')]


def disable_promo(code):
    with _db() as c:
        c.execute('UPDATE trial_promos SET enabled=0 WHERE code=?', (code,))
