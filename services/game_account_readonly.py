"""Read the owner's saved game session without refreshing or logging in."""
import base64
from contextlib import closing
import json
from pathlib import Path
import sqlite3

import database
from services import game_account as ga, game_event_live, game_personal_modes, game_read_probes, tb_live


_EVENT_READS = {
    "tb": ("TerritoryRpc", "GetTerritoryMapStatuses", ((1, 0, 9),), tb_live.parse),
    "tw": ("TerritoryRpc", "GetTerritoryMapStatuses", ((1, 0, 8),), game_event_live.parse_tw),
    "raid": ("GuildRpc", "GetGuildRaidStatuses", (), game_event_live.parse_raid),
    "arena": ("TerritoryRpc", "GetTerritoryMapStatuses", ((1, 0, 10),), game_personal_modes.arena),
    "conquest": ("ConquestRpc", "GetConquestStatus", (), game_personal_modes.conquest),
}


def _connection(owner, ally_code):
    uri = Path(database.DB_NAME).resolve().as_uri() + "?mode=ro"
    with closing(sqlite3.connect(uri, uri=True)) as db:
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not {"ea_account_links", "game_lab_accounts"} <= tables:
            return None
        return db.execute(
            "SELECT l.id, e.expected_id, e.secret FROM game_lab_accounts l "
            "JOIN ea_account_links e ON e.target=l.id AND e.expected_id=l.player_id "
            "WHERE l.added_by=? AND l.ally_code=?",
            (str(owner), str(ally_code)),
        ).fetchone()


def own_main(user, *, connected_only=True, member_read=False):
    """Return the main ally code of an eligible officer."""
    guild_id, profile = user.get("guild_id"), user.get("bot_profile")
    owner = str(user["discord_id"])
    try:
        authorize = ga.require_account_read if member_read else ga.require_pilot
        authorize(owner, guild_id, profile, user["tier"])
        code = ga.select_account(owner, guild_id)
    except ga.AccountError:
        return None
    return code if not connected_only or _connection(owner, code) else None


def _group(rows, label):
    return [dict(data) for key, entries in rows if key.startswith(label + " · ") and isinstance(entries, list)
            for _, data in entries if isinstance(data, list)]


def _load(owner, guild_id, profile, tier, ally_code, *, optimizer_read=False):
    from cryptography.fernet import InvalidToken

    authorize = ga.require_account_read if optimizer_read else ga.require_pilot
    authorize(owner, guild_id, profile, tier)
    code = ga.select_account(owner, guild_id, ally_code=ally_code)
    connection = _connection(owner, code)
    if connection is None:
        raise ga.ConnectionRequired("Подключите аккаунт через EA.")
    _, expected_id, secret = connection
    cipher = ga._cipher()
    try:
        credentials = json.loads(cipher.decrypt(secret))
        account_id = credentials["expected_id"]
    except (InvalidToken, KeyError, TypeError, ValueError):
        raise ga.ConnectionRequired("Подключите аккаунт через EA заново.") from None
    if account_id != expected_id:
        raise ga.AccountError("Сохранённая сессия относится к другому аккаунту.")

    def is_current(current_connection=connection):
        authorize(owner, guild_id, profile, tier)
        return (ga.select_account(owner, guild_id, ally_code=code) == code
                and _connection(owner, code) == current_connection)

    return connection, credentials, is_current


async def _read_event(raw, expected_id, event, is_current):
    if not is_current():
        raise ga.AccountError("Подключение изменилось.")
    service, method, arguments, parse = _EVENT_READS[event]
    rows = ga.fields(ga.read_request(raw, expected_id))
    body = ga.pack([row for row in rows if row[0] not in (4, 5, 6)] + [
        (4, 2, service.encode()), (5, 2, method.encode()), (6, 2, ga.pack(arguments)),
    ])
    payload = await ga._read_payload(body, service, method, allow_empty=True)
    if not is_current():
        raise ga.AccountError("Подключение изменилось во время чтения.")
    return parse(payload)


