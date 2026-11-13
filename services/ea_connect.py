"""EA email/code enrollment and bounded on-demand session restoration.

Only game_lab's explicitly enrolled accounts. Credentials and browser cookies
are encrypted with the existing vault key; browser state never enters cookies.
"""
import base64
import json
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

import grpc
import httpx

import database
from services import ea_browser, game_account as ga, game_lab as lab

TTL = 600


class ReconnectRequired(ga.AccountError):
    pass


@contextmanager
def _db():
    conn = sqlite3.connect(database.DB_NAME, timeout=15)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("""CREATE TABLE IF NOT EXISTS ea_account_links (
            target TEXT PRIMARY KEY, expected_id TEXT NOT NULL, secret BLOB NOT NULL, updated_at REAL NOT NULL)""")
        conn.execute("""CREATE TABLE IF NOT EXISTS ea_browser_pending (
            id TEXT PRIMARY KEY, owner TEXT NOT NULL, target TEXT NOT NULL,
            secret BLOB, expires REAL NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'starting', created_at REAL NOT NULL)""")
        conn.commit()
        with conn:
            yield conn
    finally:
        conn.close()


def config():
    path = Path(database.DB_NAME).resolve().parent / "cache" / "ea-client.encrypted"
    try:
        return json.loads(ga._cipher().decrypt(path.read_bytes()))
    except Exception:
        raise ga.AccountError("Подключение EA ещё не настроено на сервере.") from None


def available():
    try:
        config()
        return True
    except ga.AccountError:
        return False


def linked(target):
    with _db() as conn:
        return bool(conn.execute("SELECT 1 FROM ea_account_links WHERE target=?", (target,)).fetchone())


def target_for_registration(owner, guild_id, ally_code):
    if guild_id != 1:
        return None
    conn = sqlite3.connect(database.DB_NAME, timeout=15)
    try:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not {"ea_account_links", "game_lab_accounts"} <= tables:
            return None
        row = conn.execute("SELECT l.id FROM game_lab_accounts l JOIN ea_account_links e ON e.target=l.id "
                           "WHERE l.added_by=? AND l.ally_code=? AND l.player_id=e.expected_id",
                           (str(owner), str(ally_code))).fetchone()
        return row[0] if row else None
    finally:
        conn.close()


def pending(owner, target):
    with _db() as conn:
        row = conn.execute("SELECT id,status FROM ea_browser_pending WHERE owner=? AND target=? AND expires>? "
                           "AND status='code' ORDER BY created_at DESC LIMIT 1", (str(owner), target, time.time())).fetchone()
    return {"id": row["id"], "status": row["status"]} if row else None


async def start(owner, target, email, consent):
    lab._row(target)
    return await _start_flow(owner, target, email, consent)


def registration_target(owner, is_alt):
    return "registration:" + str(owner) + (":alt" if is_alt else ":main")


async def start_registration(owner, is_alt, email, consent):
    ga.require_pilot(owner, 1, "test")
    return await _start_flow(owner, registration_target(owner, is_alt), email, consent,
                             registration={"is_alt": bool(is_alt)})


async def _start_flow(owner, target, email, consent, registration=None):
    if not consent:
        raise ga.AccountError("Разрешите EA-подключение и чтение выбранного тестового аккаунта.")
    cfg = config()
    now = time.time()
    flow_id = uuid.uuid4().hex
    with _db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("DELETE FROM ea_browser_pending WHERE expires<=?", (now,))
        if conn.execute("SELECT 1 FROM ea_browser_pending WHERE owner=? AND created_at>?", (str(owner), now - 60)).fetchone():
            raise ga.AccountError("Повторная отправка кода доступна через минуту.")
        conn.execute("DELETE FROM ea_browser_pending WHERE owner=? AND target=?", (str(owner), target))
        conn.execute("INSERT INTO ea_browser_pending (id,owner,target,expires,created_at) VALUES (?,?,?,?,?)",
                     (flow_id, str(owner), target, now + TTL, now))
    try:
        state = await ea_browser.start(cfg, email.strip())
        if state["stage"] != "code":
            raise ga.AccountError("EA не запросила код подтверждения. Начните вход заново.")
        stored = {"browser": state, "registration": registration} if registration is not None else state
        encrypted = ga._cipher().encrypt(json.dumps(stored).encode())
        with _db() as conn:
            conn.execute("UPDATE ea_browser_pending SET secret=?,status='code' WHERE id=?", (encrypted, flow_id))
    except Exception as error:
        with _db() as conn:
            conn.execute("UPDATE ea_browser_pending SET status='failed',secret=NULL,expires=? WHERE id=?", (time.time() + 60, flow_id))
        if isinstance(error, ga.AccountError):
            raise
        raise ga.AccountError("Не удалось начать вход EA. Повторите позже.") from None
    return flow_id


