"""Super-admin experiments, restricted to explicitly enrolled test accounts.

The default is notest. No guild roster/account selector, generic RPC proxy,
automatic login, game writes, or credential export is exposed here.
"""
import asyncio
import base64
import json
import sqlite3
import time
import uuid
from contextlib import contextmanager

import database
from services import game_account as ga

NOTEST_OWNER = "276671902733434880"
NOTEST_CODE = "986884832"
NOTEST_PLAYER_ID = "oj8I3wDrTXOtnSugYTfTPg"
MAX_SESSION_BYTES = 2 * 1024 * 1024

# Sources are historical observations, not claims of current competitor access.
# state: proven = tested live; decode = private data exists, parser missing;
# research = schema/idea only; event = needs a developed account/guild;
# write = game mutation, no execution endpoint in this laboratory.
CAPABILITIES = [
    ("inventory", "Запасы", "Приватный инвентарь", "proven", "Получить личные данные и проверить, что игра вернула нужный аккаунт.", "На notest чтение проверено. Сейчас показаны объём и число записей, без расшифровки предметов.", "Наши тесты GetInitialData"),
    ("currencies", "Запасы", "Валюты и остатки ресурсов", "decode", "Кредиты, кристаллы и другие валюты из личного инвентаря.", "В клиенте есть CurrencyItem. Номера полей и расшифровку валют ещё нужно подтвердить.", "Исследование клиента"),
    ("gear", "Запасы", "Снаряжение и материалы", "decode", "Учесть уже имеющиеся детали, осколки и материалы прокачки.", "Списки Material и Equipment есть в Inventory; отчёт по предметам ещё не реализован.", "Клиент; обзоры HotUtils и swgoh.ru"),
    ("farm", "Запасы", "Фарм с учётом запасов", "research", "Вычесть имеющееся снаряжение из цели и рассчитать, чего не хватает.", "Нужны расшифровка инвентаря и расчёт рецептов. Сроки зависят от выбранных трат энергии.", "Обзоры HotUtils и swgoh.ru; закрытые инструменты не проверялись"),
    ("salvage", "Запасы", "Материалы для переработки", "research", "Показать, какие предметы можно переработать и что оставить для целей.", "Сначала нужен разбор запасов; сама переработка в игре сюда не входит.", "Раздел Salvage в обзоре HotUtils"),
    ("mod-bank", "Модули", "Банк свободных модулей", "decode", "Показать модули, которые не надеты на персонажей и не видны в публичном ростере.", "Inventory содержит UnequippedMod. Нужны проверенные номера полей и расшифровка характеристик.", "Клиент; полный инвентарь HotUtils ранее изучали"),
    ("mod-search", "Модули", "Поиск по всем своим модулям", "research", "Искать комплект, основную характеристику и скорость с учётом банка.", "Наш текущий поиск видит надетые модули. Для полного поиска нужен разбор свободных.", "Обзор управления модулями HotUtils"),
    ("mod-plan", "Модули", "План перестановки и стоимость", "research", "Подобрать комплекты из всего инвентаря и оценить снятие модулей.", "Расчёт плана не меняет игру. Автоматическое применение — отдельная возможность ниже.", "HotUtils; анализатор swgoh.ru"),
    ("mod-upgrades", "Модули", "Кандидаты на прокачку и перекалибровку", "research", "Сравнить свободные модули и запасы материалов для улучшений.", "Нужны разбор модулей, цены и правила оценки. Здесь пока нет готового рейтинга.", "Обзоры HotUtils и swgoh.ru"),
    ("loadouts", "Модули", "Игровые наборы и фильтры модулей", "research", "Читать сохранённые в игре наборы модулей и фильтры.", "Найдены схемы GetModLoadouts и GetModFilters; запросы ещё не проверяли.", "Исследование клиента"),
    ("energy", "Личные события", "Энергия, таймеры и дневные ограничения", "research", "Показывать личные значения энергии и оставшиеся попытки.", "В клиенте есть Energy, CooldownStatus и DailyActionCapStatus. Отчёта пока нет.", "Исследование клиента"),
    ("shipments", "Личные события", "Текущие поставки и магазин", "research", "Смотреть актуальное предложение и цену для своего аккаунта.", "Данные магазина есть в InitialDataResponse. Необходимо отделить чтение от обновления за валюту.", "Клиент; EA-инструменты swgoh.ru, внутри не проверялись"),
    ("inbox", "Личные события", "Почта и доступные награды", "research", "Показывать письма и награды, которые ещё можно забрать.", "В InitialDataResponse есть Inbox и DailyLogin. Сбор наград — отдельное игровое действие.", "Исследование клиента"),
    ("personal-events", "Личные события", "Личные события, задания и прогресс", "research", "Показывать доступные события и личное прохождение испытаний.", "Есть GameEvent, ChallengeProgress, CampaignStatus и EpisodeStatus. Требуется проверка формата.", "Клиент; раздел Player Events в обзоре HotUtils"),
    ("conquest", "Личные события", "Живое Завоевание", "event", "Текущее прохождение, задачи, диски и выносливость персонажей.", "Есть схема ConquestStatus. На notest уровня 2 режим закрыт; нужен развитый тестовый аккаунт.", "Клиент; раздел Conquest в обзоре HotUtils"),
    ("gac", "Гильдия и бои", "Текущий раунд Великой арены", "event", "Читать свою расстановку, состояние раунда и доступные сведения о боях.", "Есть TerritoryTournamentStatus. Состав данных и историю боёв ещё нужно проверить на активном событии.", "Клиент; Grand Arena в обзоре HotUtils"),
    ("live-tb", "Гильдия и бои", "Живая ТБ: заливка, взводы и миссии", "event", "Читать состояние текущего этапа и участие игроков во время события.", "Найдены TerritoryBattleStatus и GetTerritoryMapStatuses. На notest нет гильдии; офицерские поля требуют отдельной проверки.", "Клиент; Guild Events / TB в обзоре HotUtils"),
    ("live-tw", "Гильдия и бои", "Живая ВГ: карта, защита и атаки", "event", "Проверять текущие территории и доступные сведения об атаках.", "Есть TerritoryWarStatus. Полнота данных зависит от события и прав аккаунта; пока не проверено.", "Клиент; командный центр HotUtils"),
    ("live-raid", "Гильдия и бои", "Активный рейд", "event", "Получить состояние и доступные результаты текущего рейда до его завершения.", "Найдена схема GetGuildRaidStatuses. Нужны гильдия и активный рейд.", "Клиент; Guild Events в обзоре HotUtils"),
    ("notifications", "Гильдия и бои", "Оповещения по живым событиям", "research", "Напоминать о незавершённых личных и гильдейских действиях по приватным данным.", "Это надстройка над проверенными отчётами. Частота, получатели и отправка пока не настроены.", "Обзор Guild Events HotUtils"),
    ("move-mods", "Действия в игре", "Надеть и снять модули", "write", "Применить рассчитанную перестановку к аккаунту.", "Есть схема UpdateMods. На наших аккаунтах не тестировали; исполнитель на этой странице отсутствует.", "Клиент; управление модулями HotUtils"),
    ("improve-mods", "Действия в игре", "Прокачать или перекалибровать модуль", "write", "Повысить уровень, качество или изменить дополнительную характеристику.", "Есть схемы LevelMod, TierMod, PromoteMod, RerollMod и AcceptRerolledMod. Расход ресурсов не реализован.", "Исследование клиента"),
    ("manage-mods", "Действия в игре", "Блокировка и продажа модулей", "write", "Защитить модуль от продажи либо продать выбранные модули.", "Есть схемы LockMod, UnlockMod и SellMod. Продажа необратима; кнопок выполнения нет.", "Исследование клиента"),
    ("save-loadout", "Действия в игре", "Записать игровой набор модулей", "write", "Сохранить, переименовать или удалить набор и его вкладки в игре.", "Есть схемы SaveModLoadout и управления вкладками. Пока только исследование.", "Исследование клиента"),
    ("claim", "Действия в игре", "Сбор энергии и наград", "write", "Забирать доступную энергию и награды.", "Найдены ClaimEnergyReward и ClaimAllEnergyRewards. Сбор ничего автоматически не выполняет.", "Исследование клиента"),
    ("shopping", "Действия в игре", "Покупки и открытие наборов", "write", "Покупать выбранные поставки или открывать бронзиумные наборы.", "Есть схемы StoreTransaction и ShipmentTransaction. Массовое открытие видели в HotUtils; в нашем коде его нет.", "Клиент; Bronziums в обзоре HotUtils"),
    ("progress-unit", "Действия в игре", "Прокачка персонажей и снаряжения", "write", "Применять выбранные улучшения с расходом собственных материалов.", "Раздел прокачки видели среди EA-инструментов swgoh.ru. Конкретные операции и их протокол ещё не проверены.", "Обзор swgoh.ru: раздел требовал EA-входа"),
    ("event-actions", "Действия в игре", "Защита и действия в гильдейских событиях", "write", "Сохранить игровую защиту или выполнить доступное действие события.", "Есть схемы SaveTerritoryDefense, CommandTerritory, JoinTerritoryEvent. Отправка взводов и боевые действия не проверены.", "Клиент; защита Grand Arena в обзоре HotUtils"),
]

