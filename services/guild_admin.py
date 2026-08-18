"""Ядро управления гильдиями/админами/ручными грантами — общее для Discord-cog
(cogs/admin_management.py) и веб-роута (web/routes/admin.py), по образцу
services/registration.py. Только Comlink-валидация + запись в БД, без
форматирования ответа."""

import asyncio
from dataclasses import dataclass

import database


@dataclass
class GuildAddResult:
    ok: bool
    error: str = None
    guild_id: int = None
    name: str = None
    swgoh_guild_id: str = None
    member_count: int = None


@dataclass
class GrantResult:
    ok: bool
    error: str = None
    ingame_name: str = None


async def add_guild(comlink, ally_code: str, discord_guild_id: str) -> GuildAddResult:
    """Заводит новую участвующую SWGOH-гильдию по аллай-коду ЛЮБОГО её участника —
    имя и игровой ID гильдии подтягиваются из Comlink, вручную вводить не нужно."""
    clean_code = "".join(filter(str.isdigit, ally_code))
    if len(clean_code) != 9:
        return GuildAddResult(ok=False, error="Код союзника должен состоять ровно из 9 цифр!")

    try:
        player_data = await asyncio.to_thread(comlink.get_player, clean_code)
    except Exception as e:
        return GuildAddResult(ok=False, error=f"Не удалось проверить код из-за сбоя связи с сервером: {e}")
    if not player_data or "name" not in player_data:
        return GuildAddResult(ok=False, error=f"Игрок с кодом союзника {clean_code} не найден на серверах EA/CG.")

    swgoh_guild_id = player_data.get("guildId")
    if not swgoh_guild_id:
        return GuildAddResult(ok=False, error="Этот игрок не состоит ни в одной гильдии SWGOH.")

    if database.get_guild_config_by_swgoh_id(str(swgoh_guild_id)):
        return GuildAddResult(ok=False, error="Эта гильдия уже добавлена в список обслуживаемых.")

    try:
        guild = await asyncio.to_thread(comlink.get_guild, swgoh_guild_id)
    except Exception as e:
        return GuildAddResult(ok=False, error=f"Не удалось получить данные гильдии из Comlink: {e}")
    guild = guild.get("guild", guild)
    profile = guild.get("profile", {})
    name = profile.get("name") or f"Гильдия {swgoh_guild_id}"
    member_count = profile.get("memberCount")

    new_id = database.create_guild(
        name=name,
        ally_code=clean_code,
        swgoh_guild_id=str(swgoh_guild_id),
        discord_guild_id=str(discord_guild_id),
        member_role_id="",
        officer_role_id="",
    )
    return GuildAddResult(
        ok=True, guild_id=new_id, name=name,
        swgoh_guild_id=str(swgoh_guild_id), member_count=member_count,
    )


def deactivate_guild(guild_id: int) -> bool:
    return database.update_guild_config(guild_id, is_active=0)


def list_guilds(active_only: bool = True) -> list:
    return database.get_all_guild_configs(active_only=active_only)


def add_super_admin(discord_id: str, username: str, added_by: str) -> bool:
    return database.add_bot_admin(str(discord_id), username=username, added_by=added_by)


def remove_super_admin(discord_id: str) -> bool:
    return database.remove_bot_admin(str(discord_id))


async def add_grant(comlink, discord_id: str, ally_code: str, guild_id: int, tier: str, granted_by: str) -> GrantResult:
    """Ручной грант для игрока вне участвующих гильдий — проверяет ally_code
    через Comlink (тот же паттерн, что и /регистрация), чтобы не выдавать доступ
    по опечатке. Гильдию, к данным которой привязывается грант (для команд вида
    /статы, где нужен guild_id), супер-админ указывает явно — это НЕ обязательно
    гильдия, где игрок реально состоит (он же вне участвующих гильдий)."""
    clean_code = "".join(filter(str.isdigit, ally_code))
    if len(clean_code) != 9:
        return GrantResult(ok=False, error="Код союзника должен состоять ровно из 9 цифр!")
    if tier not in ("member", "officer"):
        return GrantResult(ok=False, error="Уровень должен быть 'member' или 'officer'.")
    if database.get_guild_config(guild_id) is None:
        return GrantResult(ok=False, error=f"Гильдия с id={guild_id} не найдена.")

    try:
        player_data = await asyncio.to_thread(comlink.get_player, clean_code)
    except Exception as e:
        return GrantResult(ok=False, error=f"Не удалось проверить код из-за сбоя связи с сервером: {e}")
    if not player_data or "name" not in player_data:
        return GrantResult(ok=False, error=f"Игрок с кодом союзника {clean_code} не найден на серверах EA/CG.")

    database.add_manual_grant(str(discord_id), clean_code, guild_id, tier, granted_by=granted_by, ingame_name=player_data["name"])
    return GrantResult(ok=True, ingame_name=player_data["name"])


def remove_grant(discord_id: str) -> bool:
    return database.remove_manual_grant(str(discord_id))


def list_admins() -> dict:
    """{"super_admins": [...], "grants": [...]} — сводный список для /админы список."""
    return {
        "super_admins": database.get_all_bot_admins(),
        "grants": database.get_all_manual_grants(),
    }
