"""Фоновый цикл скана моддинга чужих гильдий — вся логика в services/mod_scan.py, здесь
только disnake tasks.loop. Без слэш-команд: цели (1-5 гильд ID/аликодов) добавляются и
списки (минорные изменения/аномалии) смотрятся на веб-странице /mod-scan (см. запрос
пользователя, чат 2026-09-15: "на отдельной страничке"), не в Discord."""

import asyncio

from disnake.ext import commands, tasks

import database
from services import mod_scan

SCAN_INTERVAL_MINUTES = 5
EVENT_RETENTION_DAYS = 14
# Пауза между самими гильдиями-целями (не между игроками одной цели — та пауза внутри
# services/mod_scan.py::scan_target) — до 5 целей, не нужно гнать их одну за другой без
# зазора.
BETWEEN_TARGETS_SLEEP = 1.0


class ModScan(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.mod_scan_loop.start()

    def cog_unload(self):
        self.mod_scan_loop.cancel()

    @tasks.loop(minutes=SCAN_INTERVAL_MINUTES)
    async def mod_scan_loop(self):
        targets = database.get_mod_scan_targets(owner_guild_id=None)
        if not targets:
            return
        for target in targets:
            try:
                await mod_scan.scan_target(self.bot.comlink, target)
            except Exception as e:
                print(f"⚠️ [МодСкан] Цель {target['id']} ({target.get('guild_name')}): {e}")
            await asyncio.sleep(BETWEEN_TARGETS_SLEEP)
        database.prune_mod_scan_events(days=EVENT_RETENTION_DAYS)

    @mod_scan_loop.before_loop
    async def _before_mod_scan_loop(self):
        await self.bot.wait_until_ready()


def setup(bot: commands.Bot):
    bot.add_cog(ModScan(bot))
