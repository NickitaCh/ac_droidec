import disnake
from disnake.ext import commands
import database
import guild_resolver

class RegisterCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.slash_command(
        name="register", 
        description="👑 [ОФИЦЕР] Привязать Discord-аккаунт игрока к его коду союзника SWGOH"
    )
    async def register(
        self,
        inter: disnake.ApplicationCommandInteraction,
        discord_user: disnake.User = commands.Param(
            desc="Выберите пользователя Дискорда (можно вводить ник или тегнуть)"
        ),
        ally_code: str = commands.Param(
            desc="Введите 9-значный код союзника игрока (только цифры, без дефисов)"
        )
    ):
        # Отправляем боту сигнал, что мы обрабатываем запрос (чтобы Дискорд не выдал таймаут за 3 секунды)
        await inter.response.defer()

        guild_id = guild_resolver.resolve_guild_id(inter.author)
        if guild_id is None:
            await inter.edit_original_response(content="❌ Не удалось определить, к какой гильдии вы относитесь.")
            return

        # Очищаем код от возможных случайных пробелов или дефисов
        clean_code = "".join(filter(str.isdigit, ally_code))
        if len(clean_code) != 9:
            await inter.edit_original_response(
                content="❌ **Ошибка:** Код союзника должен состоять ровно из 9 цифр!"
            )
            return

        # Запрашиваем данные игрока через Comlink для валидации кода и получения точного игрового имени
        try:
            player_data = self.bot.comlink.get_player(clean_code)
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

        # Записываем привязку в единую базу данных (перезаписывает старую связь этого
        # Discord ID в пределах гильдии, если уже была)
        try:
            database.set_user_mapping(str(discord_user.id), clean_code, ingame_name, guild_id=guild_id)
        except Exception as e:
            await inter.edit_original_response(
                content=f"❌ **Ошибка БД:** Не удалось сохранить данные в базу: {e}"
            )
            return

        # Формируем красивую карточку-подтверждение для офицерского чата
        embed = disnake.Embed(
            title="🔗 Аккаунт успешно зарегистрирован",
            description=f"Офицер {inter.author.mention} установил жесткую привязку аккаунтов в базе данных.",
            color=disnake.Color.green()
        )
        embed.add_field(name="📱 Пользователь Discord", value=discord_user.mention, inline=True)
        embed.add_field(name="🎮 Игровой ник SWGOH", value=ingame_name, inline=True)
        embed.add_field(name="🔢 Код союзника", value=f"`{clean_code}`", inline=False)
        
        await inter.edit_original_response(embed=embed)

def setup(bot):
    bot.add_cog(RegisterCog(bot))