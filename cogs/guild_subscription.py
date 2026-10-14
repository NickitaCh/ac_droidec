"""`/подписка` — ссылка на страницу подключения и продления гильдии на сайте.
Заявка и оплата целиком на сайте (2026-10-14; раньше была Discord-команда
/гильдия_заявка). Открыта всем (main.py::ALWAYS_ALLOWED_COMMANDS): до оплаты у
заявителя ещё нет никакого уровня доступа. На сервере гильдии в ссылку
подставляется ID этого сервера — форма на сайте заполнит его сама."""

from urllib.parse import urlencode

import disnake
from disnake.ext import commands

SITE_URL = "https://swgoh-sng.ru"


class GuildSubscriptionCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.slash_command(
        name="подписка",
        description="Подключить гильдию к боту или продлить подписку — ссылка на страницу оплаты",
    )
    async def subscription_link(self, inter: disnake.ApplicationCommandInteraction):
        url = f"{SITE_URL}/subscribe"
        if inter.guild_id is not None:
            url += "?" + urlencode({"discord_guild_id": inter.guild_id})
        embed = disnake.Embed(
            title="💳 Подписка гильдии",
            description=(
                f"Подключение и продление — на сайте: {url}\n\n"
                "Войдите через Discord. Для новой гильдии нужен код союзника любого её участника"
                + (" — ID этого сервера уже подставлен в форму." if inter.guild_id else
                   " и ID Discord-сервера гильдии (лучше вызвать эту команду прямо на сервере — подставится сам).")
                + "\nДоступ откроется автоматически в течение минуты после оплаты."
            ),
            color=disnake.Color.gold(),
        )
        await inter.response.send_message(embed=embed, ephemeral=True)


def setup(bot):
    bot.add_cog(GuildSubscriptionCog(bot))
