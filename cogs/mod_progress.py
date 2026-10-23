"""«Прогресс модинга» — фоновая часть (страницы — web/routes/mod_progress.py, логика —
services/mod_progress.py): раз в час отмечает изменения плейтов у трекеров (новая версия
с датой) и делает автоснимки, у которых подошёл срок. В Discord ничего не пишет (решение
пользователя 2026-10-23)."""

import traceback

import database
from disnake.ext import commands, tasks
from services import mod_progress


class ModProgressCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.mod_progress_loop.start()

    def cog_unload(self):
        self.mod_progress_loop.cancel()

    @tasks.loop(minutes=60)
    async def mod_progress_loop(self):
        # disnake tasks.loop умирает от необработанного исключения — ловим всё.
        try:
            guild_ids = {cfg["id"] for cfg in database.get_all_guild_configs()}
            for tracker_id in mod_progress.all_tracker_ids(guild_ids):
                mod_progress.sync_version(tracker_id)
            if getattr(self.bot, "stat_calc", None) is None:
                return  # калькулятор статов ещё строится — снимем в следующий час
            for tracker_id in mod_progress.due_tracker_ids(guild_ids):
                try:
                    await mod_progress.take_snapshot(self.bot, tracker_id, "auto")
                except Exception:
                    print(f"⚠️ [mod_progress] автоснимок трекера {tracker_id} не удался")
                    traceback.print_exc()
        except Exception:
            traceback.print_exc()

    @mod_progress_loop.before_loop
    async def before_mod_progress_loop(self):
        await self.bot.wait_until_ready()


def setup(bot):
    bot.add_cog(ModProgressCog(bot))
