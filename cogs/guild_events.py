import disnake
from disnake.ext import commands, tasks
from datetime import datetime
from zoneinfo import ZoneInfo
import database
from swgoh_comlink import SwgohComlink

MSK = ZoneInfo("Europe/Moscow")

class GuildEvents(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.comlink = SwgohComlink(url="http://localhost:3000")
        self.guild_id = "-kJhCaGGQqGOjgbWpJFEIg"  # ваш ID гильдии
        self.officer_channel_id = bot.OFFICER_CHANNEL_ID
        self.allowed_role_id = bot.ALLOWED_OFFICER_ROLE_ID
        self.last_tb_status = None
        self.last_tw_status = None
        self.monitor_loop.start()

    def cog_unload(self):
        self.monitor_loop.cancel()

    @tasks.loop(minutes=5)
    async def monitor_loop(self):
        await self.bot.wait_until_ready()
        guild = self.comlink.get_guild(self.guild_id)
        # Проверяем ТБ
        tb_status = guild.get("territoryBattleStatus", [])
        current_tb = tb_status[0] if tb_status else None
        if current_tb and self.last_tb_status and current_tb.get("status") != self.last_tb_status.get("status"):
            if current_tb.get("status") == "completed":
                await self.generate_tb_report(guild)
        self.last_tb_status = current_tb

        # Проверяем ТВ
        tw_status = guild.get("territoryWarStatus", [])
        current_tw = tw_status[0] if tw_status else None
        if current_tw and self.last_tw_status and current_tw.get("status") != self.last_tw_status.get("status"):
            if current_tw.get("status") == "completed":
                await self.generate_tw_report(guild)
        self.last_tw_status = current_tw

    async def generate_tb_report(self, guild):
        """Автоматическая попытка создать отчёт по ТБ"""
        result = guild.get("recentTerritoryBattleResult", [])
        if not result:
            await self.notify_officers("TB_REPORT_NEEDED", "Территориальная Битва завершена, но автоматический отчёт не сформирован. Пожалуйста, введите данные вручную через `/tb_report add`.")
            return
        # Если данные есть – парсим и отправляем
        report = self.parse_tb_result(result[0])
        channel = self.bot.get_channel(self.officer_channel_id)
        if channel:
            await channel.send(report)

    async def generate_tw_report(self, guild):
        """Аналогично для ТВ"""
        result = guild.get("recentTerritoryWarResult", [])
        if not result:
            await self.notify_officers("TW_REPORT_NEEDED", "Территориальная Война завершена, но автоматический отчёт не сформирован. Пожалуйста, введите данные вручную через `/tw_report add`.")
            return
        report = self.parse_tw_result(result[0])
        channel = self.bot.get_channel(self.officer_channel_id)
        if channel:
            await channel.send(report)

    def parse_tb_result(self, result_data):
        """Парсинг результата ТБ (будет дополнен после получения реальной структуры)"""
        return "```\nАвтоматический отчёт по ТБ пока не реализован.\n```"

    def parse_tw_result(self, result_data):
        return "```\nАвтоматический отчёт по ТВ пока не реализован.\n```"

    async def notify_officers(self, event_type, message):
        channel = self.bot.get_channel(self.officer_channel_id)
        if channel:
            await channel.send(f"📢 {message}")

    # ------------------ Slash-команды ручного ввода ------------------
    @commands.slash_command(name="tb_report", description="Управление ручным отчётом по ТБ")
    @commands.has_any_role(1153753506772164629)  # замените на вашу роль
    async def tb_report(self, inter: disnake.ApplicationCommandInteraction):
        pass

    @tb_report.sub_command(name="add", description="Добавить результат игрока в отчёт по ТБ")
    async def tb_add(
        self,
        inter: disnake.ApplicationCommandInteraction,
        user: disnake.User,
        score: int = commands.Param(description="Количество очков/миссий")
    ):
        database.add_manual_score("tb", str(user.id), score)
        await inter.response.send_message(f"✅ {user.mention} добавлен с результатом {score}", ephemeral=True)

    @tb_report.sub_command(name="post", description="Опубликовать итоговый отчёт по ТБ")
    async def tb_post(self, inter: disnake.ApplicationCommandInteraction):
        rows = database.get_manual_scores("tb")
        if not rows:
            await inter.response.send_message("Нет данных для отчёта.", ephemeral=True)
            return
        report = "📊 **Итоги Территориальной Битвы (ручной ввод):**\n"
        for disc_id, score in rows:
            member = inter.guild.get_member(int(disc_id))
            name = member.display_name if member else disc_id
            report += f"{name}: {score} очков\n"
        channel = self.bot.get_channel(self.officer_channel_id)
        if channel:
            await channel.send(report)
            database.clear_manual_scores("tb")
            await inter.response.send_message("Отчёт опубликован.", ephemeral=True)

    @tb_report.sub_command(name="clear", description="Очистить все ручные записи для ТБ")
    async def tb_clear(self, inter: disnake.ApplicationCommandInteraction):
        database.clear_manual_scores("tb")
        await inter.response.send_message("Записи очищены.", ephemeral=True)

    # Аналогичные команды для ТВ (tw_report) можно добавить по такому же шаблону

def setup(bot: commands.Bot):
    bot.add_cog(GuildEvents(bot))