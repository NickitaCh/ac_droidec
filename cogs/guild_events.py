import disnake
from disnake.ext import commands, tasks
from datetime import datetime
from zoneinfo import ZoneInfo
import database
from swgoh_comlink import SwgohComlink
from collections import defaultdict

MSK = ZoneInfo("Europe/Moscow")

class GuildEvents(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.comlink = SwgohComlink(url="http://localhost:3000")
        self.guild_id = "-kJhCaGGQqGOjgbWpJFEIg"
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
        try:
            guild = self.comlink.get_guild(self.guild_id, include_recent_guild_activity_info=True)
        except Exception as e:
            print(f"Ошибка получения данных гильдии: {e}")
            return

        # Проверяем ТБ
        tb_status = guild.get("territoryBattleStatus", [])
        current_tb = tb_status[0] if tb_status else None
        if current_tb and self.last_tb_status and current_tb.get("status") != self.last_tb_status.get("status"):
            if current_tb.get("status") == "completed":
                await self.generate_tb_report(guild)
        self.last_tb_status = current_tb

        # Проверяем ТВ (пока заглушка)
        tw_status = guild.get("territoryWarStatus", [])
        current_tw = tw_status[0] if tw_status else None
        if current_tw and self.last_tw_status and current_tw.get("status") != self.last_tw_status.get("status"):
            if current_tw.get("status") == "completed":
                await self.generate_tw_report(guild)
        self.last_tw_status = current_tw

    async def generate_tb_report(self, guild):
        """Создаёт автоматический отчёт по завершённой ТБ и отправляет в офицерский канал."""
        result = guild.get("recentTerritoryBattleResult", [])
        if not result:
            await self.notify_officers(
                "TB_REPORT_NEEDED",
                "Территориальная Битва завершена, но автоматический отчёт не сформирован. "
                "Введите данные вручную через `/tb_report add`."
            )
            return

        # Собираем имена игроков из гильдии (playerId -> playerName)
        members = guild.get("member", [])
        player_names = {m["playerId"]: m["playerName"] for m in members if "playerId" in m and "playerName" in m}

        # Агрегируем очки по всем зонам
        total_scores = defaultdict(int)
        for zone in result[0].get("finalStat", []):
            for ps in zone.get("playerStat", []):
                member_id = ps.get("memberId")
                if member_id:
                    total_scores[member_id] += int(ps.get("score", 0))

        # Формируем отчёт
        if not total_scores:
            await self.notify_officers("TB_REPORT_EMPTY", "Нет данных по очкам игроков.")
            return

        sorted_scores = sorted(total_scores.items(), key=lambda x: x[1], reverse=True)
        lines = ["📊 **Итоги Территориальной Битвы (автоматический отчёт)**"]
        for member_id, score in sorted_scores:
            name = player_names.get(member_id, member_id[:8] + "…")
            lines.append(f"{name}: {score} очков")

        report = "\n".join(lines)
        channel = self.bot.get_channel(self.officer_channel_id)
        if channel:
            await channel.send(report)
        else:
            print("Офицерский канал не найден")

    async def generate_tw_report(self, guild):
        """Аналогично для ТВ (пока заглушка)."""
        result = guild.get("recentTerritoryWarResult", [])
        if not result:
            await self.notify_officers(
                "TW_REPORT_NEEDED",
                "Территориальная Война завершена, но автоматический отчёт не сформирован. "
                "Введите данные вручную через `/tw_report add`."
            )
            return
        # TODO: реализовать парсинг результатов TW
        report = "Автоматический отчёт по ТВ пока не реализован."
        channel = self.bot.get_channel(self.officer_channel_id)
        if channel:
            await channel.send(report)

    async def notify_officers(self, event_type, message):
        channel = self.bot.get_channel(self.officer_channel_id)
        if channel:
            await channel.send(f"📢 {message}")

    # ------------------ Slash-команды ------------------
    @commands.slash_command(name="tb_report", description="Управление отчётами по ТБ")
    @commands.has_any_role(1153753506772164629)
    async def tb_report(self, inter: disnake.ApplicationCommandInteraction):
        pass

    @tb_report.sub_command(name="add", description="Добавить результат игрока в ручной отчёт по ТБ")
    async def tb_add(
        self,
        inter: disnake.ApplicationCommandInteraction,
        user: disnake.User,
        score: int = commands.Param(description="Количество очков/миссий")
    ):
        database.add_manual_score("tb", str(user.id), score)
        await inter.response.send_message(f"✅ {user.mention} добавлен с результатом {score}", ephemeral=True)

    @tb_report.sub_command(name="post", description="Опубликовать итоговый ручной отчёт по ТБ")
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
        else:
            await inter.response.send_message("Офицерский канал не найден.", ephemeral=True)

    @tb_report.sub_command(name="last", description="Показать автоматическую сводку за последнюю завершённую ТБ")
    async def tb_last(self, inter: disnake.ApplicationCommandInteraction):
        await inter.response.defer()
        try:
            guild = self.comlink.get_guild(self.guild_id, include_recent_guild_activity_info=True)
        except Exception as e:
            await inter.edit_original_message(f"Ошибка получения данных гильдии: {e}")
            return

        result = guild.get("recentTerritoryBattleResult", [])
        if not result:
            await inter.edit_original_message("Нет данных о последней ТБ.")
            return

        members = guild.get("member", [])
        player_names = {m["playerId"]: m["playerName"] for m in members if "playerId" in m and "playerName" in m}

        total_scores = defaultdict(int)
        for zone in result[0].get("finalStat", []):
            for ps in zone.get("playerStat", []):
                member_id = ps.get("memberId")
                if member_id:
                    total_scores[member_id] += int(ps.get("score", 0))

        if not total_scores:
            await inter.edit_original_message("Нет очков в последней ТБ.")
            return

        sorted_scores = sorted(total_scores.items(), key=lambda x: x[1], reverse=True)
        lines = ["📊 **Итоги последней ТБ (автоматический отчёт)**"]
        for member_id, score in sorted_scores:
            name = player_names.get(member_id, member_id[:8] + "…")
            lines.append(f"{name}: {score} очков")

        report = "\n".join(lines)
        await inter.edit_original_message(report)

    @tb_report.sub_command(name="clear", description="Очистить все ручные записи для ТБ")
    async def tb_clear(self, inter: disnake.ApplicationCommandInteraction):
        database.clear_manual_scores("tb")
        await inter.response.send_message("Записи очищены.", ephemeral=True)


def setup(bot: commands.Bot):
    bot.add_cog(GuildEvents(bot))