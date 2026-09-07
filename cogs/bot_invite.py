"""`/пригласить` — самостоятельный от cogs/guild_subscription.py (тот всё ещё не
задеплоен, ждёт реальных ключей Prodamus): просто отдаёт ссылку-приглашение
(services/discord_invite.py) в виде кликабельной кнопки, без всякой оплаты и
без Comlink/БД. Открыта всем (см. main.py::ALWAYS_ALLOWED_COMMANDS) и работает
в личке бота — до вступления в сервер у пользователя ещё нет никакого tier."""

import disnake
from disnake.ext import commands

from services import discord_invite


class BotInviteCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.slash_command(
        name="пригласить",
        description="Получить ссылку, чтобы добавить бота на свой Discord-сервер",
    )
    async def invite(self, inter: disnake.ApplicationCommandInteraction):
        url = discord_invite.build_invite_url()
        if not url:
            await inter.response.send_message(
                "❌ Приглашение сейчас недоступно (не настроен DISCORD_CLIENT_ID на сервере бота).",
                ephemeral=True,
            )
            return

        embed = disnake.Embed(
            title="🤖 Добавить AC Droidec на сервер",
            description=(
                "Бот сразу запросит нужные права (роль на день рождения, "
                "антиспам-модерация, вложения и т.п.) — Discord создаст под них "
                "отдельную роль автоматически.\n\n"
                "Пригласить можно в любой момент — до оплаты подписки на гильдию или после."
            ),
            color=disnake.Color.blurple(),
        )
        view = disnake.ui.View()
        view.add_item(disnake.ui.Button(style=disnake.ButtonStyle.link, label="➕ Пригласить на сервер", url=url))
        await inter.response.send_message(embed=embed, view=view, ephemeral=True)


def setup(bot):
    bot.add_cog(BotInviteCog(bot))
