"""Ядро привязки Discord-аккаунта к коду союзника SWGOH (/регистрация в боте и
веб-дашборд зовут одну и ту же функцию) — только Comlink-валидация + запись в БД,
без форматирования ответа (embed в cog, HTML в web/routes/registration.py).

Это единственная "дверь" в систему прав: гильдия определяется НЕ по Discord-роли
вызывающего, а живым запросом к Comlink по самому ally_code (guildId игрока →
сверка с guilds.swgoh_guild_id) — так что команда доступна вообще всем, включая
пользователей без единого известного боту тега (main.py::ALWAYS_ALLOWED_COMMANDS)."""

import asyncio
from dataclasses import dataclass, field

import database


@dataclass
class RegistrationResult:
    ok: bool
    error: str = None
    ally_code: str = None
    ingame_name: str = None
    is_main: bool = None
    accounts: list = field(default_factory=list)  # [(ally_code, ingame_name, is_main), ...]


def _discord_conflict_error(existing_owner: str) -> str:
    if existing_owner.startswith("tg:"):  # привязал игрок из Telegram-бота (tg_bot/store.py)
        return (
            "Этот код союзника уже привязан к игроку в Telegram. Если это вы — свяжите Telegram "
            "с Discord на сайте («Мой профиль» → «Привязать Telegram»), иначе обратитесь к офицеру."
        )
    return (
        f"Этот код союзника уже привязан к другому участнику Discord (<@{existing_owner}>). "
        "Если это ошибка, попросите офицера перерегистрировать его через "
        "`/регистрация участник:@нужный_пользователь`."
    )


# Игровые ранги, чьи коды офицер не привязывает другим (лидер 4, офицер 3): права в боте
# идут от ранга привязанного кода, так что такая привязка = выдать офицерский доступ.
_OFFICER_LEVELS = (3, 4)


async def _notify(owner_id: str, text: str) -> None:
    from services import discord_rest
    try:
        await discord_rest.send_dm(owner_id, text)
    except Exception as e:  # закрытая личка и т.п. — привязка от этого не отменяется
        print(f"⚠️ [Регистрация] Не удалось написать {owner_id}: {e}")


async def register_player(comlink, discord_id: str, ally_code: str, is_alt: bool = False, allow_reassign: bool = False,
                          conflict_error=_discord_conflict_error, officer_guild_id: int | None = None,
                          registered_by: str | None = None) -> RegistrationResult:
    """conflict_error(owner_id) -> текст отказа, когда код уже у другого владельца —
    у Telegram-бота свой текст (tg_bot/handlers.py), там <@id> не работает.
    officer_guild_id — офицер регистрирует другого человека: код должен быть из гильдии
    офицера (раньше проверялось только «офицер хоть какой-то гильдии») и не должен быть
    кодом офицера/лидера в игре (это может сам игрок или супер-админ).
    registered_by — имя того, кто привязывает ДРУГОГО человека (не себя и не своего альта):
    тому уходит сообщение в личку, а прежнему владельцу перенесённого кода — что код забрали."""
    discord_id = str(discord_id)
    clean_code = "".join(filter(str.isdigit, ally_code))
    if len(clean_code) != 9:
        return RegistrationResult(ok=False, error="Код союзника должен состоять ровно из 9 цифр!")

    try:
        player_data = await asyncio.to_thread(comlink.get_player, clean_code)
        if not player_data or "name" not in player_data:
            return RegistrationResult(ok=False, error=f"Игрок с кодом союзника {clean_code} не найден на серверах EA/CG. Проверьте цифры.")
        ingame_name = player_data["name"]
    except Exception as e:
        return RegistrationResult(ok=False, error=f"Не удалось проверить код из-за сбоя связи с сервером: {e}")

    swgoh_guild_id = player_data.get("guildId")
    if not swgoh_guild_id:
        return RegistrationResult(ok=False, error="Этот аккаунт не состоит ни в одной гильдии SWGOH.")
    guild_cfg = database.get_guild_config_by_swgoh_id(str(swgoh_guild_id))
    if not guild_cfg:
        return RegistrationResult(ok=False, error="Эта SWGOH-гильдия не входит в число обслуживаемых ботом. Обратитесь к супер-админу за ручным доступом.")
    guild_id = guild_cfg["id"]
    if officer_guild_id is not None and guild_id != officer_guild_id:
        return RegistrationResult(ok=False, error="Этот код союзника из другой гильдии — офицер регистрирует только игроков своей гильдии.")
    if officer_guild_id is not None and database.get_member_level(guild_id, clean_code) in _OFFICER_LEVELS:
        return RegistrationResult(ok=False, error=(
            "Это код офицера или лидера гильдии в игре — привязка дала бы офицерский доступ к боту. "
            "Такой код привязывает только сам игрок (/регистрация без участника) или админ бота."
        ))

    # Один и тот же ally_code нельзя привязать сразу к нескольким Discord-аккаунтам
    # в пределах гильдии — иначе статы/задания одного игрока будет видеть и
    # редактировать чужой Discord-пользователь. allow_reassign=True (officer-путь:
    # /регистрация участник:@X в боте, либо весь веб-дашборд — он и так officer-only)
    # снимает старую привязку и переносит код на нового владельца.
    existing_owner = database.find_ally_code_owner(clean_code, guild_id=guild_id)
    moved_from = None
    if existing_owner and existing_owner != discord_id:
        if not allow_reassign:
            return RegistrationResult(ok=False, error=conflict_error(existing_owner))
        database.delete_user_registration(existing_owner, clean_code, guild_id=guild_id)
        moved_from = existing_owner

    # Первая регистрация в этой гильдии всегда основная, даже если попросили
    # альт — иначе получится аккаунт без единого основного.
    has_existing = bool(database.get_user_registrations(discord_id, guild_id=guild_id))
    is_main = (not is_alt) or (not has_existing)

    try:
        database.set_user_registration(discord_id, clean_code, ingame_name, is_main=is_main, guild_id=guild_id)
    except Exception as e:
        return RegistrationResult(ok=False, error=f"Не удалось сохранить регистрацию: {e}")

    if registered_by:
        guild_name = guild_cfg.get("name") or "гильдия"
        await _notify(discord_id, (
            f"🔗 {registered_by} привязал(а) к вам игровой аккаунт {ingame_name} (код союзника {clean_code}, {guild_name})"
            f"{'' if is_main else ' как альт'}. Теперь бот считает этот аккаунт вашим.\n"
            f"Если это не ваш аккаунт — отвяжите его командой /регистрация_отмена {clean_code} или напишите офицерам."
        ))
        if moved_from:
            await _notify(moved_from, (
                f"🔓 Код союзника {clean_code} ({ingame_name}, {guild_name}) отвязан от вас и привязан к другому участнику — "
                f"это сделал(а) {registered_by}. Если это ошибка — напишите офицерам."
            ))

    accounts = database.get_user_registrations(discord_id, guild_id=guild_id)
    return RegistrationResult(ok=True, ally_code=clean_code, ingame_name=ingame_name, is_main=is_main, accounts=accounts)


