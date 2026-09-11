"""`/канал` — офицер/лидер гильдии (tier "officer", дефолтный гейт из main.py —
не в MEMBER_ACCESSIBLE_COMMANDS, значит рядовым участникам недоступна) вызывает
эту команду в нужном канале или ветке, бот запоминает его id+имя в
guild_channels (database.py). Наполняет список каналов, из которого затем
выбирают и /настройки в Discord (см. cogs/guild_settings.py — там раньше был
нативный disnake.TextChannel-параметр, теперь выбор ограничен только
зарегистрированными через /канал), и веб-`/settings` (там раньше канал для
оповещений задавался вводом голого Discord ID вручную)."""

import disnake
from disnake.ext import commands

import database
import guild_resolver


class ChannelRegistry(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @commands.slash_command(
        name="канал",
        description="Запомнить этот канал в списке гильдии — станет доступен для выбора в настройках",
    )
    async def register_channel(self, inter: disnake.ApplicationCommandInteraction):
        if inter.guild is None:
            await inter.response.send_message(
                "❌ Эту команду нужно вызывать в самом канале или ветке на сервере гильдии, не в личке.",
                ephemeral=True,
            )
            return

        guild_id = await guild_resolver.require_guild_id(inter)
        if guild_id is None:
            return

        channel = inter.channel
        name = getattr(channel, "name", None) or str(channel.id)
        channel_type = channel.type.name if hasattr(channel, "type") else None
        database.register_guild_channel(guild_id, channel.id, name, channel_type=channel_type, registered_by=inter.author.id)

        await inter.response.send_message(
            f"✅ Канал «{name}» запомнен для гильдии — теперь его можно выбрать в настройках оповещений "
            f"(`/настройки` или веб-дашборд `/settings`).",
            ephemeral=True,
        )


def setup(bot: commands.Bot):
    bot.add_cog(ChannelRegistry(bot))
