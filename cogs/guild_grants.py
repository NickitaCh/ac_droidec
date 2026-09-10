"""Ручной доступ для игроков вне обычного резолва по игровому рангу — офицерская
версия /админы грант_* (cogs/admin_management.py), но с жёстко зафиксированным
guild_id (своя гильдия, не выбирается) и без доступа к чужим гильдиям — см.
services/guild_admin.py::add_guild_scoped_grant/remove_guild_scoped_grant за
проверкой, что офицер не может перехватить/снять чужой грант."""

import disnake
from disnake.ext import commands

import guild_resolver
from services.guild_admin import add_guild_scoped_grant, list_grants_for_guild, remove_guild_scoped_grant

TIER_CHOICES = [
    disnake.OptionChoice(name="Игрок", value="member"),
    disnake.OptionChoice(name="Офицер", value="officer"),
]


class GuildGrantsCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.slash_command(name="грант", description="Ручной доступ к боту для участника вашей гильдии (офицеры)")
    @commands.check(lambda inter: guild_resolver.is_officer_for_resolved_guild(inter.author))
    async def grant_group(self, inter: disnake.ApplicationCommandInteraction):
        pass

    @grant_group.sub_command(name="добавить", description="Выдать участнику ручной доступ в вашей гильдии")
    async def grant_add(
        self,
        inter: disnake.ApplicationCommandInteraction,
        юзер: disnake.User = commands.Param(description="Кому выдать доступ"),
        аллай_код: str = commands.Param(description="9-значный код союзника"),
        уровень: str = commands.Param(choices=TIER_CHOICES, description="Уровень доступа"),
    ):
        await inter.response.defer(ephemeral=True)
        guild_id = await guild_resolver.require_guild_id(inter)
        if guild_id is None:
            return
        result = await add_guild_scoped_grant(self.bot.comlink, str(юзер.id), аллай_код, уровень, guild_id, str(inter.author.id))
        if not result.ok:
            await inter.edit_original_response(content=f"❌ {result.error}")
            return
        await inter.edit_original_response(
            content=f"✅ {юзер.mention} ({result.ingame_name}) получил уровень доступа «{уровень}» в вашей гильдии."
        )

    @grant_group.sub_command(name="удалить", description="Снять ранее выданный ручной доступ (только в вашей гильдии)")
    async def grant_remove(
        self,
        inter: disnake.ApplicationCommandInteraction,
        юзер: disnake.User = commands.Param(description="У кого снять доступ"),
    ):
        await inter.response.defer(ephemeral=True)
        guild_id = await guild_resolver.require_guild_id(inter)
        if guild_id is None:
            return
        result = remove_guild_scoped_grant(str(юзер.id), guild_id)
        if not result.ok:
            await inter.edit_original_response(content=f"❌ {result.error}")
            return
        await inter.edit_original_response(content=f"✅ Доступ снят с {юзер.mention}.")

    @grant_group.sub_command(name="список", description="Список ручных грантов доступа в вашей гильдии")
    async def grant_list(self, inter: disnake.ApplicationCommandInteraction):
        await inter.response.defer(ephemeral=True)
        guild_id = await guild_resolver.require_guild_id(inter)
        if guild_id is None:
            return
        grants = list_grants_for_guild(guild_id)
        lines = [
            f"• <@{g['discord_id']}> — `{g['ally_code']}` ({g.get('ingame_name') or '?'}), уровень **{g['tier']}** "
            f"(выдал: <@{g['granted_by']}>, {g['granted_at']})"
            for g in grants
        ] or ["— никого —"]
        embed = disnake.Embed(title="👑 Ручные гранты доступа вашей гильдии", description="\n".join(lines), color=disnake.Color.gold())
        await inter.edit_original_response(embed=embed)


def setup(bot):
    bot.add_cog(GuildGrantsCog(bot))
