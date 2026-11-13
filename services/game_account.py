"""Authenticated SWGOH pilot. Only an existing session and one read RPC.

Provider onboarding is deliberately separate: a site Google identity is not a
game session. No login, refresh, linking, mod changes or automatic retries here.
"""
import base64
import json
import sqlite3
import time
import uuid
import zlib
from pathlib import Path
from contextlib import contextmanager

import httpx

import database
import guild_resolver

MAX_BYTES = 16 * 1024 * 1024
COOLDOWN = 60


class AccountError(ValueError):
    pass


class SessionExpired(AccountError):
    pass


def require_pilot(owner, guild_id, profile, tier=None):
    access = guild_resolver.resolve_access(str(owner))
    # Do not let web guild switching, view-as or the prod worker broaden access.
    if (profile != "test" or guild_id != 1 or access.get("guild_id") != 1
            or access.get("tier") != "officer" or tier not in (None, "officer")):
        raise AccountError("Пилот доступен только офицерам АС в тестовой версии.")
    from services import bot_profiles
    if bot_profiles.guild_profile(1) != "test":
        raise AccountError("Пилот доступен только в тестовой версии.")


@contextmanager
def _db():
    conn = sqlite3.connect(database.DB_NAME, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("BEGIN IMMEDIATE")
    conn.execute("""CREATE TABLE IF NOT EXISTS game_account_sessions (
        owner TEXT NOT NULL, guild_id INTEGER NOT NULL, ally_code TEXT NOT NULL,
        provider TEXT NOT NULL, secret BLOB NOT NULL, consent_at REAL NOT NULL,
        last_attempt REAL NOT NULL DEFAULT 0, summary TEXT, state TEXT NOT NULL,
        PRIMARY KEY (owner, guild_id, ally_code))""")
    primary_key = [r[1] for r in conn.execute("PRAGMA table_info(game_account_sessions)") if r[5]]
    if primary_key == ["owner", "guild_id"]:
        conn.execute("ALTER TABLE game_account_sessions RENAME TO game_account_sessions_single")
        conn.execute("""CREATE TABLE game_account_sessions (
            owner TEXT NOT NULL, guild_id INTEGER NOT NULL, ally_code TEXT NOT NULL,
            provider TEXT NOT NULL, secret BLOB NOT NULL, consent_at REAL NOT NULL,
            last_attempt REAL NOT NULL DEFAULT 0, summary TEXT, state TEXT NOT NULL,
            PRIMARY KEY (owner, guild_id, ally_code))""")
        conn.execute("INSERT INTO game_account_sessions SELECT * FROM game_account_sessions_single")
        conn.execute("DROP TABLE game_account_sessions_single")
    conn.commit()
    try:
        with conn:
            yield conn
    finally:
        conn.close()


def _cipher():
    # Provision the key outside SQLite; never create a replacement on read.
    from cryptography.fernet import Fernet
    path = Path(database.DB_NAME).resolve().parent / "cache" / "game-account.key"
    try:
        return Fernet(path.read_bytes())
    except (OSError, ValueError):
        raise AccountError("Хранилище игровых сессий ещё не настроено.") from None


def fields(raw):
    if len(raw) > MAX_BYTES:
        raise AccountError("Ответ игры слишком большой.")
    pos = 0

    def varint():
        nonlocal pos
        value = 0
        for shift in range(0, 70, 7):
            if pos >= len(raw):
                raise AccountError("Неполный ответ игры.")
            b = raw[pos]
            pos += 1
            value |= (b & 127) << shift
            if b < 128:
                return value
        raise AccountError("Некорректный ответ игры.")

    result = []
    while pos < len(raw):
        key = varint()
        number, wire = key >> 3, key & 7
        if not number:
            raise AccountError("Некорректный ответ игры.")
        if wire == 0:
            value = varint()
        elif wire in (1, 2, 5):
            size = varint() if wire == 2 else (8 if wire == 1 else 4)
            if size > len(raw) - pos:
                raise AccountError("Неполный ответ игры.")
            value = raw[pos:pos + size]
            pos += size
        else:
            raise AccountError("Неизвестный формат ответа игры.")
        result.append((number, wire, value))
    return result


def _varint(value):
    out = bytearray()
    while value > 127:
        out.append((value & 127) | 128)
        value >>= 7
    return bytes(out) + bytes([value])


def pack(rows):
    out = bytearray()
    for number, wire, value in rows:
        out += _varint(number << 3 | wire)
        out += _varint(value) if wire == 0 else (_varint(len(value)) + value if wire == 2 else value)
    return bytes(out)


def _one(rows, number, wire):
    matches = [v for n, w, v in rows if n == number and w == wire]
    if len(matches) != 1:
        raise AccountError("В игровой сессии отсутствует обязательное поле.")
    return matches[0]


def read_request(raw, expected_id):
    rows = fields(raw)
    if (_one(rows, 4, 2) != b"PlayerRpc" or _one(rows, 5, 2) != b"GetInitialData"
            or _one(rows, 7, 2) != expected_id.encode() or not _one(rows, 8, 2)):
        raise AccountError("Сессия не соответствует аккаунту или операции чтения.")
    now = int(time.time())
    return pack([(n, w, now if n in (11, 20) and w == 0 else
                  str(uuid.uuid4()).encode() if n == 16 and w == 2 else v)
                 for n, w, v in rows])


def _unzip(raw):
    if not raw.startswith(b"\x1f\x8b"):
        return raw
    decoder = zlib.decompressobj(16 + zlib.MAX_WBITS)
    result = decoder.decompress(raw, MAX_BYTES + 1)
    if len(result) > MAX_BYTES or not decoder.eof:
        raise AccountError("Ответ игры слишком большой или повреждён.")
    return result


async def read_initial(raw, expected_id, *, report=None):
    body = read_request(raw, expected_id)
    response = await _read_payload(body, "PlayerRpc", "GetInitialData")
    payload = fields(response)
    if _one(payload, 1, 2) != expected_id.encode():
        raise AccountError("Игра вернула другой аккаунт. Данные не сохранены.")
    inventory = _one(payload, 5, 2)
    if report is not None:
        from services.game_read_decode import decode
        report.extend(decode(response, "initial"))
    return {"read_at": int(time.time()), "inventory_bytes": len(inventory),
            "inventory_records": len(fields(inventory))}


async def _read_payload(body, service, method, *, allow_empty=False):
    try:
        async with httpx.AsyncClient(timeout=25, follow_redirects=False) as client:
            async with client.stream("POST", "https://sw-live.cgswprod.com/rpc", content=body,
                                     headers={"Content-Type": "application/x-protobuf",
                                              "X-Rpc-Service-Name": service,
                                              "X-Rpc-Method-Name": method,
                                              "Accept-Encoding": "identity"}) as response:
                if response.status_code != 200:
                    raise AccountError("Игра временно недоступна. Повторите позже.")
                chunks, size = [], 0
                async for chunk in response.aiter_raw():
                    size += len(chunk)
                    if size > MAX_BYTES:
                        raise AccountError("Ответ игры слишком большой.")
                    chunks.append(chunk)
    except httpx.HTTPError:
        raise AccountError("Не удалось связаться с игрой.") from None
    envelope = fields(_unzip(b"".join(chunks)))
    code = _one(envelope, 5, 0)
    if code == 5:
        raise SessionExpired("Игровая сессия завершилась. Нужно повторное подключение.")
    if code != 1:
        raise AccountError(f"Игра отклонила чтение (код {code}).")
    if allow_empty and not any(n == 4 for n, _, _ in envelope):
        return b""
    return _unzip(_one(envelope, 4, 2))


def _identity(owner, guild_id, ally_code):
    own = database.get_user_registrations(str(owner), guild_id=guild_id)
    if str(ally_code) not in {str(r[0]) for r in own}:
        raise AccountError("Можно подключить только свой зарегистрированный аккаунт.")
    for player_id, (code, _name) in database.get_roster_by_player_id(guild_id).items():
        if str(code) == str(ally_code):
            return player_id
    from services import test_accounts
    test_player_id = test_accounts.player_id(owner, guild_id, ally_code)
    if test_player_id:
        return test_player_id
    raise AccountError("Аккаунт отсутствует в текущем составе АС.")


def select_account(owner, guild_id, *, alt=False, ally_code=None):
    """Select a registered main/alt, never silently fall back to a different session."""
    own = database.get_user_registrations(str(owner), guild_id=guild_id)
    if ally_code:
        if str(ally_code) not in {str(r[0]) for r in own}:
            raise AccountError("Можно выбрать только свой зарегистрированный аккаунт.")
        return str(ally_code)
    candidates = [r for r in own if bool(r[2]) != bool(alt)]
    if not candidates:
        raise AccountError("Второй аккаунт не найден." if alt else "Основной аккаунт не зарегистрирован.")
    if len(candidates) > 1:
        raise AccountError("У вас несколько альтов — укажите код союзника.")
    return str(candidates[0][0])


async def provision(owner, guild_id, profile, ally_code, provider, request_bytes, *, consent=False):
    """Operator-only entry point, no public route accepting tokens or passwords.

    Call only after the owner has explicitly consented to this connection.
    Existing session must come from that owner's authorized login.
    """
    require_pilot(owner, guild_id, profile)
    if not consent or provider not in ("ea", "google"):
        raise AccountError("Нужно согласие владельца и способ подключения EA/Google.")
    expected = _identity(owner, guild_id, ally_code)
    cipher = _cipher()
    summary = await read_initial(request_bytes, expected)
    store_verified_session(owner, guild_id, ally_code, provider, request_bytes, summary)
    return summary


def store_verified_session(owner, guild_id, ally_code, provider, request_bytes, summary):
    """Internal persistence after a successful identity-checked read."""
    require_pilot(owner, guild_id, "test")
    expected = _identity(owner, guild_id, ally_code)
    read_request(request_bytes, expected)
    cipher = _cipher()
    secret = cipher.encrypt(json.dumps({"request": base64.b64encode(request_bytes).decode(),
                                        "player_id": expected}).encode())
    with _db() as conn:
        conn.execute("INSERT OR REPLACE INTO game_account_sessions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                     (str(owner), guild_id, str(ally_code), provider, secret, time.time(),
                      time.time(), json.dumps(summary), "connected"))