def _wrapped(values):
    return ga.pack([(number, 2, ga.pack([(1, 2, value.encode() if isinstance(value, str) else value)])) for number, value in values])


async def _grpc(service, method, values):
    allowed = {("AuthService", "GetAuthForMobileAppAndroidClient"),
               ("TokenService", "GrantTokenByAuthorizationCode"), ("TokenService", "GrantTokenByRefreshToken")}
    if (service, method) not in allowed:
        raise ga.AccountError("Недопустимая операция EA.")
    try:
        async with grpc.aio.secure_channel("accounts.grpc.ea.com:443", grpc.ssl_channel_credentials()) as channel:
            raw = await channel.unary_unary("/eadp.nexus.connect.grpc.v1." + service + "/" + method)(_wrapped(values), timeout=20)
    except grpc.RpcError as error:
        if error.code() in (grpc.StatusCode.UNAUTHENTICATED, grpc.StatusCode.PERMISSION_DENIED, grpc.StatusCode.INVALID_ARGUMENT):
            raise ReconnectRequired("EA не продлила доступ. Подключите аккаунт заново по почте и коду.") from None
        raise ga.AccountError("EA временно недоступна или ограничила запросы. Повторите позже.") from None
    result = {}
    for number, wire, value in ga.fields(raw):
        if wire == 2:
            result[number] = next((v for n, w, v in ga.fields(value) if n == 1), None)
    return result


def _token_result(result, previous=None):
    previous = previous or {}
    access = result.get(1)
    refresh = result.get(4) or previous.get("refresh", "").encode()
    if not access or not refresh:
        raise ReconnectRequired("EA не выдала длительный доступ. Подключите аккаунт заново.")
    try:
        claims = json.loads(base64.urlsafe_b64decode(access.split(b".")[1] + b"==="))
        nucleus = str(claims["nexus"]["pid"])
        expires = int(result.get(3) or 0)
        if not nucleus or expires <= 0:
            raise ValueError()
    except Exception:
        raise ga.AccountError("Не удалось проверить ответ EA.") from None
    if previous.get("nucleus") and previous["nucleus"] != nucleus:
        raise ga.AccountError("EA вернула другую учётную запись. Вход остановлен.")
    return {**previous, "access": access.decode(), "refresh": refresh.decode(), "nucleus": nucleus,
            "expires_at": time.time() + expires}


async def _exchange(cfg, longlive):
    auth = await _grpc("AuthService", "GetAuthForMobileAppAndroidClient", [
        (1, cfg["client_id"]), (3, "code"), (4, "nucleus:rest"), (2201, "mobile_ea_account"), (2207, longlive)])
    if not auth.get(1):
        raise ReconnectRequired("EA не подтвердила вход. Запросите новый код.")
    token = await _grpc("TokenService", "GrantTokenByAuthorizationCode", [
        (1, cfg["client_id"]), (2, cfg["client_secret"]), (3, auth[1]), (4, "JWS"), (103, "nucleus:rest")])
    return _token_result(token, {"longlive": longlive})


async def _renew(cfg, credentials):
    if credentials.get("expires_at", 0) > time.time() + 300:
        return credentials
    try:
        result = await _grpc("TokenService", "GrantTokenByRefreshToken", [
            (1, cfg["client_id"]), (2, cfg["client_secret"]), (4, "JWS"), (8, credentials["refresh"])])
        return _token_result(result, credentials)
    except ReconnectRequired:
        renewed = await _exchange(cfg, credentials["longlive"])
        if renewed["nucleus"] != credentials["nucleus"]:
            raise ga.AccountError("EA вернула другую учётную запись. Продление остановлено.")
        return {**credentials, **renewed}


