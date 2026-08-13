"""Ядро привязки Discord-аккаунта к коду союзника SWGOH (/регистрация в боте и
веб-дашборд зовут одну и ту же функцию) — только Comlink-валидация + запись в БД,
без форматирования ответа (embed в cog, HTML в web/routes/registration.py)."""

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


async def register_player(comlink, guild_id: int, discord_id: str, ally_code: str, is_alt: bool = False) -> RegistrationResult:
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

    # Первая регистрация в этой гильдии всегда основная, даже если попросили
    # альт — иначе получится аккаунт без единого основного.
    has_existing = bool(database.get_user_registrations(discord_id, guild_id=guild_id))
    is_main = (not is_alt) or (not has_existing)

    try:
        database.set_user_registration(discord_id, clean_code, ingame_name, is_main=is_main, guild_id=guild_id)
    except Exception as e:
        return RegistrationResult(ok=False, error=f"Не удалось сохранить регистрацию: {e}")

    accounts = database.get_user_registrations(discord_id, guild_id=guild_id)
    return RegistrationResult(ok=True, ally_code=clean_code, ingame_name=ingame_name, is_main=is_main, accounts=accounts)