STATES = {
    "proven": ("Чтение проверено", "badge-ok"),
    "decode": ("Нужен разбор данных", "badge-neutral"),
    "research": ("Нужно проверить", "badge-neutral"),
    "event": ("Нужен доступ к режиму", "badge-warn"),
    "write": ("Изменяет игру · не реализовано", "badge-warn"),
}


@contextmanager
def _db():
    conn = sqlite3.connect(database.DB_NAME, timeout=15)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("""CREATE TABLE IF NOT EXISTS game_lab_accounts (
            id TEXT PRIMARY KEY, ally_code TEXT NOT NULL UNIQUE, player_id TEXT NOT NULL UNIQUE,
            name TEXT NOT NULL, level INTEGER NOT NULL, game_guild_id TEXT NOT NULL,
            game_guild_name TEXT NOT NULL, added_by TEXT NOT NULL, added_at REAL NOT NULL,
            secret BLOB, provider TEXT, summary TEXT, state TEXT NOT NULL DEFAULT 'unconnected',
            last_attempt REAL NOT NULL DEFAULT 0)""")
        conn.execute("INSERT OR IGNORE INTO game_lab_accounts "
                     "(id,ally_code,player_id,name,level,game_guild_id,game_guild_name,added_by,added_at) "
                     "VALUES ('notest',?,?, 'notest',2,'','',?,?)",
                     (NOTEST_CODE, NOTEST_PLAYER_ID, NOTEST_OWNER, time.time()))
        conn.commit()
        with conn:
            yield conn
    finally:
        conn.close()


