import disnake
from disnake.ext import commands, tasks
from datetime import datetime, date, timedelta
from zoneinfo import ZoneInfo
import database
import guild_resolver
from services import feature_flags
from services.config_status import config_warning_text

MSK = ZoneInfo("Europe/Moscow")

def parse_birthday(date_str: str):
    """Парсит строку в формате ДД-ММ-ГГГГ или ДД-ММ.
    Возвращает (day, month, year). Если год не указан, year = None."""
    for fmt in ("%d-%m-%Y", "%d-%m"):
        try:
            dt = datetime.strptime(date_str, fmt)
        except ValueError:
            continue
        # strptime без года в формате сам подставляет 1900 (особенность модуля
        # time/_strptime) — это не настоящий год, поэтому отбрасываем его и
        # явно возвращаем None вместо магического числа, которое могло бы
        # случайно совпасть с чьим-то настоящим годом рождения.
        year = dt.year if fmt == "%d-%m-%Y" else None
        return dt.day, dt.month, year
    raise ValueError("Неверный формат даты. Используйте ДД-ММ-ГГГГ или ДД-ММ")

def next_birthday(day: int, month: int, year: int | None, today: date):
    """Вычисляет ближайший день рождения (date) после или равный today."""
    try:
        bday_this_year = date(today.year, month, day)
    except ValueError:
        # 29 февраля в невисокосный год -> 28 февраля
        if month == 2 and day == 29:
            bday_this_year = date(today.year, 2, 28)
        else:
            raise
    if bday_this_year >= today:
        return bday_this_year
    # Следующий год
    try:
        return date(today.year + 1, month, day)
    except ValueError:
        if month == 2 and day == 29:
            return date(today.year + 1, 2, 28)
        raise

