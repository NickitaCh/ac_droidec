import asyncio
import tempfile

import disnake
from disnake.ext import commands

import database


OFFICER_ROLE_ID = 1153753506772164629


def _is_officer(inter: disnake.ApplicationCommandInteraction) -> bool:
    """Та же логика, что и в главном ролевом чеке main.py (_check_allowed_role):
    читаем сырые author._roles, а не author.roles — Interaction.guild может быть
    None для незакэшированных гильдий, а _roles заполняется прямо из пейлоада."""
    author = inter.author
    role_ids = getattr(author, "_roles", None)
    if role_ids is None:
        role_ids = [role.id for role in getattr(author, "roles", [])]
    return OFFICER_ROLE_ID in role_ids


class SelfRegistrationCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    async def _do_registration(self, inter, target_user, ally_code, альт):
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
        has_existing = bool(database.get_user_registrations(str(target_user.id)))
        is_main = (not альт) or (not has_existing)

        try:
            database.set_user_registration(str(target_user.id), clean_code, ingame_name, is_main=is_main)
        except Exception as e:
            await inter.edit_original_response(
                content=f"❌ **Ошибка БД:** Не удалось сохранить регистрацию: {e}"
            )
            return

        accounts = database.get_user_registrations(str(target_user.id))

        embed = disnake.Embed(
            title="🔗 Регистрация выполнена",
            description=(
                f"Теперь команды, где можно не указывать игрока (например `/статы`), "
                f"по умолчанию будут брать основной аккаунт {target_user.mention}."
            ),
            color=disnake.Color.green()
        )
        embed.add_field(name="👤 Discord", value=target_user.mention, inline=True)
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
        name="регистрация",
        description="🔗 Привязать себя (или, для офицеров, другого участника) к коду союзника SWGOH"
    )
    async def registration(
        self,
        inter: disnake.ApplicationCommandInteraction,
        ally_code: str = commands.Param(
            desc="9-значный код союзника (только цифры, без дефисов)"
        ),
        участник: disnake.User = commands.Param(
            default=None,
            description="[Офицер] Зарегистрировать другого участника вместо себя"
        ),
        альт: bool = commands.Param(
            default=False,
            description="Добавить как второй аккаунт (альт), не заменяя основной"
        )
    ):
        await inter.response.defer(ephemeral=True)

        if участник is not None and участник.id != inter.author.id:
            if not _is_officer(inter):
                await inter.edit_original_response(
                    content="❌ Регистрировать других участников могут только офицеры."
                )
                return
            target_user = участник
        else:
            target_user = inter.author

        await self._do_registration(inter, target_user, ally_code, альт)

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

        # discord_id и is_main по каждому привязанному ally_code (и основные, и альты)
        by_ally = {
            str(ally_code): (discord_id, bool(is_main))
            for discord_id, ally_code, _, is_main in database.get_all_registrations()
        }

        main_lines = []
        alt_lines = []
        missing_lines = []
        for name in sorted(cache.keys()):
            ally_code = str(cache[name])
            entry = by_ally.get(ally_code)
            if entry is None:
                missing_lines.append(f"{name} (`{ally_code}`)")
                continue
            discord_id, is_main = entry
            line = f"<@{discord_id}> — {name} (`{ally_code}`)"
            (main_lines if is_main else alt_lines).append(line)

        total = len(cache)
        text_parts = [f"❌ **Не привязаны ({len(missing_lines)}):**"]
        text_parts.extend(missing_lines or ["— все привязаны —"])
        text_parts.append("")
        text_parts.append(f"✅ **Основной аккаунт: {len(main_lines)}/{total}**")
        text_parts.extend(main_lines or ["— никого —"])
        if alt_lines:
            text_parts.append("")
            text_parts.append(f"🔸 **Привязаны как альт ({len(alt_lines)}):**")
            text_parts.extend(alt_lines)
        text = "\n".join(text_parts)

        if len(text) <= 4000:
            embed = disnake.Embed(title="📋 Регистрация по гильдии", description=text, color=disnake.Color.blurple())
            await inter.edit_original_response(embed=embed)
        else:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", delete=False, suffix=".txt") as f:
                f.write(text)
                fname = f.name
            await inter.edit_original_response(
                content=f"📋 Основной аккаунт: {len(main_lines)}/{total} — полный список слишком длинный для сообщения, файлом:"
            )
            await inter.channel.send(file=disnake.File(fname, filename="registration_report.txt"))


def setup(bot):
    bot.add_cog(SelfRegistrationCog(bot))