def _envelope(template, service, method, payload, credentials=None, device=None):
    rows = ga.fields(base64.b64decode(template)) if isinstance(template, str) else ga.fields(template)
    now = int(time.time())
    result = [(n, w, now if n in (11, 20) and w == 0 else str(uuid.uuid4()).encode() if n == 16 and w == 2
               else device if n == 32 and w == 2 and device else v) for n, w, v in rows if n not in (4, 5, 6, 7, 8)]
    result.extend([(4, 2, service.encode()), (5, 2, method.encode()), (6, 2, payload)])
    if credentials:
        result.extend([(7, 2, credentials[0]), (8, 2, credentials[1])])
    return ga.pack(result)


async def _game(service, method, request):
    if (service, method) not in {("AuthRpc", "DoAuthGuest"), ("PlayerRpc", "GetNucleusPlayerPreview"), ("PlayerRpc", "LinkNucleusAccount")}:
        raise ga.AccountError("Недопустимая операция подключения.")
    try:
        async with httpx.AsyncClient(timeout=25, follow_redirects=False) as client:
            async with client.stream("POST", "https://sw-live.cgswprod.com/rpc", content=request,
                                     headers={"Content-Type": "application/x-protobuf", "X-Rpc-Service-Name": service,
                                              "X-Rpc-Method-Name": method, "Accept-Encoding": "identity"}) as response:
                if response.status_code != 200:
                    raise ga.AccountError("Игра временно недоступна.")
                chunks, size = [], 0
                async for chunk in response.aiter_raw():
                    size += len(chunk)
                    if size > ga.MAX_BYTES:
                        raise ga.AccountError("Ответ игры слишком большой.")
                    chunks.append(chunk)
    except httpx.HTTPError:
        raise ga.AccountError("Не удалось связаться с игрой.") from None
    envelope = ga.fields(ga._unzip(b"".join(chunks)))
    code = ga._one(envelope, 5, 0)
    if code != 1:
        raise ga.AccountError(f"Игра отклонила подключение (код {code}).")
    return ga._unzip(next((v for n, w, v in envelope if n == 4 and w == 2), b""))


def _auth_result(payload, expected=None):
    fields = ga.fields(payload)
    player, token = ga._one(fields, 1, 2), ga._one(fields, 2, 2)
    if expected and player.decode() != expected:
        raise ga.AccountError("Игра вернула другой аккаунт. Его данные не запрашивались.")
    return player, token


def _read_request(cfg, player, token, device=None):
    template = cfg["read_template"]
    payload = ga._one(ga.fields(base64.b64decode(template)), 6, 2)
    return _envelope(template, "PlayerRpc", "GetInitialData", payload, (player, token), device)


async def _import(cfg, tokens, expected, validate_preview=None):
    if expected is None and validate_preview is None:
        raise ga.AccountError("Не указан игрок для подключения.")
    uid, device = uuid.uuid4().hex.encode(), uuid.uuid4().hex.encode()
    template = cfg["guest_template"]
    rows = ga.fields(ga._one(ga.fields(base64.b64decode(template)), 6, 2))
    ga._one(rows, 1, 2)  # the device UID must be present before replacing it
    guest_payload = ga.pack([(n, w, uid if n == 1 and w == 2 else v) for n, w, v in rows])
    guest_request = _envelope(template, "AuthRpc", "DoAuthGuest", guest_payload, device=device)
    guest, token = _auth_result(await _game("AuthRpc", "DoAuthGuest", guest_request))
    if guest.decode() == expected:
        raise ga.AccountError("Не удалось создать отдельное устройство для подключения.")
    preview_request = _envelope(guest_request, "PlayerRpc", "GetNucleusPlayerPreview", ga.pack([(2, 2, tokens["access"].encode())]), (guest, token))
    preview = ga.fields(await _game("PlayerRpc", "GetNucleusPlayerPreview", preview_request))
    preview_id = ga._one(preview, 5, 2).decode()
    if (expected is not None and preview_id != expected) or ga._one(preview, 6, 2).decode() != tokens["nucleus"]:
        raise ga.AccountError("Эта почта EA связана с другим игроком. Выбранный аккаунт не подключён.")
    if validate_preview is not None:
        await validate_preview(preview_id)
    expected = preview_id
    # This restores the selected existing account to our isolated device; never
    # call LinkNewEAConnect or link an EA identity to an existing game account.
    await _game("PlayerRpc", "LinkNucleusAccount", _envelope(guest_request, "PlayerRpc", "LinkNucleusAccount", b"", (guest, token)))
    fresh = _envelope(guest_request, "AuthRpc", "DoAuthGuest", guest_payload)
    player, token = _auth_result(await _game("AuthRpc", "DoAuthGuest", fresh), expected)
    request = _read_request(cfg, player, token, device)
    summary = await ga.read_initial(request, expected)
    return {**tokens, "expected_id": expected, "guest_request": base64.b64encode(fresh).decode(),
            "read_request": base64.b64encode(request).decode(), "device": device.decode()}, summary