def _row(target_id):
    with _db() as conn:
        row = conn.execute("SELECT * FROM game_lab_accounts WHERE id=?", (target_id,)).fetchone()
    if not row:
        raise ga.AccountError("Аккаунт не добавлен к испытаниям.")
    if target_id == "notest" and (row["ally_code"] != NOTEST_CODE or row["player_id"] != NOTEST_PLAYER_ID):
        raise ga.AccountError("Привязка notest изменилась. Чтение остановлено.")
    return row


def _public(row):
    # Explicit allowlist: never return the stored protobuf, token, or player ID.
    return {key: row[key] for key in ("id", "ally_code", "name", "level", "game_guild_name", "state", "provider")}


def accounts():
    with _db() as conn:
        rows = conn.execute("SELECT * FROM game_lab_accounts ORDER BY id!='notest', added_at").fetchall()
    return [_public(row) for row in rows]


def current(target_id="notest"):
    row = _row(target_id)
    result = _public(row)
    result["summary"] = json.loads(row["summary"]) if row["summary"] else None
    from services import ea_connect
    result["ea_linked"] = ea_connect.linked(target_id)
    if target_id == "notest":
        if ga._identity(NOTEST_OWNER, 1, NOTEST_CODE) != NOTEST_PLAYER_ID:
            raise ga.AccountError("Сессия notest не совпадает с закреплённым тестовым игроком.")
        session = ga.status(NOTEST_OWNER, 1, "test", ally_code=NOTEST_CODE)
        result["state"] = "error" if result["ea_linked"] and row["state"] == "error" else session["state"] if session else "unconnected"
        result["summary"] = session["summary"] if session else None
        result["provider"] = session["provider"] if session else None
    return result


async def add_account(owner, ally_code, consent, comlink):
    code = "".join(c for c in ally_code if c not in " -")
    if not consent:
        raise ga.AccountError("Подтвердите владение аккаунтом и согласие на тестовое чтение.")
    if len(code) != 9 or any(c not in "123456789" for c in code):
        raise ga.AccountError("Код союзника — 9 цифр без нулей.")
    try:
        player = await asyncio.to_thread(comlink.get_player, allycode=code)
    except Exception:
        raise ga.AccountError("Не удалось проверить игрока через Comlink. Повторите позже.") from None
    if (not player or str(player.get("allyCode")) != code or not player.get("playerId")
            or not player.get("name")):
        raise ga.AccountError("Игрок не найден или его данные не совпадают с кодом.")
    with _db() as conn:
        existing = conn.execute("SELECT id,player_id FROM game_lab_accounts WHERE ally_code=?", (code,)).fetchone()
        if existing:
            if existing[1] != player["playerId"]:
                raise ga.AccountError("Игрок не совпадает с уже добавленным тестовым аккаунтом.")
            return existing[0]
        target_id = uuid.uuid4().hex
        conn.execute("INSERT INTO game_lab_accounts "
                     "(id,ally_code,player_id,name,level,game_guild_id,game_guild_name,added_by,added_at) "
                     "VALUES (?,?,?,?,?,?,?,?,?)", (target_id, code, player["playerId"], player["name"],
                     int(player.get("level") or 0), str(player.get("guildId") or ""),
                     str(player.get("guildName") or ""), str(owner), time.time()))
    return target_id


