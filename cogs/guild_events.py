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

# Сопоставление сторон
SIDE_MAP = {"light": "Свет", "dark": "Тьма", "mixed": "Смешанная"}

# Типы планет
PLANET_TYPE = {
    "conflict": "Конфликт",
    "bonus": "Бонус",
    "covert": "Спецмиссия",
    "power": "Мощь",
    "summary": "Очки"
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
    def _get_metric_and_base(self, map_stat_id):
        """
        Возвращает (metric, base_planet_id).
        metric: 'summary', 'power', 'strike_encounter', 'strike_attempt',
                'unit_donated', 'covert_complete', 'covert_attempt', или None.
        base_planet_id: уникальный идентификатор планеты (например, 'tb3_mixed_phase03_conflict01')
        """
        # Проверяем общие метрики (без привязки к планете)
        if map_stat_id in ("summary", "power", "strike_encounter", "strike_attempt",
                           "unit_donated", "covert_complete", "covert_attempt"):
            return map_stat_id, None

        # Зоны с префиксами _zone_ или _mission_
        patterns = [
            ("summary_zone", "summary"),
            ("power_zone", "power"),
            ("strike_encounter_zone", "strike_encounter"),
            ("strike_attempt_zone", "strike_attempt"),
            ("unit_donated_zone", "unit_donated"),
            ("covert_complete_mission", "covert_complete"),
            ("covert_round_attempted_mission", "covert_attempt"),
        ]
        for prefix, metric in patterns:
            if map_stat_id.startswith(prefix):
                # Убираем префикс и получаем base_id
                base_id = map_stat_id[len(prefix)+1:]  # +1 чтобы убрать '_' после префикса
                return metric, base_id

        # Если ничего не подошло
        return None, None

    def _extract_planet_name(self, base_planet_id):
        """
        Преобразует идентификатор планеты в читаемое русское название.
        Пример: 'tb3_mixed_phase03_conflict01' -> 'Конфликт 1 (Смешанная)'
        """
        if not base_planet_id:
            return "Общие очки"

        # Ищем сторону
        side = None
        for s in SIDE_MAP:
            if s in base_planet_id:
                side = SIDE_MAP[s]
                break

        # Ищем тип планеты и номер
        parts = base_planet_id.split("_")
        planet_type = None
        planet_num = None
        for i, part in enumerate(parts):
            if part in PLANET_TYPE:
                planet_type = PLANET_TYPE[part]
                # Номер может быть следующим элементом
                if i+1 < len(parts) and parts[i+1].isdigit():
                    planet_num = parts[i+1]
                break

        if planet_type:
            name = planet_type
            if planet_num:
                name += f" {planet_num}"
        else:
            # fallback
            name = base_planet_id

        if side:
            name += f" ({side})"
        return name

    def parse_player_stats_detailed(self, tb_result, member_id):
        """
        Собирает:
        - общие итоги (totals)
        - разбивку по раундам (phases)
        - таблицу по планетам (planets_list)
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
        # Словарь для планет: ключ = (фаза, base_id)
        planets = defaultdict(lambda: {
            "summary": 0, "power": 0,
            "strike_encounter": 0, "strike_attempt": 0,
            "unit_donated": 0,
            "covert_complete": 0, "covert_attempt": 0,
            "phase": 0,
            "base_id": ""
        })

        for zone in tb_result[0].get("finalStat", []):
            map_stat = zone.get("mapStatId", "")
            metric, base_id = self._get_metric_and_base(map_stat)

            if metric is None:
                continue

            # Общие метрики (без base_id)
            if base_id is None:
                for ps in zone.get("playerStat", []):
                    if ps.get("memberId") != member_id:
                        continue
                    value = int(ps.get("score", 0))
                    totals[metric] += value
                    # Также собираем раундовые ключи (summary_round_1...)
                    for key, val in ps.items():
                        if key in ("score", "memberId", "covertZoneResult"):
                            continue
                        try:
                            int_val = int(val)
                        except (ValueError, TypeError):
                            continue
                        round_match = re.search(r'_round_(\d+)$', key)
                        if round_match:
                            round_num = int(round_match.group(1))
                            if 1 <= round_num <= 6:
                                if "summary_round" in key:
                                    phases[round_num]["points"] = int_val
                                elif "power_round" in key:
                                    phases[round_num]["power"] = int_val
                                elif "unit_donated_round" in key:
                                    phases[round_num]["deployed"] = int_val
                                elif "strike_attempt_round" in key:
                                    phases[round_num]["combat_attempts"] = int_val
                                elif "strike_encounter_round" in key:
                                    phases[round_num]["combat_wins"] = int_val
                                elif "covert_attempt_round" in key:
                                    phases[round_num]["special_attempts"] = int_val
                                elif "covert_complete_round" in key:
                                    phases[round_num]["special_wins"] = int_val
                continue

            # Планетарные метрики: добавляем в соответствующий planet
            # Извлекаем фазу из base_id
            phase_match = re.search(r'phase(\d+)', base_id)
            phase = int(phase_match.group(1)) if phase_match else 0
            key = (phase, base_id)

            for ps in zone.get("playerStat", []):
                if ps.get("memberId") != member_id:
                    continue
                value = int(ps.get("score", 0))
                planets[key][metric] += value
                planets[key]["phase"] = phase
                planets[key]["base_id"] = base_id

        # Преобразуем planets в список для вывода
        planet_list = []
        for (phase, base_id), data in planets.items():
            name = self._extract_planet_name(base_id)
            planet_list.append({
                "phase": phase,
                "name": name,
                "summary": data["summary"],
                "power": data["power"],
                "strike_encounter": data["strike_encounter"],
                "strike_attempt": data["strike_attempt"],
                "unit_donated": data["unit_donated"],
                "covert_complete": data["covert_complete"],
                "covert_attempt": data["covert_attempt"],
            })

        # Если раундовые данные не были заполнены (нет общей зоны), суммируем из планет
        if all(p["points"] == 0 for p in phases.values()):
            for planet in planet_list:
                ph = planet["phase"]
                if 1 <= ph <= 6:
                    phases[ph]["points"] += planet["summary"]
                    phases[ph]["power"] += planet["power"]
                    phases[ph]["deployed"] += planet["unit_donated"]
                    phases[ph]["combat_attempts"] += planet["strike_attempt"]
                    phases[ph]["combat_wins"] += planet["strike_encounter"]
                    phases[ph]["special_attempts"] += planet["covert_attempt"]
                    phases[ph]["special_wins"] += planet["covert_complete"]

        # Дополнительно: если общие тоталы не были найдены, берём из раундов
        if totals["summary"] == 0:
            totals["summary"] = sum(p["points"] for p in phases.values())
            totals["power"] = sum(p["power"] for p in phases.values())
            totals["unit_donated"] = sum(p["deployed"] for p in phases.values())
            totals["strike_encounter"] = sum(p["combat_wins"] for p in phases.values())
            totals["strike_attempt"] = sum(p["combat_attempts"] for p in phases.values())
            totals["covert_complete"] = sum(p["special_wins"] for p in phases.values())
            totals["covert_attempt"] = sum(p["special_attempts"] for p in phases.values())

        return totals, phases, planet_list

    def format_player_report(self, player_name, totals, phases, planets):
        lines = [
            f"Детальная статистика: {player_name}",
            f"Всего очков: {totals['summary']:,}",
            f"Деплой (взводы): {totals['unit_donated']}",
            f"БЗ (усп/поп): {totals['strike_encounter']}/{totals['strike_attempt']}",
            f"ОЗ (усп/поп): {totals['covert_complete']}/{totals['covert_attempt']}",
            f"Мощь отрядов: {totals['power']:,}",
            "",
            "=== Сводка по фазам ===",
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

        if planets:
            lines.append("")
            lines.append("=== Детализация по планетам ===")
            header = (f"{'Планета':<30} {'Фаза':<5} {'Очки':>12} {'Мощь':>10} "
                      f"{'БЗ(усп)':>8} {'БЗ(поп)':>8} {'Деплой':>7} {'ОЗ(усп)':>8} {'ОЗ(поп)':>8}")
            lines.append(header)
            lines.append("-" * 110)
            planets_sorted = sorted(planets, key=lambda x: (x["phase"], x["name"]))
            for p in planets_sorted:
                lines.append(
                    f"{p['name']:<30} {p['phase']:<5} {p['summary']:>12,} {p['power']:>10,} "
                    f"{p['strike_encounter']:>8} {p['strike_attempt']:>8} {p['unit_donated']:>7} "
                    f"{p['covert_complete']:>8} {p['covert_attempt']:>8}"
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

            totals, phases, planets = self.parse_player_stats_detailed(result, player_id)
            if totals["summary"] == 0:
                await inter.edit_original_message(f"{name} не участвовал в последней ТБ.")
                return

            report = self.format_player_report(name, totals, phases, planets)
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