def _save(target, credentials, summary, previous_secret=None, pending_id=None):
    row = lab._row(target)
    if row["player_id"] != credentials["expected_id"]:
        raise ga.AccountError("Выбранный тестовый аккаунт изменился.")
    encrypted = ga._cipher().encrypt(json.dumps(credentials).encode())
    game_secret = None
    owner = lab.NOTEST_OWNER if target == "notest" else row["added_by"]
    own = database.get_user_registrations(owner, guild_id=1)
    if target == "notest" or any(str(r[0]) == row["ally_code"] for r in own):
        ga.require_pilot(owner, 1, "test")
        if ga._identity(owner, 1, row["ally_code"]) != credentials["expected_id"]:
            raise ga.AccountError("Привязка игрока изменилась.")
        request = base64.b64decode(credentials["read_request"])
        ga.read_request(request, credentials["expected_id"])
        game_secret = ga._cipher().encrypt(json.dumps({"request": credentials["read_request"], "player_id": credentials["expected_id"]}).encode())
        with ga._db():
            pass  # ensure the current per-account schema before the transaction
    with _db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        if pending_id is not None and not conn.execute("SELECT 1 FROM ea_browser_pending WHERE id=? AND target=? AND status='checking' AND expires>?", (pending_id, target, time.time())).fetchone():
            raise ReconnectRequired("Подключение отменено или истекло. Начните его заново.")
        if previous_secret is not None and not conn.execute("SELECT 1 FROM ea_account_links WHERE target=? AND secret=?", (target, previous_secret)).fetchone():
            raise ReconnectRequired("Доступ отключён или заменён во время запроса. Чтение остановлено.")
        changed = conn.execute("UPDATE game_lab_accounts SET provider='ea',summary=?,state='connected' WHERE id=?", (json.dumps(summary), target))
        if not changed.rowcount:
            raise ReconnectRequired("Аккаунт удалён из испытаний.")
        conn.execute("INSERT OR REPLACE INTO ea_account_links VALUES (?,?,?,?)", (target, row["player_id"], encrypted, time.time()))
        if game_secret is not None:
            now = time.time()
            conn.execute("INSERT OR REPLACE INTO game_account_sessions VALUES (?,?,?,?,?,?,?,?,?)", (owner, 1, row["ally_code"], "ea", game_secret, now, now, json.dumps(summary), "connected"))


async def finish(owner, target, flow_id, code):
    return await _finish_flow(owner, target, flow_id, code, row=lab._row(target))


async def finish_registration(owner, is_alt, flow_id, code, comlink):
    ga.require_pilot(owner, 1, "test")
    return await _finish_flow(owner, registration_target(owner, is_alt), flow_id, code,
                              is_alt=bool(is_alt), comlink=comlink)


