"""Фоновое обслуживание подписок гильдий. Команды /гильдия и /админы убраны из Discord
2026-10-14 — управление гильдиями, подписками, супер-админами и грантами только на сайте
в «СуперАдмин меню» (/admin/guilds, /admin/access; логика — services/guild_admin.py)."""

from disnake.ext import commands, tasks

import database


class AdminManagementCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.subscription_expiry_loop.start()

    def cog_unload(self):
        self.subscription_expiry_loop.cancel()

    # Раз в сутки гасит is_active у гильдий с истёкшей платной подпиской
    # (database.get_expired_active_guilds — только subscription_expires_at
    # IS NOT NULL, бессрочные ручные гильдии сюда не попадают). Доступ у их
    # участников отваливается сразу же на следующем вызове команды —
    # guild_resolver.resolve_access читает БД каждый раз, без кэша.
    @tasks.loop(hours=24)
    async def subscription_expiry_loop(self):
        for g in database.get_expired_active_guilds():
            database.update_guild_config(g["id"], is_active=0)
            print(f"⏳ Подписка гильдии «{g['name']}» (id={g['id']}) истекла — доступ отключён.")

    @subscription_expiry_loop.before_loop
    async def before_subscription_expiry_loop(self):
        await self.bot.wait_until_ready()


def setup(bot):
    bot.add_cog(AdminManagementCog(bot))
