"""Own-account checks for the simple Discord → EA email/code linking form."""
import asyncio
import time
import uuid

import database
from services import game_account as ga, game_lab as lab, test_accounts


async def validate_player(owner, is_alt, preview_id, comlink):
    ga.require_pilot(owner, 1, "test")
    try:
        player = await asyncio.to_thread(comlink.get_player, player_id=preview_id)
    except Exception:
        raise ga.AccountError("Не удалось проверить игрока. Начните подключение позже.") from None
    code = str((player or {}).get("allyCode", ""))
    if (not player or player.get("playerId") != preview_id or not player.get("name")
            or len(code) != 9 or any(c not in "123456789" for c in code)):
        raise ga.AccountError("EA и игра вернули несовпадающие данные игрока.")
    main = database.get_user_registration(str(owner), guild_id=1)
    if not main:
        raise ga.AccountError("Сначала зарегистрируйте основной аккаунт в боте.")
    if is_alt and code == str(main[0]):
        raise ga.AccountError("Это ваш основной аккаунт. Откройте форму без «альт: Да».")
    if not is_alt and code != str(main[0]):
        raise ga.AccountError("Эта почта относится к другому игроку. Для второго аккаунта откройте форму с «альт: Да».")
    if any(str(other) != str(owner) for _gid, other in database.get_ally_code_registrations(code)):
        raise ga.AccountError("Этот игрок уже зарегистрирован у другого пользователя. Привязка не изменена.")
    with test_accounts._db() as conn:
        if conn.execute("SELECT 1 FROM test_account_links WHERE ally_code=? AND owner!=?", (code, str(owner))).fetchone():
            raise ga.AccountError("Этот тестовый игрок уже привязан к другому пользователю.")
    with lab._db() as conn:
        existing = conn.execute("SELECT player_id,added_by FROM game_lab_accounts WHERE ally_code=?", (code,)).fetchone()
    if existing and (existing["player_id"] != preview_id or existing["added_by"] != str(owner)):
        raise ga.AccountError("Аккаунт уже используется в другом тестовом подключении.")
    return player


def enroll(owner, is_alt, player, *, pending_id=None, pending_target=None):
    ga.require_pilot(owner, 1, "test")
    code = str(player["allyCode"])
    main = database.get_user_registration(str(owner), guild_id=1)
    if not main or (is_alt and str(main[0]) == code) or (not is_alt and str(main[0]) != code):
        raise ga.AccountError("Основной аккаунт изменился. Начните привязку заново.")
    with test_accounts._db():
        pass
    with lab._db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        if pending_id is not None and not conn.execute(
                "SELECT 1 FROM ea_browser_pending WHERE id=? AND owner=? AND target=? AND status='checking' AND expires>?",
                (pending_id, str(owner), pending_target, time.time())).fetchone():
            raise ga.AccountError("Подключение отменено или истекло. Начните его заново.")
        current_main = conn.execute("SELECT ally_code FROM user_registration WHERE guild_id=1 AND discord_id=? ORDER BY is_main DESC LIMIT 1", (str(owner),)).fetchone()
        if not current_main or (is_alt and current_main[0] == code) or (not is_alt and current_main[0] != code):
            raise ga.AccountError("Основной аккаунт изменился. Начните привязку заново.")
        if (conn.execute("SELECT 1 FROM user_registration WHERE ally_code=? AND discord_id!=?", (code, str(owner))).fetchone()
                or conn.execute("SELECT 1 FROM test_account_links WHERE ally_code=? AND owner!=?", (code, str(owner))).fetchone()):
            raise ga.AccountError("Игрок уже привязан к другому пользователю.")
        existing = conn.execute("SELECT id,player_id,added_by FROM game_lab_accounts WHERE ally_code=?", (code,)).fetchone()
        if existing and (existing["player_id"] != player["playerId"] or existing["added_by"] != str(owner)):
            raise ga.AccountError("Этот аккаунт уже используется в другом подключении.")
        target = existing["id"] if existing else uuid.uuid4().hex
        if not existing:
            conn.execute("INSERT INTO game_lab_accounts "
                         "(id,ally_code,player_id,name,level,game_guild_id,game_guild_name,added_by,added_at,lab_visible) VALUES (?,?,?,?,?,?,?,?,?,0)",
                         (target, code, player["playerId"], player["name"], int(player.get("level") or 0),
                          str(player.get("guildId") or ""), str(player.get("guildName") or ""), str(owner), time.time()))
        if is_alt:
            conn.execute("INSERT OR REPLACE INTO test_account_links VALUES (?,?,?,?,?)", (1, str(owner), code, player["playerId"], player["name"]))
        if pending_id is not None:
            conn.execute("UPDATE ea_browser_pending SET target=? WHERE id=?", (target, pending_id))
    return target