@dataclass
class UnregisterResult:
    ok: bool
    error: str = None
    ally_code: str = None
    ingame_name: str = None
    remaining_accounts: list = field(default_factory=list)  # [(ally_code, ingame_name, is_main), ...]


async def unregister_player(discord_id: str, guild_id: int, ally_code: str | None = None) -> UnregisterResult:
    """Отвязывает один аккаунт (основной или альт) от discord_id в этой гильдии.
    Не трогает Comlink — это чисто отмена локальной привязки, в отличие от
    register_player, который живым запросом проверяет игру.

    Если у discord_id несколько привязанных аккаунтов, ally_code обязателен —
    иначе непонятно, какой из них отвязывать. Если отвязывается основной, а
    альт остаётся — альт становится новым основным (не оставляем discord_id
    без единого is_main=1, см. set_user_registration)."""
    discord_id = str(discord_id)
    accounts = database.get_user_registrations(discord_id, guild_id=guild_id)
    if not accounts:
        return UnregisterResult(ok=False, error="У вас нет ни одной привязки в этой гильдии.")

    if ally_code:
        clean_code = "".join(filter(str.isdigit, ally_code))
        match = next((a for a in accounts if a[0] == clean_code), None)
        if not match:
            return UnregisterResult(ok=False, error=f"Код союзника {clean_code} не привязан к вам в этой гильдии.")
    elif len(accounts) > 1:
        listed = ", ".join(f"`{code}` ({name})" for code, name, _ in accounts)
        return UnregisterResult(
            ok=False,
            error=f"У вас привязано несколько аккаунтов: {listed}. Укажите параметр ally_code — какой из них отвязать.",
        )
    else:
        match = accounts[0]

    target_ally_code, ingame_name, was_main = match
    database.delete_user_registration(discord_id, target_ally_code, guild_id=guild_id)

    remaining = database.get_user_registrations(discord_id, guild_id=guild_id)
    if was_main and remaining:
        # Отвязали основной, но остался альт — назначаем его новым основным,
        # иначе у discord_id не останется ни одной is_main=1 записи.
        promoted_code, promoted_name, _ = remaining[0]
        database.set_user_registration(discord_id, promoted_code, promoted_name, is_main=True, guild_id=guild_id)
        remaining = database.get_user_registrations(discord_id, guild_id=guild_id)

    return UnregisterResult(ok=True, ally_code=target_ally_code, ingame_name=ingame_name, remaining_accounts=remaining)