class Birthday(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        # Защита от дублирования: (guild_id, discord_id) -> дата, для которой уже поздравляли
        self.last_congratulated = {}
        self.check_loop.start()
        self.refresh_member_cache_loop.start()

    def cog_unload(self):
        self.check_loop.cancel()
        self.refresh_member_cache_loop.cancel()

    @tasks.loop(seconds=30)
    async def check_loop(self):
        await self.bot.wait_until_ready()
        now = datetime.now(MSK)
        hour = now.hour
        minute = now.minute
        today = now.date()

        # Без интеракции резолвить гильдию по роли нечем — идём по всем
        # зарегистрированным гильдиям на каждом тике (как в rotation_ping.py).
        for guild_cfg in database.get_all_guild_configs():
            if not guild_cfg.get("birthday_channel_id") or not guild_cfg.get("birthday_role_id"):
                continue
            if not feature_flags.is_enabled(guild_cfg["id"], "birthdays"):
                continue
            # Утром в 6:00 – поздравление и выдача роли
            if hour == 6 and minute == 0:
                await self.handle_birthday_morning(guild_cfg, today)
            # В полночь – снятие роли со всех, у кого она есть, и сброс кэша поздравлений
            if hour == 0 and minute == 0:
                await self.handle_birthday_reset(guild_cfg)

    async def handle_birthday_morning(self, guild_cfg, today: date):
        gid = guild_cfg["id"]
        gname = guild_cfg["name"]
        all_bdays = database.get_all_birthdays(guild_id=gid)
        if not all_bdays:
            return

        channel = self.bot.get_channel(int(guild_cfg["birthday_channel_id"]))
        if channel is None:
            print(f"❌ [Birthday] [{gname}] Канал {guild_cfg['birthday_channel_id']} не найден")
            return

        guild = channel.guild
        role = guild.get_role(int(guild_cfg["birthday_role_id"])) if guild else None
        if role is None:
            print(f"❌ [Birthday] [{gname}] Роль {guild_cfg['birthday_role_id']} не найдена")
            return

        for discord_id_str, day, month, year in all_bdays:
            # Проверяем, совпадает ли день рождения с сегодняшней датой
            if month != today.month or day != today.day:
                continue

            discord_id = int(discord_id_str)
            key = (gid, discord_id)

            # Проверяем, не поздравляли ли уже сегодня этого пользователя
            if self.last_congratulated.get(key) == today:
                continue  # уже поздравляли – пропускаем

            member = guild.get_member(discord_id)
            if member is None:
                continue

            # Выдаём роль, если ещё нет
            if role not in member.roles:
                try:
                    await member.add_roles(role)
                    print(f"✅ [Birthday] [{gname}] Роль выдана {member.display_name}")
                except Exception as e:
                    print(f"❌ [Birthday] [{gname}] Ошибка выдачи роли {member}: {e}")

            # Отправляем поздравление
            try:
                await channel.send(f"### 🎉 {member.mention} сегодня отмечает день рождения!")
                print(f"✅ [Birthday] [{gname}] Поздравление для {member.display_name}")
                # Запоминаем, что сегодня уже поздравляли
                self.last_congratulated[key] = today
            except Exception as e:
                print(f"❌ [Birthday] [{gname}] Ошибка отправки: {e}")

    async def handle_birthday_reset(self, guild_cfg):
        gname = guild_cfg["name"]
        # Снимаем роль с тех, у кого она есть
        channel = self.bot.get_channel(int(guild_cfg["birthday_channel_id"]))
        if channel is None:
            return
        guild = channel.guild
        role = guild.get_role(int(guild_cfg["birthday_role_id"])) if guild else None
        if role is None:
            return

        for member in guild.members:
            if role in member.roles:
                try:
                    await member.remove_roles(role)
                    print(f"✅ [Birthday] [{gname}] Роль снята с {member.display_name}")
                except Exception as e:
                    print(f"❌ [Birthday] [{gname}] Ошибка снятия роли с {member}: {e}")

        # Сбрасываем кэш поздравлений этой гильдии (на случай, если полночь пропущена)
        gid = guild_cfg["id"]
        for key in [k for k in self.last_congratulated if k[0] == gid]:
            del self.last_congratulated[key]

    @check_loop.before_loop
    async def before_loop(self):
        await self.bot.wait_until_ready()

    @tasks.loop(hours=1)
    async def refresh_member_cache_loop(self):
        """Часовой снимок discord_id -> display_name по всем участникам Discord-серверов
        гильдий бота, в database.discord_member_cache. Живёт здесь (не в отдельном cog'е),
        потому что раньше страдал именно /др — веб-дашборд не может резолвить имя игрока,
        у которого сохранён день рождения, но который ни разу не логинился в веб и не
        проходил /регистрация (см. get_username_for_discord_id). Собирается по всем
        гильдиям сразу, а не только с настроенным birthday-каналом, чтобы не зависеть
        от birthday-конфига — ДР можно добавить любому участнику через /др добавить."""
        members = []
        seen_guild_ids = set()
        for guild_cfg in database.get_all_guild_configs():
            discord_guild_id = guild_cfg.get("discord_guild_id")
            if not discord_guild_id or discord_guild_id in seen_guild_ids:
                continue
            seen_guild_ids.add(discord_guild_id)
            discord_guild = self.bot.get_guild(int(discord_guild_id))
            if discord_guild is None:
                continue
            for member in discord_guild.members:
                members.append((str(member.id), member.display_name))
        if members:
            database.sync_discord_member_cache(members)

    @refresh_member_cache_loop.before_loop
    async def before_refresh_member_cache_loop(self):
        await self.bot.wait_until_ready()

    # -------------------- /др — панель (2026-10-14) --------------------
    # Одна команда вместо добавить/удалить/список: список по ближайшей дате в сообщении,
    # участник — нативным списком Discord с поиском, дата — окном.
    @commands.slash_command(name="др", description="Дни рождения участников гильдии — панель")
    @commands.check(lambda inter: guild_resolver.is_officer_for_resolved_guild(inter.author))
    async def birthday_panel(self, inter: disnake.ApplicationCommandInteraction):
        guild_id = await guild_resolver.require_feature(inter, "birthdays")
        if guild_id is None:
            return
        if inter.guild is None:
            await inter.response.send_message("❌ Откройте `/др` на сервере гильдии — участник выбирается из списка сервера.",
                                              ephemeral=True)
            return
        panel = BirthdayPanel(self, guild_id, inter)
        await inter.response.send_message(panel.render(), view=panel.build(), ephemeral=True,
                                          allowed_mentions=disnake.AllowedMentions.none())

    def birthday_lines(self, guild_id: int, guild) -> list[str]:
        today = datetime.now(MSK).date()
        rows = []
        for discord_id_str, day, month, year in database.get_all_birthdays(guild_id=guild_id):
            if guild is not None and guild.get_member(int(discord_id_str)) is None:
                continue  # ушёл с сервера
            rows.append((next_birthday(day, month, year, today), discord_id_str, day, month, year))
        rows.sort()
        lines = []
        for _next, discord_id_str, day, month, year in rows:
            date_str = f"{day:02d}.{month:02d}" + (f".{year}" if year is not None else "")
            lines.append(f"• <@{discord_id_str}> — {date_str}")
        return lines


class BirthdayPanel(disnake.ui.View):
    def __init__(self, cog, guild_id: int, inter: disnake.ApplicationCommandInteraction):
        super().__init__(timeout=840)  # токен ответа на команду живёт 15 минут
        self.cog = cog
        self.guild_id = guild_id
        self.inter = inter
        self.author_id = inter.author.id
        self.user = None
        self.confirm_remove = False

    async def interaction_check(self, interaction: disnake.MessageInteraction) -> bool:
        return interaction.author.id == self.author_id

    def _current(self):
        if self.user is None:
            return None
        return next(((d, m, y) for did, d, m, y in database.get_all_birthdays(guild_id=self.guild_id)
                     if did == str(self.user.id)), None)

    def render(self) -> str:
        lines = self.cog.birthday_lines(self.guild_id, self.inter.guild)
        shown = lines[:40]
        more = f"\n…и ещё {len(lines) - 40}" if len(lines) > 40 else ""
        text = "🎂 **Дни рождения гильдии** (по ближайшей дате)\n" + ("\n".join(shown) + more if shown else "— пока никого —")
        current = self._current()
        if self.user is not None:
            text += (f"\n\nВыбран {self.user.mention}: "
                     + (f"{current[0]:02d}.{current[1]:02d}" + (f".{current[2]}" if current[2] else "") if current else "дата не задана"))
        warning = config_warning_text(database.get_guild_config(self.guild_id), "birthday")
        return text + (f"\n{warning}" if warning else "") + "\n-# Выберите участника → «Задать дату» или «Удалить»."

    def build(self):
        self.clear_items()
        user_select = disnake.ui.UserSelect(
            placeholder="Участник сервера", row=0,
            default_values=[disnake.SelectDefaultValue(self.user.id, disnake.SelectDefaultValueType.user)] if self.user else None)
        user_select.callback = self._on_user
        self.add_item(user_select)
        set_date = disnake.ui.Button(label="Задать дату", style=disnake.ButtonStyle.success, row=1, disabled=self.user is None)
        set_date.callback = self._on_set
        self.add_item(set_date)
        remove = disnake.ui.Button(label="Точно удалить?" if self.confirm_remove else "Удалить",
                                   style=disnake.ButtonStyle.danger, row=1, disabled=self._current() is None)
        remove.callback = self._on_remove
        self.add_item(remove)
        return self

    async def refresh(self, interaction=None):
        kwargs = dict(content=self.render(), view=self.build(), allowed_mentions=disnake.AllowedMentions.none())
        if interaction is not None:
            await interaction.response.edit_message(**kwargs)
        else:
            await self.inter.edit_original_response(**kwargs)

    async def _on_user(self, interaction):
        self.user = interaction.resolved_values[0] if interaction.resolved_values else None
        self.confirm_remove = False
        await self.refresh(interaction)

    async def _on_set(self, interaction):
        await interaction.response.send_modal(BirthdayDateModal(self))

    async def _on_remove(self, interaction):
        if not self.confirm_remove:
            self.confirm_remove = True
            await self.refresh(interaction)
            return
        self.confirm_remove = False
        database.remove_birthday(str(self.user.id), guild_id=self.guild_id)
        await self.refresh(interaction)


class BirthdayDateModal(disnake.ui.Modal):
    def __init__(self, panel: BirthdayPanel):
        self.panel = panel
        current = panel._current()
        value = (f"{current[0]:02d}-{current[1]:02d}" + (f"-{current[2]}" if current[2] else "")) if current else None
        super().__init__(title=f"День рождения: {panel.user.display_name}"[:45], components=[
            disnake.ui.TextInput(label="Дата: ДД-ММ-ГГГГ или ДД-ММ", custom_id="date", max_length=10, value=value),
        ])

    async def callback(self, interaction: disnake.ModalInteraction):
        try:
            day, month, year = parse_birthday(interaction.text_values["date"].strip())
        except ValueError as e:
            await interaction.response.send_message(f"❌ {e}", ephemeral=True)
            return
        database.add_birthday(str(self.panel.user.id), day, month, year, guild_id=self.panel.guild_id)
        await interaction.response.send_message(
            f"✅ День рождения {self.panel.user.mention} сохранён: {day:02d}-{month:02d}" + (f"-{year}" if year else ""),
            ephemeral=True)
        try:
            await self.panel.refresh()
        except disnake.HTTPException:
            pass  # панель старше 15 минут — дата уже сохранена


def setup(bot: commands.Bot):
    bot.add_cog(Birthday(bot))