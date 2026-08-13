import asyncio
import tempfile

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
        ),
        альт: bool = commands.Param(
            default=False,
            description="Добавить как второй аккаунт (альт), не заменяя основной"
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

        # Первая регистрация всегда основная, даже если попросили альт — иначе
        # получится аккаунт без единого основного.
        has_existing = bool(database.get_user_registrations(str(inter.author.id)))
        is_main = (not альт) or (not has_existing)

        try:
            database.set_user_registration(str(inter.author.id), clean_code, ingame_name, is_main=is_main)
        except Exception as e:
            await inter.edit_original_response(
                content=f"❌ **Ошибка БД:** Не удалось сохранить регистрацию: {e}"
            )
            return

        accounts = database.get_user_registrations(str(inter.author.id))

        embed = disnake.Embed(
            title="🔗 Регистрация выполнена",
            description=(
                "Теперь команды, где можно не указывать игрока (например `/статы`), "
                "по умолчанию будут брать ваш основной аккаунт."
            ),
            color=disnake.Color.green()
        )
        embed.add_field(name="🎮 Игровой ник SWGOH", value=ingame_name, inline=True)
        embed.add_field(name="🔢 Код союзника", value=f"`{clean_code}`", inline=True)
        embed.add_field(name="⭐ Статус", value="Основной" if is_main else "Альт", inline=True)

        if len(accounts) > 1:
            lines = [
                f"{'⭐' if row_is_main else '•'} {name} (`{code}`)"
                for code, name, row_is_main in accounts
            ]
            embed.add_field(name="Все привязанные аккаунты", value="\n".join(lines), inline=False)

        await inter.edit_original_response(embed=embed)

    @commands.slash_command(
        name="регистрация_отчёт",
        description="📋 Кто из гильдии привязал Discord к аликоду, а кто ещё нет"
    )
    async def registration_report(self, inter: disnake.ApplicationCommandInteraction):
        await inter.response.defer()

        cache = self.bot.guild_roster_cache
        if not cache:
            await inter.edit_original_response("⏳ Состав гильдии ещё загружается, попробуйте через минуту.")
            return

        registered_by_ally = {
            str(ally_code): discord_id
            for discord_id, ally_code, _ in database.get_all_main_registrations()
        }

        registered_lines = []
        missing_lines = []
        for name in sorted(cache.keys()):
            ally_code = str(cache[name])
            discord_id = registered_by_ally.get(ally_code)
            if discord_id:
                registered_lines.append(f"<@{discord_id}> — {name} (`{ally_code}`)")
            else:
                missing_lines.append(f"{name} (`{ally_code}`)")

        total = len(cache)
        text_parts = [f"✅ **Зарегистрировано: {len(registered_lines)}/{total}**", ""]
        text_parts.extend(registered_lines or ["— никого —"])
        text_parts.append("")
        text_parts.append(f"❌ **Не зарегистрированы ({len(missing_lines)}):**")
        text_parts.extend(missing_lines or ["— все зарегистрированы —"])
        text = "\n".join(text_parts)

        if len(text) <= 4000:
            embed = disnake.Embed(title="📋 Регистрация по гильдии", description=text, color=disnake.Color.blurple())
            await inter.edit_original_response(embed=embed)
        else:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", delete=False, suffix=".txt") as f:
                f.write(text)
                fname = f.name
            await inter.edit_original_response(
                content=f"📋 Зарегистрировано {len(registered_lines)}/{total} — полный список слишком длинный для сообщения, файлом:"
            )
            await inter.channel.send(file=disnake.File(fname, filename="registration_report.txt"))


def setup(bot):
    bot.add_cog(SelfRegistrationCog(bot))
