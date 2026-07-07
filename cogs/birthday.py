import disnake
from disnake.ext import commands, tasks
from datetime import datetime, date, timedelta
from zoneinfo import ZoneInfo
import database

MSK = ZoneInfo("Europe/Moscow")

def parse_birthday(date_str: str):
    """Парсит строку в формате ДД-ММ-ГГГГ или ДД-ММ.
    Возвращает (day, month, year). Если год не указан, подставляется 2000."""
    for fmt in ("%d-%m-%Y", "%d-%m"):
        try:
            dt = datetime.strptime(date_str, fmt)
            return dt.day, dt.month, dt.year
        except ValueError:
            continue
    raise ValueError("Неверный формат даты. Используйте ДД-ММ-ГГГГ или ДД-ММ")

def next_birthday(day: int, month: int, year: int, today: date):
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
        self.channel_id = bot.BIRTHDAY_CHANNEL_ID
        self.role_id = bot.BIRTHDAY_ROLE_ID
        self.check_loop.start()

    def cog_unload(self):
        self.check_loop.cancel()

    @tasks.loop(seconds=30)
    async def check_loop(self):
        await self.bot.wait_until_ready()
        now = datetime.now(MSK)
        hour = now.hour
        minute = now.minute
        today = now.date()

        if hour == 17 and minute == 20:
            await self.handle_birthday_morning(today)
        if hour == 0 and minute == 0:
            await self.handle_birthday_reset()

    async def handle_birthday_morning(self, today: date):
        all_bdays = database.get_all_birthdays()
        if not all_bdays:
            return

        channel = self.bot.get_channel(self.channel_id)
        if channel is None:
            print(f"❌ [Birthday] Канал {self.channel_id} не найден")
            return

        guild = channel.guild
        role = guild.get_role(self.role_id) if guild else None
        if role is None:
            print(f"❌ [Birthday] Роль {self.role_id} не найдена")
            return

        for discord_id_str, day, month, year in all_bdays:
            if month == today.month and day == today.day:
                member = guild.get_member(int(discord_id_str))
                if member is None:
                    continue
                if role not in member.roles:
                    try:
                        await member.add_roles(role)
                        print(f"✅ [Birthday] Роль выдана {member.display_name}")
                    except Exception as e:
                        print(f"❌ [Birthday] Ошибка выдачи роли {member}: {e}")
                try:
                    await channel.send(f"🎉 {member.mention}, поздравляем с днём рождения! 🎂")
                    print(f"✅ [Birthday] Поздравление для {member.display_name}")
                except Exception as e:
                    print(f"❌ [Birthday] Ошибка отправки: {e}")

    async def handle_birthday_reset(self):
        channel = self.bot.get_channel(self.channel_id)
        if channel is None:
            return
        guild = channel.guild
        role = guild.get_role(self.role_id) if guild else None
        if role is None:
            return

        for member in guild.members:
            if role in member.roles:
                try:
                    await member.remove_roles(role)
                    print(f"✅ [Birthday] Роль снята с {member.display_name}")
                except Exception as e:
                    print(f"❌ [Birthday] Ошибка снятия роли с {member}: {e}")

    @check_loop.before_loop
    async def before_loop(self):
        await self.bot.wait_until_ready()

    # -------------------- Slash-команды --------------------
    # Используйте ID роли, которой разрешено управлять днями рождения
    ALLOWED_ROLE_ID = 1153753506772164629  # Замените на нужную роль

    @commands.slash_command(name="add_birthday", description="Добавить/обновить день рождения")
    @commands.has_any_role(ALLOWED_ROLE_ID)
    async def add_birthday(
        self,
        inter: disnake.ApplicationCommandInteraction,
        user: disnake.User = commands.Param(description="Пользователь"),
        date: str = commands.Param(description="Дата в формате ДД-ММ-ГГГГ или ДД-ММ")
    ):
        try:
            day, month, year = parse_birthday(date)
        except ValueError as e:
            await inter.response.send_message(f"❌ {e}", ephemeral=True)
            return

        database.add_birthday(str(user.id), day, month, year)
        date_str = f"{day:02d}-{month:02d}"
        if year != 2000:
            date_str += f"-{year}"
        await inter.response.send_message(
            f"✅ День рождения {user.mention} сохранён: {date_str}", ephemeral=True
        )

    @commands.slash_command(name="remove_birthday", description="Удалить день рождения")
    @commands.has_any_role(ALLOWED_ROLE_ID)
    async def remove_birthday(
        self,
        inter: disnake.ApplicationCommandInteraction,
        user: disnake.User = commands.Param(description="Пользователь")
    ):
        database.remove_birthday(str(user.id))
        await inter.response.send_message(f"✅ День рождения {user.mention} удалён", ephemeral=True)

    @commands.slash_command(name="birthday_list", description="Список дней рождений гильдии")
    @commands.has_any_role(ALLOWED_ROLE_ID)
    async def birthday_list(self, inter: disnake.ApplicationCommandInteraction):
        await inter.response.defer()
        all_bdays = database.get_all_birthdays()
        if not all_bdays:
            await inter.edit_original_message("Нет сохранённых дней рождений.")
            return

        today = datetime.now(MSK).date()
        bday_list = []
        for discord_id_str, day, month, year in all_bdays:
            next_bday = next_birthday(day, month, year, today)
            bday_list.append((discord_id_str, day, month, year, next_bday))

        bday_list.sort(key=lambda x: x[4])

        lines = []
        for discord_id_str, day, month, year, next_bday in bday_list:
            member = inter.guild.get_member(int(discord_id_str))
            if member is None:
                continue
            date_str = f"{day:02d}.{month:02d}"
            if year != 2000:
                date_str += f".{year}"
            lines.append(f"• {member.mention} — {date_str}")

        if not lines:
            await inter.edit_original_message("Нет участников с сохранёнными днями рождения.")
            return

        await inter.edit_original_message("🎂 **Дни рождения гильдии:**\n" + "\n".join(lines))


def setup(bot: commands.Bot):
    bot.add_cog(Birthday(bot))