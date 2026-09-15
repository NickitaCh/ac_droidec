import json
import disnake
from disnake.ext import commands, tasks
from datetime import datetime
from zoneinfo import ZoneInfo

import database
from services import feature_flags, tb_schedule

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
    def _is_ping_week(now_msk_date) -> bool:
        # Якорь чётности недели общий для всех гильдий (services/tb_schedule.py) —
        # это факт игрового календаря ТБ, а не настройка одной гильдии.
        delta = (now_msk_date - tb_schedule.get_week_anchor_date()).days
        week_number = delta // 7
        return week_number % 2 == 0

    # Без интеракции резолвить гильдию по роли нечем — идём по всем зарегистрированным
    # гильдиям на каждом тике (как update_roster_cache в violations.py), у каждой
    # своё расписание/канал/роль из guilds; чётность недели и DST-поправка общие.
    @tasks.loop(seconds=30)
    async def rotation_ping_loop(self):
        now_msk = datetime.now(MSK)
        today_msk = now_msk.date()
        weekday = now_msk.weekday()
        hour = now_msk.hour
        minute = now_msk.minute
        dst_correction = tb_schedule.effective_dst_offset_minutes(today_msk)

        if not self._is_ping_week(today_msk):
            return

        for guild_cfg in database.get_all_guild_configs():
            gid = guild_cfg["id"]
            gname = guild_cfg["name"]
            if not guild_cfg.get("ping_channel_id") or not guild_cfg.get("ping_role_id") or \
                    not guild_cfg.get("ping_schedule_json"):
                continue
            if not feature_flags.is_enabled(gid, "tb_ping"):
                continue

            try:
                schedule = json.loads(guild_cfg["ping_schedule_json"])
            except (ValueError, TypeError, json.JSONDecodeError) as e:
                print(f"❌ [RotationPing] [{gname}] Некорректный конфиг расписания: {e}")
                continue

            for entry in schedule:
                raw_h, raw_m = map(int, entry["time"].split(":"))
                h, m = tb_schedule.apply_correction(raw_h, raw_m, dst_correction)
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