async def login_for_events(owner, guild_id, profile, tier, ally_code):
    """One explicitly requested login, returning a reader with no login fallback."""
    from services import ea_connect

    connection, credentials, is_current = _load(owner, guild_id, profile, tier, ally_code)
    target, expected_id, secret = connection
    try:
        guest = base64.b64decode(credentials["guest_request"], validate=True)
        device = credentials["device"].encode()
    except (KeyError, AttributeError, TypeError, ValueError):
        raise ga.AccountError("В сохранённом подключении нет данных для игрового входа.") from None
    rows = ga.fields(guest)
    if ga._one(rows, 4, 2) != b"AuthRpc" or ga._one(rows, 5, 2) != b"DoAuthGuest":
        raise ga.AccountError("Сохранённое подключение не соответствует игровому входу.")
    config = ea_connect.config()
    if not is_current():
        raise ga.AccountError("Подключение изменилось до входа.")
    fresh = ea_connect._envelope(guest, "AuthRpc", "DoAuthGuest", ga._one(rows, 6, 2))
    player, token = ea_connect._auth_result(await ea_connect._game("AuthRpc", "DoAuthGuest", fresh), expected_id)
    raw = ea_connect._read_request(config, player, token, device)

    credentials["read_request"] = base64.b64encode(raw).decode()
    updated = ga._cipher().encrypt(json.dumps(credentials).encode())
    if not is_current():
        raise ga.AccountError("Подключение изменилось во время входа.")
    with ea_connect._db() as conn:
        changed = conn.execute(
            "UPDATE ea_account_links SET secret=? WHERE target=? AND expected_id=? AND secret=?",
            (updated, target, expected_id, secret),
        ).rowcount
    if not changed:
        raise ga.AccountError("Подключение изменилось во время входа.")

    async def read_event(kind):
        return await _read_event(raw, expected_id, kind, lambda: is_current((target, expected_id, updated)))

    return read_event


async def read(owner, guild_id, profile, tier, ally_code, *, territory=False, active_tb=False, event=None, include_supplies=False, member_read=False):
    if member_read and (territory or active_tb or event not in (None, "arena", "conquest")):
        raise ga.AccountError("Доступно только офицерам.")
    connection, credentials, is_current = _load(owner, guild_id, profile, tier, ally_code, optimizer_read=member_read)
    _, expected_id, _ = connection
    try:
        raw = base64.b64decode(credentials["read_request"], validate=True)
    except (KeyError, TypeError, ValueError):
        raise ga.ConnectionRequired("Подключите аккаунт через EA заново.") from None
    event = "tb" if active_tb else event
    if event:
        return await _read_event(raw, expected_id, event, is_current)

    if territory:
        return await game_read_probes.run(raw, expected_id, is_current=is_current, territory_only=True)
    report = []
    supplies = []
    options = {"supplies": supplies} if include_supplies else {}
    summary = await ga.read_initial(raw, expected_id, report=report, **options)
    if not is_current():
        raise ga.AccountError("Подключение изменилось во время чтения.")
    decoded = dict(report)
    inventory = dict(decoded.get("Инвентарь", ()))
    supply_view = dict(supplies)
    energy = _group(supply_view.get("Игрок", ()), "Энергия")
    shipments = [
        {"store": store.get("Магазин"), "refresh": store.get("Следующее обновление"),
         "item": dict(offer.get("Товар", ())), "price": dict(offer.get("Цена", ())),
         "bought": offer.get("Куплено") == "Да"}
        for store in _group(supply_view.get("Магазин", ()), "Поставки")
        for offer in _group(store.items(), "Предложения") if offer.get("Товар")
    ]
    return {
        "read_at": summary["read_at"],
        "energy": energy, "shipments": shipments,
        "inventory_records": summary["inventory_records"],
        "modules": inventory.get("Поиск по модулям", {}).get("mod_inventory"),
        "farm": inventory.get("Калькулятор по запасам", {}).get("gear_plan"),
    }
