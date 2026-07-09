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

# Словари для перевода
ZONE_NAMES_RU = {
    "conflict": "Конфликт",
    "bonus": "Бонус",
    "covert": "Спецмиссия",
    "power": "Сила",
}
SIDE_MAP = {
    "light": "Свет",
    "dark": "Тьма",
    "mixed": "Смешанная",
}

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
    def _extract_zone_name_ru(self, map_stat_id):
        """Возвращает русское название планеты и сторону (Свет/Тьма/Смешанная)."""
        parts = map_stat_id.split("_")
        side = None
        for part in parts:
            if part in SIDE_MAP:
                side = SIDE_MAP[part]
                break
        # Определяем тип планеты (conflict, bonus, covert) и номер
        for part in parts:
            if part in ZONE_NAMES_RU:
                idx = parts.index(part)
                num = parts[idx+1] if idx+1 < len(parts) else ""
                base = ZONE_NAMES_RU[part]
                name = f"{base} {num}" if num else base
                if side:
                    name += f" ({side})"
                return name
        # Если не нашли, возвращаем последний кусок
        fallback = parts[-1] if parts else map_stat_id
        if side:
            fallback += f" ({side})"
        return fallback

    def _get_zone_base_id(self, map_stat_id):
        """Извлекает уникальный идентификатор планеты без суффикса показателя.
        Например, 'summary_zone_tb3_mixed_phase03_conflict01' -> 'tb3_mixed_phase03_conflict01'."""
        # Убираем префиксы показателей
        for prefix in ["summary_zone_", "power_zone_", "strike_encounter_zone_", "strike_attempt_zone_",
                       "unit_donated_zone_", "covert_complete_mission_", "covert_attempt_mission_",
                       "covert_round_attempted_mission_", "power_", "summary_"]:
            if map_stat_id.startswith(prefix):
                return map_stat_id[len(prefix):]
        return map_stat_id

    def parse_player_stats_detailed(self, tb_result, member_id):
        """
        Собирает полные показатели по каждой планете для одного игрока.
        Группирует все подзоны (очки, мощь, БМ, деплой, СМ) по базовому идентификатору планеты.
        Возвращает (total_score, planet_list).
        """
        total_score = 0
        planet_data = defaultdict(lambda: {
            "score": 0, "power": 0,
            "strike_encounter": 0, "strike_attempt": 0,
            "unit_donated": 0,
            "covert_complete": 0, "covert_attempt": 0,
            "phase": 0, "side": "", "name": ""
        })

        for zone in tb_result[0].get("finalStat", []):
            map_stat = zone.get("mapStatId", "неизвестная_зона")
            # Определяем базовый идентификатор планеты (без префикса показателя)
            base_id = self._get_zone_base_id(map_stat)
            # Извлекаем фазу
            phase_match = re.search(r'phase(\d+)', base_id)
            phase_num = int(phase_match.group(1)) if phase_match else 0
            if phase_num == 0:   # пропускаем общие итоги
                continue

            # Название планеты получим позже, сохраним пока базовый id
            planet = planet_data[base_id]
            planet["phase"] = phase_num
            # Определяем сторону (light/dark/mixed)
            if "light" in base_id:
                planet["side"] = "Свет"
            elif "dark" in base_id:
                planet["side"] = "Тьма"
            elif "mixed" in base_id:
                planet["side"] = "Смешанная"
            else:
                planet["side"] = "Неизв."
            # Имя планеты сформируем позже на основе base_id

            # Собираем все значения для этого игрока в данной зоне
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
                    # Сопоставляем ключи с нашими показателями
                    if key == "score":
                        planet["score"] += val
                        total_score += val
                    elif key == "power":
                        planet["power"] += val
                    elif key == "strike_encounter":
                        planet["strike_encounter"] += val
                    elif key == "strike_attempt":
                        planet["strike_attempt"] += val
                    elif key == "unit_donated":
                        planet["unit_donated"] += val
                    elif key == "covert_complete":
                        planet["covert_complete"] += val
                    elif key == "covert_attempt":
                        planet["covert_attempt"] += val
                    # Остальные ключи (например, summary_zone_...) уже обработаны как score
                    # но если встретится что-то специфичное, можно добавить

        # Формируем финальный список планет
        planets = []
        for base_id, p in planet_data.items():
            # Генерируем читаемое имя планеты, используя base_id
            # Пытаемся вытащить тип и номер
            name = self._extract_zone_name_ru(base_id)
            p["name"] = name
            # Если сторона не определилась, можно оставить как есть
            planets.append(p)

        # Сортируем по фазе, затем по имени
        planets.sort(key=lambda x: (x["phase"], x["name"]))
        return total_score, planets

    def format_detailed_report(self, player_name, total_score, planets):
        """Форматирует подробный отчёт в текстовый файл с группировкой по фазам."""
        lines = [f"Детальная статистика: {player_name}"]
        lines.append(f"Общий счёт: {total_score:,}")
        lines.append("")

        by_phase = defaultdict(list)
        for p in planets:
            by_phase[p["phase"]].append(p)

        for phase_num in sorted(by_phase):
            lines.append(f"Фаза {phase_num}")
            lines.append("-" * 100)
            # Заголовок таблицы
            header = f"{'Планета':<30} {'Очки':>15} {'Мощь':>12} {'БЗ(усп/поп)':>12} {'Деплой':>7} {'ОЗ(усп/поп)':>12}"
            lines.append(header)
            lines.append("-" * 100)
            for p in by_phase[phase_num]:
                bz = f"{p['strike_encounter']}/{p['strike_attempt']}" if p['strike_attempt'] else "-"
                oz = f"{p['covert_complete']}/{p['covert_attempt']}" if p['covert_attempt'] else "-"
                lines.append(
                    f"{p['name']:<30} {p['score']:>15,} {p['power']:>12,} {bz:>12} {p['unit_donated']:>7} {oz:>12}"
                )
            lines.append("")
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

            total_score, planets = self.parse_player_stats_detailed(result, player_id)
            if total_score == 0:
                await inter.edit_original_message(f"{name} не участвовал в последней ТБ.")
                return

            report = self.format_detailed_report(name, total_score, planets)
            await self.send_as_file(inter.channel, report, f"tb_detail_{name}.txt")
            embed = disnake.Embed(title=f"📊 Детальная статистика: {name}", color=0x3498db)
            embed.add_field(name="Общий счёт", value=f"{total_score:,}", inline=False)
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