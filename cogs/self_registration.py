import tempfile

import disnake
from disnake.ext import commands

import database
import guild_resolver
from services.registration import register_player, unregister_player


async def autocomplete_own_registrations(inter: disnake.ApplicationCommandInteraction, string: str):
    """Свои же привязанные аккаунты (для /регистрация_отмена) — не чужие: у
    officer-вызова "участник" автокомплит ally_code всё равно не знает, за кого
    сейчас выбирают (Discord не отдаёт filled_options для User-параметров),
    так что показываем аккаунты автора команды — для self-service (основной
    сценарий) этого достаточно."""
    guild_id = guild_resolver.resolve_guild_id(inter.author)
    if guild_id is None:
        return []
    accounts = database.get_user_registrations(str(inter.author.id), guild_id=guild_id)
    string = string.lower()
    return [
        f"{code} — {name}{' ⭐' if is_main else ''}"
        for code, name, is_main in accounts
        if string in code or string in (name or "").lower()
    ][:25]


class SelfRegistrationCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    async def _do_registration(self, inter, target_user, ally_code, альт, allow_reassign=False):
        result = await register_player(self.bot.comlink, str(target_user.id), ally_code, is_alt=альт, allow_reassign=allow_reassign)
        if not result.ok:
            await inter.edit_original_response(content=f"❌ **Ошибка:** {result.error}")
            return

        embed = disnake.Embed(
            title="🔗 Регистрация выполнена",
            description=(
                f"Теперь команды, где можно не указывать игрока (например `/статы`), "
                f"по умолчанию будут брать основной аккаунт {target_user.mention}."
            ),
            color=disnake.Color.green()
        )
        embed.add_field(name="👤 Discord", value=target_user.mention, inline=True)
        embed.add_field(name="🎮 Игровой ник SWGOH", value=result.ingame_name, inline=True)
        embed.add_field(name="🔢 Код союзника", value=f"`{result.ally_code}`", inline=True)
        embed.add_field(name="⭐ Статус", value="Основной" if result.is_main else "Альт", inline=True)

        if len(result.accounts) > 1:
            lines = [
                f"{'⭐' if row_is_main else '•'} {name} (`{code}`)"
                for code, name, row_is_main in result.accounts
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

        # Гильдия определяется не по Discord-роли вызывающего, а внутри
        # register_player живым запросом к Comlink по самому ally_code — эта
        # команда открыта вообще всем (main.py::ALWAYS_ALLOWED_COMMANDS), в том
        # числе тем, у кого пока нет вообще никакого резолвящегося доступа.
        is_officer_action = участник is not None and участник.id != inter.author.id
        if is_officer_action:
            if not guild_resolver.is_officer_for_resolved_guild(inter.author):
                await inter.edit_original_response(
                    content="❌ Регистрировать других участников могут только офицеры."
                )
                return
            target_user = участник
        else:
            target_user = inter.author

        # Офицер, явно указавший участника, может перевесить код союзника с чужого
        # Discord-аккаунта на нужный — самостоятельная регистрация так не может.
        await self._do_registration(inter, target_user, ally_code, альт, allow_reassign=is_officer_action)

    @commands.slash_command(
        name="регистрация_отмена",
        description="🔓 Отвязать код союзника от себя (или, для офицеров, от другого участника)"
    )
    async def registration_cancel(
        self,
        inter: disnake.ApplicationCommandInteraction,
        ally_code: str = commands.Param(
            default=None,
            description="Какой аккаунт отвязать — обязательно, если у вас привязано больше одного",
            autocomplete=autocomplete_own_registrations,
        ),
        участник: disnake.User = commands.Param(
            default=None,
            description="[Офицер] Отвязать аккаунт другого участника вместо себя"
        ),
    ):
        await inter.response.defer(ephemeral=True)

        is_officer_action = участник is not None and участник.id != inter.author.id
        if is_officer_action:
            if not guild_resolver.is_officer_for_resolved_guild(inter.author):
                await inter.edit_original_response(
                    content="❌ Отвязывать аккаунты других участников могут только офицеры."
                )
                return
            target_user = участник
        else:
            target_user = inter.author

        guild_id = await guild_resolver.require_guild_id(inter)
        if guild_id is None:
            return

        result = await unregister_player(str(target_user.id), guild_id, ally_code)
        if not result.ok:
            await inter.edit_original_response(content=f"❌ {result.error}")
            return

        lines = [f"🔓 Код союзника `{result.ally_code}` ({result.ingame_name}) отвязан от {target_user.mention}."]
        if result.remaining_accounts:
            acc_lines = [
                f"{'⭐' if row_is_main else '•'} {name} (`{code}`)"
                for code, name, row_is_main in result.remaining_accounts
            ]
            lines.append("Остались привязаны:\n" + "\n".join(acc_lines))
        else:
            lines.append("Больше никаких аккаунтов не привязано — при необходимости зарегистрируйтесь заново через `/регистрация`.")
        await inter.edit_original_response(content="\n".join(lines))

    @commands.slash_command(
        name="регистрация_отчёт",
        description="📋 Кто из гильдии привязал Discord к аликоду, а кто ещё нет"
    )
    async def registration_report(self, inter: disnake.ApplicationCommandInteraction):
        await inter.response.defer()

        guild_id = await guild_resolver.require_guild_id(inter)
        if guild_id is None:
            return

        cache = self.bot.guild_roster_caches.get(guild_id)
        if not cache:
            await inter.edit_original_response("⏳ Состав гильдии ещё загружается, попробуйте через минуту.")
            return

        # discord_id и is_main по каждому привязанному ally_code (и основные, и альты)
        by_ally = {
            str(ally_code): (discord_id, bool(is_main))
            for discord_id, ally_code, _, is_main in database.get_all_registrations(guild_id=guild_id)
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