async def _finish_flow(owner, target, flow_id, code, row=None, is_alt=None, comlink=None):
    if not code.strip().isascii() or not code.strip().isdigit() or not 4 <= len(code.strip()) <= 10:
        raise ga.AccountError("Введите цифровой код из письма EA.")
    now = time.time()
    with _db() as conn:
        flow = conn.execute("SELECT * FROM ea_browser_pending WHERE id=? AND owner=? AND target=?", (flow_id, str(owner), target)).fetchone()
        if not flow or flow["expires"] <= now or flow["attempts"] >= 5 or flow["status"] != "code":
            raise ga.AccountError("Подтверждение истекло или уже используется. Запросите новый код.")
        changed = conn.execute("UPDATE ea_browser_pending SET status='checking',attempts=attempts+1 WHERE id=? AND status='code'", (flow_id,))
        if not changed.rowcount:
            raise ga.AccountError("Код уже проверяется.")
    try:
        state = json.loads(ga._cipher().decrypt(flow["secret"]))
        registration = state.get("registration")
        if row is None and (registration != {"is_alt": is_alt} or target != registration_target(owner, is_alt)):
            raise ga.AccountError("Тип привязки изменился. Начните вход заново.")
        result = await ea_browser.finish(state.get("browser", state), code)
        if result["stage"] == "code":
            with _db() as conn:
                conn.execute("UPDATE ea_browser_pending SET status='code',secret=? WHERE id=?",
                             (ga._cipher().encrypt(json.dumps({"browser": result, "registration": registration} if registration is not None else result).encode()), flow_id))
            raise ga.AccountError("EA не приняла код. Проверьте последнее письмо и повторите ввод.")
        if result["stage"] != "complete":
            raise ga.AccountError("EA не завершила вход. Начните подключение заново.")
        cfg = config()
        tokens = await _exchange(cfg, result["longlive"])
        if row is not None:
            await lab._reserve(target)
            credentials, summary = await _import(cfg, tokens, row["player_id"])
        else:
            from services import account_linking
            player = {}
            async def validate(preview_id):
                player.update(await account_linking.validate_player(owner, is_alt, preview_id, comlink))
            credentials, summary = await _import(cfg, tokens, None, validate_preview=validate)
            # Only register after the private read has verified the same player.
            target = account_linking.enroll(owner, is_alt, player, pending_id=flow_id, pending_target=target)
        _save(target, credentials, summary, pending_id=flow_id)
        with _db() as conn:
            conn.execute("DELETE FROM ea_browser_pending WHERE id=?", (flow_id,))
        return {**summary, "target": target} if row is None else summary
    except Exception as error:
        with _db() as conn:
            conn.execute("DELETE FROM ea_browser_pending WHERE id=? AND status='checking'", (flow_id,))
        if isinstance(error, ga.AccountError):
            raise
        raise ga.AccountError("Не удалось завершить подключение EA. Начните вход заново.") from None


async def read(target):
    row = lab._row(target)
    await lab._reserve(target)
    with _db() as conn:
        link = conn.execute("SELECT * FROM ea_account_links WHERE target=?", (target,)).fetchone()
    if not link or link["expected_id"] != row["player_id"]:
        raise ReconnectRequired("Подключите выбранный аккаунт через EA.")
    previous_secret = link["secret"]
    try:
        credentials = json.loads(ga._cipher().decrypt(link["secret"]))
        if credentials["expected_id"] != row["player_id"]:
            raise ga.AccountError("Сохранённый доступ относится к другому аккаунту.")
        cfg = config()
        renewed = await _renew(cfg, credentials)
        if renewed != credentials:
            # Persist rotated refresh tokens even if the following game read fails.
            encrypted = ga._cipher().encrypt(json.dumps(renewed).encode())
            with _db() as conn:
                changed = conn.execute("UPDATE ea_account_links SET secret=?,updated_at=? WHERE target=? AND secret=?",
                                       (encrypted, time.time(), target, previous_secret))
                if not changed.rowcount:
                    raise ReconnectRequired("Доступ отключён или заменён во время запроса.")
            previous_secret = encrypted
            credentials = renewed
        request = base64.b64decode(credentials["read_request"])
        try:
            summary = await ga.read_initial(request, row["player_id"])
        except ga.SessionExpired:
            guest = base64.b64decode(credentials["guest_request"])
            payload = ga._one(ga.fields(guest), 6, 2)
            fresh = _envelope(guest, "AuthRpc", "DoAuthGuest", payload)
            player, token = _auth_result(await _game("AuthRpc", "DoAuthGuest", fresh), row["player_id"])
            request = _read_request(cfg, player, token, credentials["device"].encode())
            credentials["read_request"] = base64.b64encode(request).decode()
            summary = await ga.read_initial(request, row["player_id"])
        _save(target, credentials, summary, previous_secret=previous_secret)
        return summary
    except Exception as error:
        with lab._db() as conn:
            conn.execute("UPDATE game_lab_accounts SET state='error' WHERE id=? AND EXISTS "
                         "(SELECT 1 FROM ea_account_links WHERE target=? AND secret=?)", (target, target, previous_secret))
        if isinstance(error, ga.AccountError):
            raise
        raise ga.AccountError("Не удалось восстановить доступ. Подключите аккаунт через EA заново.") from None


def disconnect(target):
    with _db() as conn:
        conn.execute("DELETE FROM ea_account_links WHERE target=?", (target,))
        conn.execute("DELETE FROM ea_browser_pending WHERE target=?", (target,))
