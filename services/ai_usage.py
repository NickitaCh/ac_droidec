"""Shared AI telemetry, without prompts, replies, keys or private player data."""

import hashlib
import json
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime, timezone

import requests

import database


@contextmanager
def connection():
    conn = sqlite3.connect(database.DB_NAME, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("""CREATE TABLE IF NOT EXISTS ai_request_usage (
        id INTEGER PRIMARY KEY, created REAL NOT NULL, source TEXT NOT NULL,
        kind TEXT NOT NULL, model TEXT NOT NULL, status INTEGER NOT NULL,
        outcome TEXT NOT NULL, latency REAL NOT NULL,
        input_tokens INTEGER NOT NULL, output_tokens INTEGER NOT NULL, cost REAL NOT NULL)""")
    conn.execute("CREATE INDEX IF NOT EXISTS ai_request_usage_created ON ai_request_usage(created)")
    conn.commit()
    try:
        with conn:
            yield conn
    finally:
        conn.close()


def record(source, kind, model, status, outcome, latency, usage=None):
    usage = usage if isinstance(usage, dict) else {}
    def number(key, cast):
        try:
            return max(cast(usage.get(key) or 0), 0)
        except (ValueError, TypeError):
            return 0
    now = time.time()
    with connection() as conn:
        conn.execute("INSERT INTO ai_request_usage(created,source,kind,model,status,outcome,latency,input_tokens,output_tokens,cost) VALUES (?,?,?,?,?,?,?,?,?,?)",
                     (now, source, kind, model, status, outcome, latency,
                      number('prompt_tokens', int), number('completion_tokens', int), number('cost', float)))
        conn.execute("DELETE FROM ai_request_usage WHERE created < ?", (now - 90 * 86400,))


def summary(now=None):
    now = time.time() if now is None else now
    start = datetime.fromtimestamp(now, timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    with connection() as conn:
        return [dict(row) for row in conn.execute("""SELECT source, count(*) AS attempts,
            sum(outcome='ok') AS ok, sum(outcome='rejected') AS rejected,
            sum(status=429) AS rate_limited, sum(input_tokens) AS input_tokens,
            sum(output_tokens) AS output_tokens, sum(cost) AS cost
            FROM ai_request_usage WHERE created>=? GROUP BY source ORDER BY attempts DESC""", (start,))]


def provider_quota(api_key, *, refresh=False):
    """GET /key reports the account-wide daily free quota, including other apps.

    Only documented counters are persisted; API-key metadata is discarded.
    A stale snapshot is explicitly marked and never presented as a live balance.
    """
    day = datetime.now(timezone.utc).date().isoformat()
    key = 'ai_quota:' + hashlib.sha256((api_key or '').encode()).hexdigest()[:16]
    raw = database.get_bot_state(key, guild_id=0)
    cached = json.loads(raw) if raw else None
    if cached and cached.get('day') != day:
        cached = None
    if cached and not refresh and time.time() - cached['checked_at'] < 60:
        return {**cached, 'stale': False}
    if api_key:
        try:
            response = requests.get('https://openrouter.ai/api/v1/key',
                                    headers={'Authorization': 'Bearer ' + api_key}, timeout=6)
            response.raise_for_status()
            data = response.json()['data']['free_model_daily_requests']
            if all(type(data.get(k)) is int for k in ('used', 'limit', 'remaining')) and data['limit'] > 0:
                result = {k: max(0, data[k]) for k in ('used', 'limit', 'remaining')}
                result.update(day=day, checked_at=time.time())
                database.set_bot_state(key, json.dumps(result), guild_id=0)
                return {**result, 'stale': False}
        except (requests.RequestException, KeyError, TypeError, ValueError):
            pass
    if cached:
        return {**cached, 'stale': True}
    return None
