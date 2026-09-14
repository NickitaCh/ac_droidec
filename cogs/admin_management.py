"""Управление списком обслуживаемых гильдий и правами доступа — только для
супер-админов (guild_resolver.is_super_admin). Вся бизнес-логика — в
services/guild_admin.py (общая с веб-дашбордом), здесь только Discord-обвязка."""

import disnake
from disnake.ext import commands, tasks

import database
import guild_resolver
from services.guild_admin import (
    add_grant,
    add_guild,
    add_super_admin,
    deactivate_guild,
    list_admins,
    list_guilds,
    remove_grant,
    remove_super_admin,
)

TIER_CHOICES = [
    disnake.OptionChoice(name="Игрок", value="member"),
    disnake.OptionChoice(name="Офицер", value="officer"),
]


def _super_admin_only(inter: disnake.ApplicationCommandInteraction) -> bool:
    return guild_resolver.is_super_admin(inter.author)


class AdminManagementCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.subscription_expiry_loop.start()

    def cog_unload(self):
        self.subscription_expiry_loop.cancel()

    # Раз в сутки гасит is_active у гильдий с истёкшей платной подпиской
    # (database.get_expired_active_guilds — только subscription_expires_at
    # IS NOT NULL, бессрочные ручные гильдии сюда не попадают). Доступ у их
    # участников отваливается сразу же на следующем вызове команды —
    # guild_resolver.resolve_access читает БД каждый раз, без кэша.
    @tasks.loop(hours=24)
    async def subscription_expiry_loop(self):
        for g in database.get_expired_active_guilds():
            database.update_guild_config(g["id"], is_active=0)
            print(f"⏳ Подписка гильдии «{g['name']}» (id={g['id']}) истекла — доступ отключён.")

    @subscription_expiry_loop.before_loop
    async def before_subscription_expiry_loop(self):
        await self.bot.wait_until_ready()

    # ------------------ /гильдия ------------------
    @commands.slash_command(name="гильдия", description="Управление списком обслуживаемых гильдий (только супер-админ)")
    @commands.check(_super_admin_only)
    async def guild_group(self, inter: disnake.ApplicationCommandInteraction):
        pass

    @guild_group.sub_command(name="добавить", description="Добавить гильдию в список обслуживаемых по аллай-коду участника")
    async def guild_add(
        self,
        inter: disnake.ApplicationCommandInteraction,
        аллай_код: str = commands.Param(description="9-значный код союзника любого участника новой гильдии"),
        discord_сервер: str = commands.Param(default=None, description="ID Discord-сервера гильдии (по умолчанию — текущий)"),
    ):
        await inter.response.defer(ephemeral=True)
        discord_guild_id = discord_сервер or str(inter.guild_id)
        result = await add_guild(self.bot.comlink, аллай_код, discord_guild_id)
        if not result.ok:
            await inter.edit_original_response(content=f"❌ {result.error}")
            return
        embed = disnake.Embed(title="✅ Гильдия добавлена", color=disnake.Color.green())
        embed.add_field(name="Название", value=result.name, inline=True)
        embed.add_field(name="Внутренний ID", value=str(result.guild_id), inline=True)
        embed.add_field(name="Comlink guild ID", value=f"`{result.swgoh_guild_id}`", inline=False)
        if result.member_count is not None:
            embed.add_field(name="Участников", value=str(result.member_count), inline=True)
        embed.add_field(name="Discord-сервер", value=discord_guild_id, inline=True)
        await inter.edit_original_response(embed=embed)

    @guild_group.sub_command(name="список", description="Список обслуживаемых гильдий")
    async def guild_list(self, inter: disnake.ApplicationCommandInteraction):
        await inter.response.defer(ephemeral=True)
        guilds = list_guilds(active_only=False)
        if not guilds:
            await inter.edit_original_response(content="Список пуст.")
            return
        lines = []
        for g in guilds:
            status = "✅" if g["is_active"] else "🚫"
            expires = g.get("subscription_expires_at")
            subscription = f"до `{expires[:10]}`" if expires else "бессрочно"
            lines.append(
                f"{status} `{g['id']}` — **{g['name']}** ({subscription}) "
                f"(Comlink: `{g['swgoh_guild_id'] or '—'}`, Discord-сервер: `{g['discord_guild_id']}`)"
            )
        embed = disnake.Embed(title="📋 Обслуживаемые гильдии", description="\n".join(lines), color=disnake.Color.blurple())
        await inter.edit_original_response(embed=embed)

    @guild_group.sub_command(name="деактивировать", description="Убрать гильдию из активных (мягкое удаление)")
    async def guild_deactivate(
        self,
        inter: disnake.ApplicationCommandInteraction,
        гильдия_id: int = commands.Param(description="Внутренний ID гильдии (см. /гильдия список)"),
    ):
        await inter.response.defer(ephemeral=True)
        ok = deactivate_guild(гильдия_id)
        await inter.edit_original_response(content="✅ Гильдия деактивирована." if ok else "❌ Гильдия с таким ID не найдена.")

    @guild_group.sub_command(name="подписка", description="Вручную продлить/выставить подписку гильдии (в обход оплаты)")
    async def guild_subscription_extend(
        self,
        inter: disnake.ApplicationCommandInteraction,
        гильдия_id: int = commands.Param(description="Внутренний ID гильдии (см. /гильдия список)"),
        дней: int = commands.Param(description="На сколько дней продлить (от текущего остатка, если он ещё не истёк)"),
    ):
        await inter.response.defer(ephemeral=True)
        if database.get_guild_config(гильдия_id) is None:
            await inter.edit_original_response(content="❌ Гильдия с таким ID не найдена.")
            return
        database.extend_guild_subscription(гильдия_id, дней, source="manual")
        await inter.edit_original_response(content=f"✅ Подписка гильдии `{гильдия_id}` продлена на {дней} дн.")

    # ------------------ /админы ------------------
    @commands.slash_command(name="админы", description="Супер-админы и ручные гранты доступа (только супер-админ)")
    @commands.check(_super_admin_only)
    async def admins_group(self, inter: disnake.ApplicationCommandInteraction):
        pass

    @admins_group.sub_command(name="супер_добавить", description="Выдать пользователю статус супер-админа")
    async def admins_super_add(
        self,
        inter: disnake.ApplicationCommandInteraction,
        юзер: disnake.User = commands.Param(description="Кому выдать статус супер-админа"),
    ):
        await inter.response.defer(ephemeral=True)
        added = add_super_admin(str(юзер.id), str(юзер), str(inter.author.id))
        await inter.edit_original_response(
            content=f"✅ {юзер.mention} теперь супер-админ." if added else f"ℹ️ {юзер.mention} уже супер-админ."
        )

    @admins_group.sub_command(name="супер_удалить", description="Снять статус супер-админа")
    async def admins_super_remove(
        self,
        inter: disnake.ApplicationCommandInteraction,
        юзер: disnake.User = commands.Param(description="У кого снять статус супер-админа"),
    ):
        await inter.response.defer(ephemeral=True)
        removed = remove_super_admin(str(юзер.id))
        await inter.edit_original_response(
            content=f"✅ Статус супер-админа снят с {юзер.mention}." if removed else f"ℹ️ {юзер.mention} не был супер-админом."
        )

    @admins_group.sub_command(name="грант_добавить", description="Выдать доступ игроку вне участвующих гильдий")
    async def admins_grant_add(
        self,
        inter: disnake.ApplicationCommandInteraction,
        юзер: disnake.User = commands.Param(description="Кому выдать доступ"),
        аллай_код: str = commands.Param(description="9-значный код союзника"),
        уровень: str = commands.Param(choices=TIER_CHOICES, description="Уровень доступа"),
        гильдия_id: int = commands.Param(description="Внутренний ID гильдии для скоупа данных (см. /гильдия список)"),
    ):
        await inter.response.defer(ephemeral=True)
        result = await add_grant(self.bot.comlink, str(юзер.id), аллай_код, гильдия_id, уровень, str(inter.author.id))
        if not result.ok:
            await inter.edit_original_response(content=f"❌ {result.error}")
            return
        await inter.edit_original_response(
            content=f"✅ {юзер.mention} ({result.ingame_name}) получил уровень доступа «{уровень}» в гильдии `{гильдия_id}`."
        )

    @admins_group.sub_command(name="грант_удалить", description="Снять ручной грант доступа")
    async def admins_grant_remove(
        self,
        inter: disnake.ApplicationCommandInteraction,
        юзер: disnake.User = commands.Param(description="У кого снять грант"),
    ):
        await inter.response.defer(ephemeral=True)
        removed = remove_grant(str(юзер.id))
        await inter.edit_original_response(
            content=f"✅ Грант снят с {юзер.mention}." if removed else f"ℹ️ У {юзер.mention} не было ручного гранта."
        )

    @admins_group.sub_command(name="список", description="Список супер-админов и ручных грантов доступа")
    async def admins_list(self, inter: disnake.ApplicationCommandInteraction):
        await inter.response.defer(ephemeral=True)
        data = list_admins()

        admin_lines = [
            f"• <@{a['discord_id']}> (добавил: <@{a['added_by']}>, {a['added_at']})"
            for a in data["super_admins"]
        ] or ["— никого —"]

        grant_lines = [
            f"• <@{g['discord_id']}> — `{g['ally_code']}`, уровень **{g['tier']}**, гильдия `{g['guild_id']}` "
            f"(выдал: <@{g['granted_by']}>, {g['granted_at']})"
            for g in data["grants"]
        ] or ["— никого —"]

        embed = disnake.Embed(title="👑 Админы и ручные гранты", color=disnake.Color.gold())
        embed.add_field(name="Супер-админы", value="\n".join(admin_lines), inline=False)
        embed.add_field(name="Ручные гранты доступа", value="\n".join(grant_lines), inline=False)
        await inter.edit_original_response(embed=embed)


def setup(bot):
    bot.add_cog(AdminManagementCog(bot))