def _session_bytes(raw):
    if not raw or len(raw) > MAX_SESSION_BYTES:
        raise ga.AccountError("Нужен файл игровой сессии размером до 2 МБ.")
    if raw.lstrip().startswith(b"{"):
        try:
            data = json.loads(raw)
            value = data["request"]
            encoded = value["base64"] if isinstance(value, dict) else value
            return base64.b64decode(encoded, validate=True)
        except Exception:
            raise ga.AccountError("Не удалось прочитать файл сессии.") from None
    return raw


async def connect(target_id, provider, raw, consent):
    row = _row(target_id)
    if not consent or provider not in ("ea", "google"):
        raise ga.AccountError("Выберите EA/Google и подтвердите тестовое чтение.")
    request = _session_bytes(raw)
    ga.read_request(request, row["player_id"])  # strict one-RPC allowlist before any network
    cipher = ga._cipher()  # fail before the request if the vault key is unavailable
    await _reserve(target_id)
    if target_id == "notest":
        if ga._identity(NOTEST_OWNER, 1, NOTEST_CODE) != NOTEST_PLAYER_ID:
            raise ga.AccountError("Привязка notest не соответствует тестовому игроку.")
        summary = await ga.provision(NOTEST_OWNER, 1, "test", NOTEST_CODE, provider, request, consent=True)
        from services import ea_connect
        ea_connect.disconnect(target_id)
        return summary
    summary = await ga.read_initial(request, row["player_id"])
    secret = cipher.encrypt(request)
    with _db() as conn:
        conn.execute("UPDATE game_lab_accounts SET secret=?,provider=?,summary=?,state='connected' WHERE id=?",
                     (secret, provider, json.dumps(summary), target_id))
    from services import ea_connect
    ea_connect.disconnect(target_id)
    return summary


async def _reserve(target_id):
    now = time.time()
    with _db() as conn:
        changed = conn.execute("UPDATE game_lab_accounts SET last_attempt=? WHERE id=? AND last_attempt<=?",
                               (now, target_id, now - ga.COOLDOWN))
        if not changed.rowcount:
            raise ga.AccountError("Повторное чтение доступно через минуту.")


async def refresh(target_id="notest"):
    row = _row(target_id)
    from services import ea_connect
    if ea_connect.linked(target_id):
        return await ea_connect.read(target_id)
    if target_id == "notest":
        current(target_id)  # verifies the pinned identity before touching the session
        return await ga.refresh(NOTEST_OWNER, 1, "test", ally_code=NOTEST_CODE)
    if row["secret"] is None:
        raise ga.AccountError("Аккаунт добавлен, но игровая сессия ещё не подключена.")
    await _reserve(target_id)
    try:
        request = ga._cipher().decrypt(row["secret"])
        summary = await ga.read_initial(request, row["player_id"])
    except Exception as error:
        with _db() as conn:
            conn.execute("UPDATE game_lab_accounts SET state='error' WHERE id=? AND secret=?", (target_id, row["secret"]))
        if isinstance(error, ga.AccountError):
            raise
        raise ga.AccountError("Не удалось открыть игровую сессию. Подключите её заново.") from None
    with _db() as conn:
        conn.execute("UPDATE game_lab_accounts SET summary=?,state='connected' WHERE id=? AND secret=?",
                     (json.dumps(summary), target_id, row["secret"]))
    return summary


def remove(target_id):
    if target_id == "notest":
        raise ga.AccountError("Основной тестовый аккаунт notest закреплён на странице.")
    with _db() as conn:
        conn.execute("DELETE FROM game_lab_accounts WHERE id=?", (target_id,))
    from services import ea_connect
    ea_connect.disconnect(target_id)


def catalog():
    return [{"id": key, "group": group, "title": title, "state": state, "description": description,
             "limit": limit, "source": source, "badge": STATES[state][0], "badge_class": STATES[state][1]}
            for key, group, title, state, description, limit, source in CAPABILITIES]
