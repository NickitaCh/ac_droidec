import asyncio
import disnake
from disnake.ext import commands, tasks
from datetime import datetime
from zoneinfo import ZoneInfo
from collections import defaultdict
import re
import database
from swgoh_comlink import SwgohComlink
from cogs.violations import autocomplete_players
import tempfile

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

    # ------------------ Мониторинг событий ------------------
    @tasks.loop(minutes=5)
    async def monitor_loop(self):
        await self.bot.wait_until_ready()
        try:
            guild = await asyncio.to_thread(
                self.comlink.get_guild, self.guild_id, include_recent_guild_activity_info=True
            )
        except Exception as e:
            print(f"Ошибка получения данных гильдии: {e}")
            return

        tb_status = guild.get("territoryBattleStatus", [])
        current_tb = tb_status[0] if tb_status else None
        if current_tb and self.last_tb_status and current_tb.get("status") != self.last_tb_status.get("status"):
            if current_tb.get("status") == "completed":
                await self.generate_tb_report(guild)
        self.last_tb_status = current_tb

        tw_status = guild.get("territoryWarStatus", [])
        current_tw = tw_status[0] if tw_status else None
        if current_tw and self.last_tw_status and current_tw.get("status") != self.last_tw_status.get("status"):
            if current_tw.get("status") == "completed":
                await self.generate_tw_report(guild)
        self.last_tw_status = current_tw

    async def generate_tb_report(self, guild):
        result = guild.get("recentTerritoryBattleResult", [])
        if not result:
            await self.notify_officers("ТБ завершена, но отчёт пуст. Используйте ручной ввод.")
            return

        members = guild.get("member", [])
        player_names = {m["playerId"]: m["playerName"] for m in members if "playerId" in m and "playerName" in m}
        stats = self._collect_guild_stats(result, player_names)
        if not stats:
            await self.notify_officers("Нет данных по очкам.")
            return

        report = self._format_stats_table("📊 **Итоги Территориальной Битвы (автоотчёт)**", stats)
        channel = self.bot.get_channel(self.officer_channel_id)
        if channel:
            await self.send_as_file(channel, report, "tb_report.txt")

    async def generate_tw_report(self, guild):
        pass

    async def notify_officers(self, message):
        channel = self.bot.get_channel(self.officer_channel_id)
        if channel:
            await channel.send(f"📢 {message}")

    # ------------------ Общие вспомогательные функции ------------------
    def _collect_guild_stats(self, tb_result, player_names):
        stats = {}
        for zone in tb_result[0].get("finalStat", []):
            for ps in zone.get("playerStat", []):
                member_id = ps.get("memberId")
                if not member_id:
                    continue
                if member_id not in stats:
                    stats[member_id] = {
                        "name": player_names.get(member_id, member_id[:8] + "…"),
                        "score": 0,
                    }
                stats[member_id]["score"] += int(ps.get("score", 0))
        return stats

    def _format_stats_table(self, title, stats):
        lines = [title]
        lines.append("")
        header = f"{'Игрок':<20} {'Очки':>15}"
        lines.append(header)
        lines.append("-" * 35)
        sorted_stats = sorted(stats.items(), key=lambda x: x[1]["score"], reverse=True)
        for member_id, s in sorted_stats:
            name = s["name"][:19]
            lines.append(f"{name:<20} {s['score']:>15,}")
        return "\n".join(lines)

    async def send_as_file(self, channel, content, filename):
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", delete=False, suffix=".txt") as f:
            f.write(content)
            f.flush()
            await channel.send(file=disnake.File(f.name, filename=filename))

    # ------------------ Детальная статистика игрока ------------------
    def parse_player_stats_detailed(self, tb_result, member_id):
        """
        Собирает показатели по фазам 1-6. Сначала ищет специальную зону с ключом 'summary'
        (общая агрегированная статистика), затем дополняет разбивку по раундам.
        Если такой зоны нет – обходит все обычные зоны.
        """
        totals = {
            "summary": 0, "power": 0, "unit_donated": 0,
            "strike_encounter": 0, "strike_attempt": 0,
            "covert_complete": 0, "covert_attempt": 0
        }
        phases = {i: {"points": 0, "power": 0, "deployed": 0,
                      "combat_attempts": 0, "combat_wins": 0,
                      "special_attempts": 0, "special_wins": 0}
                  for i in range(1, 7)}

        # 1) Ищем "общую" зону, где есть ключ 'summary' для данного игрока
        summary_zone = None
        for zone in tb_result[0].get("finalStat", []):
            for ps in zone.get("playerStat", []):
                if ps.get("memberId") == member_id and "summary" in ps:
                    summary_zone = zone
                    break
            if summary_zone:
                break

        if summary_zone:
            # Собираем все данные из этой зоны
            for ps in summary_zone.get("playerStat", []):
                if ps.get("memberId") != member_id:
                    continue
                for key, value in ps.items():
                    if key == "memberId":
                        continue
                    try:
                        val = int(value)
                    except (ValueError, TypeError):
                        continue

                    # Общие тоталы
                    if key == "summary":
                        totals["summary"] += val
                    elif key == "power":
                        totals["power"] += val
                    elif key == "unit_donated":
                        totals["unit_donated"] += val
                    elif key == "strike_encounter":
                        totals["strike_encounter"] += val
                    elif key == "strike_attempt":
                        totals["strike_attempt"] += val
                    elif key == "covert_complete":
                        totals["covert_complete"] += val
                    elif key == "covert_attempt":
                        totals["covert_attempt"] += val

                    # По раундам: summary_round_1, power_round_1 и т.д.
                    round_match = re.search(r'_round_(\d+)$', key)
                    if round_match:
                        round_num = int(round_match.group(1))
                        if 1 <= round_num <= 6:
                            if "summary_round" in key:
                                phases[round_num]["points"] += val
                            elif "power_round" in key:
                                phases[round_num]["power"] += val
                            elif "unit_donated_round" in key:
                                phases[round_num]["deployed"] += val
                            elif "strike_attempt_round" in key:
                                phases[round_num]["combat_attempts"] += val
                            elif "strike_encounter_round" in key:
                                phases[round_num]["combat_wins"] += val
                            elif "covert_attempt_round" in key:
                                phases[round_num]["special_attempts"] += val
                            elif "covert_complete_round" in key:
                                phases[round_num]["special_wins"] += val
        else:
            # Запасной вариант – собираем из обычных зон
            for zone in tb_result[0].get("finalStat", []):
                map_stat = zone.get("mapStatId", "")
                phase_match = re.search(r'phase(\d+)', map_stat)
                phase_num = int(phase_match.group(1)) if phase_match else 0
                if phase_num < 1 or phase_num > 6:
                    continue

                for ps in zone.get("playerStat", []):
                    if ps.get("memberId") != member_id:
                        continue
                    for key, value in ps.items():
                        if key == "memberId":
                            continue
                        try:
                            val = int(value)
                        except (ValueError, TypeError):
                            continue

                        if key == "score":
                            totals["summary"] += val
                        elif key == "power":
                            totals["power"] += val
                        elif key == "unit_donated":
                            totals["unit_donated"] += val
                        elif key == "strike_attempt":
                            totals["strike_attempt"] += val
                        elif key == "strike_encounter":
                            totals["strike_encounter"] += val
                        elif key == "covert_attempt":
                            totals["covert_attempt"] += val
                        elif key == "covert_complete":
                            totals["covert_complete"] += val

                        # По фазе
                        if key == "score":
                            phases[phase_num]["points"] += val
                        elif key == "power":
                            phases[phase_num]["power"] += val
                        elif key == "unit_donated":
                            phases[phase_num]["deployed"] += val
                        elif key == "strike_attempt":
                            phases[phase_num]["combat_attempts"] += val
                        elif key == "strike_encounter":
                            phases[phase_num]["combat_wins"] += val
                        elif key == "covert_attempt":
                            phases[phase_num]["special_attempts"] += val
                        elif key == "covert_complete":
                            phases[phase_num]["special_wins"] += val

        return totals, phases

    def format_player_report(self, player_name, totals, phases):
        """Форматирует детальный отчёт в текстовый файл с общей сводкой и таблицей по фазам."""
        lines = [
            f"Детальная статистика: {player_name}",
            f"Всего очков: {totals['summary']:,}",
            f"Деплой (взводы): {totals['unit_donated']}",
            f"БЗ (усп/поп): {totals['strike_encounter']}/{totals['strike_attempt']}",
            f"ОЗ (усп/поп): {totals['covert_complete']}/{totals['covert_attempt']}",
            f"Мощь отрядов: {totals['power']:,}",
            "",
            "Фаза  Очки           Мощь       Деплой  БЗ(усп/поп)  ОЗ(усп/поп)",
            "-" * 70
        ]
        for phase in range(1, 7):
            p = phases.get(phase, {})
            points = p.get("points", 0)
            power = p.get("power", 0)
            deployed = p.get("deployed", 0)
            cb_att = p.get("combat_attempts", 0)
            cb_win = p.get("combat_wins", 0)
            sp_att = p.get("special_attempts", 0)
            sp_win = p.get("special_wins", 0)
            bz_str = f"{cb_win}/{cb_att}" if cb_att > 0 else "-"
            oz_str = f"{sp_win}/{sp_att}" if sp_att > 0 else "-"
            lines.append(
                f"{phase:4}  {points:>13,}  {power:>10,}  {deployed:>6}  {bz_str:>11}  {oz_str:>11}"
            )
        return "\n".join(lines)

    # ------------------ Slash-команды ------------------
    @commands.slash_command(name="tb_report", description="Управление отчётами по ТБ")
    @commands.has_any_role(1153753506772164629)
    async def tb_report(self, inter: disnake.ApplicationCommandInteraction):
        pass

    @tb_report.sub_command(name="add", description="Добавить результат игрока в ручной отчёт")
    async def tb_add(self, inter, user: disnake.User, score: int):
        database.add_manual_score("tb", str(user.id), score)
        await inter.response.send_message(f"✅ {user.mention} добавлен с {score} очков", ephemeral=True)

    @tb_report.sub_command(name="post", description="Опубликовать ручной отчёт")
    async def tb_post(self, inter: disnake.ApplicationCommandInteraction):
        rows = database.get_manual_scores("tb")
        if not rows:
            await inter.response.send_message("Нет данных для ручного отчёта.", ephemeral=True)
            return
        report = "📊 **Итоги ТБ (ручной ввод):**\n"
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

    @tb_report.sub_command(name="last", description="Сводка последней завершённой ТБ")
    async def tb_last(self, inter: disnake.ApplicationCommandInteraction):
        await inter.response.defer()
        try:
            guild = await asyncio.wait_for(
                asyncio.to_thread(self.comlink.get_guild, self.guild_id, include_recent_guild_activity_info=True),
                timeout=15.0
            )
        except asyncio.TimeoutError:
            await inter.edit_original_message("⏰ Запрос к Comlink занял слишком много времени.")
            return
        except Exception as e:
            await inter.edit_original_message(f"Ошибка получения данных: {e}")
            return

        result = guild.get("recentTerritoryBattleResult", [])
        if not result:
            await inter.edit_original_message("Нет данных о последней ТБ.")
            return

        members = guild.get("member", [])
        player_names = {m["playerId"]: m["playerName"] for m in members if "playerId" in m and "playerName" in m}
        stats = self._collect_guild_stats(result, player_names)
        if not stats:
            await inter.edit_original_message("Нет очков в последней ТБ.")
            return

        report = self._format_stats_table("📊 **Итоги последней ТБ (автоотчёт)**", stats)
        await self.send_as_file(inter.channel, report, "tb_report.txt")
        await inter.edit_original_message("Отчёт отправлен файлом.")

    @tb_report.sub_command(name="player", description="Полная детальная статистика игрока за ТБ")
    async def tb_player(
        self,
        inter: disnake.ApplicationCommandInteraction,
        name: str = commands.Param(description="Выберите игрока", autocomplete=autocomplete_players)
    ):
        await inter.response.defer()

        cache = self.bot.guild_roster_cache
        if not cache or name not in cache:
            await inter.edit_original_message("Ошибка: игрок не найден в кэше состава.")
            return
        allycode = cache[name]

        try:
            player = await asyncio.to_thread(self.comlink.get_player, allycode=allycode)
            player_id = player.get("playerId")
            if not player_id:
                await inter.edit_original_message("Не удалось определить игровой ID.")
                return

            guild = await asyncio.wait_for(
                asyncio.to_thread(self.comlink.get_guild, self.guild_id, include_recent_guild_activity_info=True),
                timeout=15.0
            )
            result = guild.get("recentTerritoryBattleResult", [])
            if not result:
                await inter.edit_original_message("Нет данных о последней ТБ.")
                return

            totals, phases = self.parse_player_stats_detailed(result, player_id)
            if totals["summary"] == 0:
                await inter.edit_original_message(f"{name} не участвовал в последней ТБ.")
                return

            report = self.format_player_report(name, totals, phases)
            await self.send_as_file(inter.channel, report, f"tb_detail_{name}.txt")
            embed = disnake.Embed(title=f"📊 Детальная статистика: {name}", color=0x3498db)
            embed.add_field(name="Общий счёт", value=f"{totals['summary']:,}", inline=False)
            embed.set_footer(text="Полный отчёт отправлен файлом.")
            await inter.edit_original_message(embed=embed)

        except asyncio.TimeoutError:
            await inter.edit_original_message("⏰ Запрос к Comlink занял слишком много времени.")
        except Exception as e:
            await inter.edit_original_message(f"Ошибка: {e}")

    @tb_report.sub_command(name="sync_members", description="Привязать Discord-пользователей к игровым аккаунтам")
    async def tb_sync_members(self, inter: disnake.ApplicationCommandInteraction):
        await inter.response.defer(ephemeral=True)
        try:
            guild = await asyncio.to_thread(self.comlink.get_guild, self.guild_id)
        except Exception as e:
            await inter.edit_original_message(f"Ошибка получения гильдии: {e}")
            return

        discord_guild = inter.guild
        if not discord_guild:
            await inter.edit_original_message("Команда доступна только на сервере.")
            return

        members = guild.get("member", [])
        linked = 0
        not_found = []
        for m in members:
            player_id = m.get("playerId")
            player_name = m.get("playerName")
            if not player_id or not player_name:
                continue
            try:
                player = await asyncio.to_thread(self.comlink.get_player, player_id=player_id)
                allycode = player.get("allyCode")
                if not allycode:
                    continue
            except Exception:
                continue

            normalized = re.sub(r'\W+', '', player_name).lower()
            member_found = None
            for discord_member in discord_guild.members:
                if re.sub(r'\W+', '', discord_member.display_name).lower() == normalized:
                    member_found = discord_member
                    break
            if not member_found:
                not_found.append(player_name)
                continue

            database.set_user_mapping(str(member_found.id), str(allycode), player_name)
            linked += 1

        msg = f"✅ Привязано: {linked} игроков."
        if not_found:
            msg += f"\nНе найдены на сервере: {', '.join(not_found[:10])}"
        await inter.edit_original_message(msg)

    @tb_report.sub_command(name="clear", description="Очистить ручные записи для ТБ")
    async def tb_clear(self, inter: disnake.ApplicationCommandInteraction):
        database.clear_manual_scores("tb")
        await inter.response.send_message("Записи очищены.", ephemeral=True)


def setup(bot: commands.Bot):
    bot.add_cog(GuildEvents(bot))