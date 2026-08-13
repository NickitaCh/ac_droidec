import asyncio

import disnake
from disnake.ext import commands

import database


class SelfRegistrationCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.slash_command(
        name="регистрация",
        description="🔗 Привязать себя к своему коду союзника SWGOH — для команд вида «мой персонаж»"
    )
    async def registration(
        self,
        inter: disnake.ApplicationCommandInteraction,
        ally_code: str = commands.Param(
            desc="Ваш 9-значный код союзника (только цифры, без дефисов)"
        )
    ):
        await inter.response.defer(ephemeral=True)

        clean_code = "".join(filter(str.isdigit, ally_code))
        if len(clean_code) != 9:
            await inter.edit_original_response(
                content="❌ **Ошибка:** Код союзника должен состоять ровно из 9 цифр!"
            )
            return

        try:
            player_data = await asyncio.to_thread(self.bot.comlink.get_player, clean_code)
            if not player_data or "name" not in player_data:
                await inter.edit_original_response(
                    content=f"❌ **Ошибка:** Игрок с кодом союзника `{clean_code}` не найден на серверах EA/CG. Проверьте цифры."
                )
                return

            ingame_name = player_data["name"]
        except Exception as e:
            await inter.edit_original_response(
                content=f"⚠️ **Ошибка Comlink:** Не удалось проверить код из-за сбоя связи с сервером. Ошибка: {e}"
            )
            return

        try:
            database.set_user_registration(str(inter.author.id), clean_code, ingame_name)
        except Exception as e:
            await inter.edit_original_response(
                content=f"❌ **Ошибка БД:** Не удалось сохранить регистрацию: {e}"
            )
            return

        embed = disnake.Embed(
            title="🔗 Регистрация выполнена",
            description=(
                "Теперь команды, где можно не указывать игрока (например `/статы`), "
                "по умолчанию будут брать именно ваш аккаунт."
            ),
            color=disnake.Color.green()
        )
        embed.add_field(name="🎮 Игровой ник SWGOH", value=ingame_name, inline=True)
        embed.add_field(name="🔢 Код союзника", value=f"`{clean_code}`", inline=True)

        await inter.edit_original_response(embed=embed)


def setup(bot):
    bot.add_cog(SelfRegistrationCog(bot))
