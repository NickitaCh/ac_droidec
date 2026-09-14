"""Самообслуживание платного подключения гильдии — `/гильдия_заявка`. Открыта
всем (см. main.py::ALWAYS_ALLOWED_COMMANDS), т.к. до оплаты у заявителя нет
никакого tier доступа. Должна выполняться на Discord-сервере самой гильдии
(discord_guild_id берётся из inter.guild_id, не вводится вручную).

Бизнес-логика — services/guild_admin.py::add_guild (is_active=False заводит
неактивную "заявку") + services/payments.py::build_payment_link. Ручное
управление гильдиями (/гильдия добавить и т.п., cogs/admin_management.py)
этот файл не трогает."""

import disnake
from disnake.ext import commands

from services.guild_admin import add_guild
from services.payments import PERIOD_CHOICES, build_payment_link

PERIOD_CHOICE_OPTIONS = [
    disnake.OptionChoice(name="Месяц", value=30),
    disnake.OptionChoice(name="Год", value=365),
]


class GuildSubscriptionCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.slash_command(
        name="гильдия_заявка",
        description="Подключить вашу гильдию к боту платно — команду нужно выполнить на сервере самой гильдии",
    )
    async def guild_apply(
        self,
        inter: disnake.ApplicationCommandInteraction,
        аллай_код: str = commands.Param(description="9-значный код союзника любого участника гильдии"),
        период: int = commands.Param(choices=PERIOD_CHOICE_OPTIONS, description="Срок подписки"),
    ):
        await inter.response.defer(ephemeral=True)
        if inter.guild_id is None:
            await inter.edit_original_response(
                content="❌ Эту команду нужно выполнить на Discord-сервере самой гильдии, не в личных сообщениях."
            )
            return

        result = await add_guild(self.bot.comlink, аллай_код, str(inter.guild_id), is_active=False)
        if not result.ok:
            await inter.edit_original_response(content=f"❌ {result.error}")
            return

        link = await build_payment_link(result.guild_id, result.name, период)
        if not link.ok:
            await inter.edit_original_response(content=f"❌ {link.error}")
            return

        price = PERIOD_CHOICES[период]
        embed = disnake.Embed(title="💳 Оплата подключения гильдии", color=disnake.Color.gold())
        embed.add_field(name="Гильдия", value=result.name, inline=False)
        embed.add_field(name="Период", value="1 месяц" if период == 30 else "1 год", inline=True)
        embed.add_field(name="Сумма", value=f"{price} ₽", inline=True)
        embed.add_field(name="Ссылка на оплату", value=link.url, inline=False)
        embed.set_footer(text="Доступ откроется автоматически в течение минуты после оплаты.")
        await inter.edit_original_response(embed=embed)


def setup(bot):
    bot.add_cog(GuildSubscriptionCog(bot))
