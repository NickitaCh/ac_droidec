import disnake
from disnake.ext import commands, tasks
from datetime import datetime
from zoneinfo import ZoneInfo
import asyncio

MSK = ZoneInfo("Europe/Moscow")   # московское время (UTC+3)

class RotationPing(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.channel_id = bot.PING_CHANNEL_ID
        self.role_id = bot.PING_ROLE_ID
        self.start_date_str = bot.PING_START_DATE
        self.schedule = bot.PING_SCHEDULE

        # Дата старта (понедельник) — считаем, что она задана по Москве
        self.start_date = datetime.strptime(self.start_date_str, "%Y-%m-%d").date()

        # Преобразуем время в расписании в (час, минута)
        self.parsed_schedule = []
        for entry in self.schedule:
            h, m = map(int, entry["time"].split(":"))
            self.parsed_schedule.append({
                "hour": h,
                "minute": m,
                "text": entry["text"],
                "days": entry["days"]
            })

        self.rotation_ping_loop.start()

    def cog_unload(self):
        self.rotation_ping_loop.cancel()

    def is_ping_week(self, now_msk_date) -> bool:
        """Проверяем, тегаемая ли сейчас неделя (неделя через две).
           now_msk_date – московская дата (date)"""
        delta = (now_msk_date - self.start_date).days
        week_number = delta // 7
        return week_number % 2 == 0

    @tasks.loop(seconds=30)
    async def rotation_ping_loop(self):
        # Получаем текущее время в Москве
        now_msk = datetime.now(MSK)
        today_msk = now_msk.date()
        weekday = now_msk.weekday()          # 0=пн, 6=вс
        hour = now_msk.hour
        minute = now_msk.minute

        # Проверяем, правильная ли неделя
        if not self.is_ping_week(today_msk):
            return

        # Проверяем все элементы расписания
        for entry in self.parsed_schedule:
            if weekday not in entry["days"]:
                continue
            if hour != entry["hour"] or minute != entry["minute"]:
                continue

            # Отправляем тег
            channel = self.bot.get_channel(self.channel_id)
            if channel is None:
                print(f"❌ [RotationPing] Канал {self.channel_id} не найден")
                continue

            role = channel.guild.get_role(self.role_id) if channel.guild else None
            if role is None:
                print(f"❌ [RotationPing] Роль {self.role_id} не найдена")
                continue

            try:
                await channel.send(f"{role.mention} {entry['text']}")
                print(f"✅ [RotationPing] Тег '{entry['text']}' отправлен в {now_msk.strftime('%Y-%m-%d %H:%M')} МСК")
            except Exception as e:
                print(f"❌ [RotationPing] Ошибка отправки: {e}")

    @rotation_ping_loop.before_loop
    async def before_loop(self):
        await self.bot.wait_until_ready()


def setup(bot: commands.Bot):
    bot.add_cog(RotationPing(bot))