"""Existing-session read pilot; game credentials never go through Discord."""
import disnake
from disnake.ext import commands

import guild_resolver
from services import bot_profiles, game_account
from services.discord_options import yes_no_param


class GameAccountCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.slash_command(name="игровой_аккаунт", description="Тест АС: статус подключения и чтение своего игрового инвентаря")
    async def account(self, inter: disnake.ApplicationCommandInteraction,
                      действие: str = commands.Param(default="статус", choices=["статус", "прочитать", "отключить"]),
                      альт: bool = yes_no_param(default=False, description="Подключение вашего альта вместо основного аккаунта"),
                      аликод: str = commands.Param(default=None, description="Свой код союзника, если альтов несколько")):
        access = guild_resolver.resolve_access(inter.author)
        args = (str(inter.author.id), access.get("guild_id"), bot_profiles.worker_profile() or bot_profiles.runtime_profile(), access.get("tier"))
        try:
            game_account.require_pilot(*args)
            await inter.response.defer(ephemeral=True)
            if альт and аликод:
                raise game_account.AccountError("Выберите «альт: Да» или свой код союзника.")
            if аликод and guild_resolver.normalize_ally_code(аликод) is None:
                raise game_account.AccountError("Укажите код союзника из 9 цифр.")
            code = game_account.select_account(args[0], args[1], alt=альт,
                                               ally_code=guild_resolver.normalize_ally_code(аликод) if аликод else None)
            if действие == "прочитать":
                result = await game_account.refresh(*args, ally_code=code)
                text = f"Аккаунт {code}. Инвентарь прочитан: {result['inventory_bytes']} байт, {result['inventory_records']} записей. Разбор ресурсов и модулей ещё готовится."
            elif действие == "отключить":
                game_account.disconnect(*args, ally_code=code)
                text = f"Сессия аккаунта {code} удалена. Привязки EA/Google в игре сохранены."
            else:
                account = game_account.status(*args, ally_code=code)
                text = f"Аккаунт {code}: " + (("подключён." if account['state'] == 'connected' else "последнее чтение не удалось.") if account else "игровая сессия ещё не подключена.")
            await inter.edit_original_response(content=text + f"\nПодключение: https://swgoh-sng.ru/game-account?ally_code={code} (тестовая версия сайта).")
        except game_account.AccountError as error:
            if inter.response.is_done():
                await inter.edit_original_response(content=str(error))
            else:
                await inter.response.send_message(str(error), ephemeral=True)


def setup(bot):
    if (bot_profiles.worker_profile() or bot_profiles.runtime_profile()) == "test":
        bot.add_cog(GameAccountCog(bot))
