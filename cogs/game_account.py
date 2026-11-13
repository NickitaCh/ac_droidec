"""Open the account-linking website; credentials never go through Discord."""
import disnake
from disnake.ext import commands

import guild_resolver
from services import bot_profiles, game_account
from services.discord_options import yes_no_param


class GameAccountCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.slash_command(name="игровой_аккаунт", description="Открыть форму привязки игрового аккаунта через EA")
    async def account(self, inter: disnake.ApplicationCommandInteraction,
                      альт: bool = yes_no_param(default=False, description="Привязать аккаунт как альт, сохранив основной")):
        access = guild_resolver.resolve_access(inter.author)
        args = (str(inter.author.id), access.get("guild_id"), bot_profiles.worker_profile() or bot_profiles.runtime_profile(), access.get("tier"))
        try:
            game_account.require_pilot(*args)
            url = "https://swgoh-sng.ru/game-account/link?alt=" + ("1" if альт else "0")
            view = disnake.ui.View()
            view.add_item(disnake.ui.Button(style=disnake.ButtonStyle.link, label="Привязать альт" if альт else "Привязать аккаунт", url=url))
            await inter.response.send_message("Откройте форму и введите почту EA, затем код из письма.", view=view, ephemeral=True)
        except game_account.AccountError as error:
            if inter.response.is_done():
                await inter.edit_original_response(content=str(error))
            else:
                await inter.response.send_message(str(error), ephemeral=True)


def setup(bot):
    if (bot_profiles.worker_profile() or bot_profiles.runtime_profile()) == "test":
        bot.add_cog(GameAccountCog(bot))