def status(owner, guild_id, profile, tier=None, *, ally_code=None):
    require_pilot(owner, guild_id, profile, tier)
    ally_code = select_account(owner, guild_id, ally_code=ally_code)
    with _db() as conn:
        row = conn.execute("SELECT ally_code, provider, summary, state FROM game_account_sessions "
                           "WHERE owner=? AND guild_id=? AND ally_code=?", (str(owner), guild_id, ally_code)).fetchone()
    if not row:
        return None
    _identity(owner, guild_id, row["ally_code"])
    return {"ally_code": row["ally_code"], "provider": row["provider"],
            "summary": json.loads(row["summary"]) if row["summary"] else None, "state": row["state"]}


async def refresh(owner, guild_id, profile, tier=None, *, ally_code=None, report=None):
    require_pilot(owner, guild_id, profile, tier)
    ally_code = select_account(owner, guild_id, ally_code=ally_code)
    from services import ea_connect
    target = ea_connect.target_for_registration(owner, guild_id, ally_code)
    if target:
        return await ea_connect.read(target, **({"report": report} if report is not None else {}))
    now = time.time()
    with _db() as conn:
        row = conn.execute("SELECT * FROM game_account_sessions WHERE owner=? AND guild_id=? AND ally_code=?",
                           (str(owner), guild_id, ally_code)).fetchone()
        if row is None:
            raise AccountError("Игровой аккаунт ещё не подключён.")
        expected = _identity(owner, guild_id, row["ally_code"])
        # Atomic lease works across bot/web processes and also limits failures.
        changed = conn.execute("UPDATE game_account_sessions SET last_attempt=? WHERE owner=? "
                               "AND guild_id=? AND ally_code=? AND last_attempt<=?", (now, str(owner), guild_id, ally_code, now - COOLDOWN))
        if not changed.rowcount:
            raise AccountError("Повторное чтение доступно через минуту.")
    try:
        data = json.loads(_cipher().decrypt(row["secret"]))
        if data["player_id"] != expected:
            raise AccountError("Привязка аккаунта изменилась. Подключите сессию заново.")
        options = {"report": report} if report is not None else {}
        summary = await read_initial(base64.b64decode(data["request"], validate=True), expected, **options)
    except AccountError:
        with _db() as conn:
            conn.execute("UPDATE game_account_sessions SET state='error' WHERE owner=? AND guild_id=? "
                         "AND ally_code=? AND secret=?", (str(owner), guild_id, ally_code, row["secret"]))
        raise
    except Exception:
        raise AccountError("Не удалось открыть игровую сессию. Требуется повторное подключение.") from None
    with _db() as conn:
        conn.execute("UPDATE game_account_sessions SET summary=?, state='connected' "
                     "WHERE owner=? AND guild_id=? AND ally_code=? AND secret=?",
                     (json.dumps(summary), str(owner), guild_id, ally_code, row["secret"]))
    return summary


def disconnect(owner, guild_id, profile, tier=None, *, ally_code=None):
    require_pilot(owner, guild_id, profile, tier)
    ally_code = select_account(owner, guild_id, ally_code=ally_code)
    from services import ea_connect
    target = ea_connect.target_for_registration(owner, guild_id, ally_code)
    if target:
        ea_connect.disconnect(target)
    with _db() as conn:
        conn.execute("DELETE FROM game_account_sessions WHERE owner=? AND guild_id=? AND ally_code=?", (str(owner), guild_id, ally_code))
