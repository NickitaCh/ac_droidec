import json
import disnake
from disnake.ext import commands, tasks
from datetime import datetime
from zoneinfo import ZoneInfo

import database

MSK = ZoneInfo("Europe/Moscow")

class RotationPing(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        # Защита от дублирования: (guild_id, текст) -> последняя минута отправки
        self.last_sent_minutes = {}
        self.rotation_ping_loop.start()

    def cog_unload(self):
        self.rotation_ping_loop.cancel()

    @staticmethod
    def _is_ping_week(start_date, now_msk_date) -> bool:
        delta = (now_msk_date - start_date).days
        week_number = delta // 7
        return week_number % 2 == 0

    # Без интеракции резолвить гильдию по роли нечем — идём по всем зарегистрированным
    # гильдиям на каждом тике (как update_roster_cache в violations.py), у каждой
    # своё расписание/канал/роль/дата старта из guilds.
    @tasks.loop(seconds=30)
    async def rotation_ping_loop(self):
        now_msk = datetime.now(MSK)
        today_msk = now_msk.date()
        weekday = now_msk.weekday()
        hour = now_msk.hour
        minute = now_msk.minute

        for guild_cfg in database.get_all_guild_configs():
            gid = guild_cfg["id"]
            gname = guild_cfg["name"]
            if not guild_cfg.get("ping_channel_id") or not guild_cfg.get("ping_role_id") or \
                    not guild_cfg.get("ping_start_date") or not guild_cfg.get("ping_schedule_json"):
                continue

            try:
                start_date = datetime.strptime(guild_cfg["ping_start_date"], "%Y-%m-%d").date()
                schedule = json.loads(guild_cfg["ping_schedule_json"])
            except (ValueError, TypeError, json.JSONDecodeError) as e:
                print(f"❌ [RotationPing] [{gname}] Некорректный конфиг расписания: {e}")
                continue

            if not self._is_ping_week(start_date, today_msk):
                continue

            for entry in schedule:
                h, m = map(int, entry["time"].split(":"))
                if weekday not in entry["days"]:
                    continue
                if hour != h or minute != m:
                    continue

                text = entry["text"]
                key = (gid, text)
                current_minute_key = f"{now_msk.strftime('%Y%m%d%H%M')}_{text}"
                if current_minute_key == self.last_sent_minutes.get(key):
                    continue  # уже отправляли в эту минуту, пропускаем

                channel = self.bot.get_channel(int(guild_cfg["ping_channel_id"]))
                if channel is None:
                    print(f"❌ [RotationPing] [{gname}] Канал {guild_cfg['ping_channel_id']} не найден")
                    continue

                role = channel.guild.get_role(int(guild_cfg["ping_role_id"])) if channel.guild else None
                if role is None:
                    print(f"❌ [RotationPing] [{gname}] Роль {guild_cfg['ping_role_id']} не найдена")
                    continue

                try:
                    await channel.send(f"{role.mention} {text}")
                    print(f"✅ [RotationPing] [{gname}] Тег '{text}' отправлен в {now_msk.strftime('%Y-%m-%d %H:%M')} МСК")
                    self.last_sent_minutes[key] = current_minute_key
                except Exception as e:
                    print(f"❌ [RotationPing] [{gname}] Ошибка отправки: {e}")

    @rotation_ping_loop.before_loop
    async def before_loop(self):
        await self.bot.wait_until_ready()


def setup(bot: commands.Bot):
    bot.add_cog(RotationPing(bot))
