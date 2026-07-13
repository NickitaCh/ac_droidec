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

# ------------------ Декодер сырых ключей ТБ (comlink mapStatId) ------------------
# Ключи вида "power_zone_tb3_mixed_phase03_conflict01_bonus" разбираются на
# действие/фазу/планету/бонус/номер ОЗ-миссии. Ключи вида "power_round_3" — это
# официальные готовые итоги comlink по фазе (совпадают с внешними трекерами вроде
# HotUtils), а не пересчитанная нами сумма по зонам — используются как основной
# источник итогов по фазе, зональная разбивка идёт только как детализация.
TB_ACTION_LABELS = {
    "power": "GP развёрнуто",
    "summary": "Очки территории",
    "unit_donated": "Юниты во взвод",
    "strike_encounter": "БЗ: волн пройдено",
    "strike_attempt": "БЗ: попытки",
    "covert_complete": "ОЗ: миссий выполнено",
    "covert_round_attempted": "ОЗ: волны",
    "covert_attempt": "ОЗ: попытки",
    "disobey": "Rogue-действия",
}

TB_CONFLICT_LABELS = {"01": "Light", "02": "Dark", "03": "Mixed"}

TB_VALUE_COL = 30  # позиция, с которой начинается число — единая для всех уровней вложенности

# comlink для этой конкретной ТБ отдаёт заведомо неверные covert_round_attempted
# в этих зонах (проверено вручную: фаза 1 Mixed показывает 1 при фактических 2 волнах;
# фаза 3 Light-бонус показывает 3 при максимум возможных 2; фаза 3 Mixed показывает 6
# вместо фактических 3) — общее для всей гильдии искажение данных за этот ивент,
# не ошибка нашего парсинга. Прячем из основного вывода, показываем сырым ключом внизу.
TB_HIDDEN_ZONE_ACTIONS = {
    ("1", "03"): {"covert_round_attempted"},
    ("3", "01_bonus"): {"covert_round_attempted"},
    ("3", "03"): {"covert_round_attempted"},
}

TB_KNOWN_GLOBAL_ACTIONS = [
    "power", "summary", "unit_donated", "strike_encounter", "strike_attempt",
    "covert_attempt", "covert_complete", "covert_round_attempted", "disobey",
]

TB_ZONE_RE = re.compile(
    r"^(?P<action>[a-z_]+?)_(?P<kind>zone|mission)_tb3_mixed_phase(?P<phase>\d+)"
    r"_conflict(?P<conflict>\d+)(?P<bonus>_bonus)?(?:_covert(?P<covert>\d+))?$"
)
TB_ROUND_RE = re.compile(r"^(?P<action>[a-z_]+)_round_(?P<round>\d+)$")


def _fmt_num(value) -> str:
    return f"{int(value):,}".replace(",", " ")


def _fmt_line(label, value, indent=0) -> str:
    prefix = " " * indent + label
    number = _fmt_num(value)
    if len(prefix) >= TB_VALUE_COL:
        return f"{prefix} {number}"
    return f"{prefix}{' ' * (TB_VALUE_COL - len(prefix))}{number:>12}"


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

    # ------------------ Детальная статистика игрока (расшифровка по фазам/планетам) ------------------
    def _decode_tb_stats(self, tb_result, member_id):
        """
        Разбирает все mapStatId игрока на три группы:
        - zone_data[phase][conflict_key][action][entry] — разбивка по планетам/бонус-зонам/ОЗ-миссиям
        - global_totals[action] — итог за весь ивент
        - round_totals[round][action] — официальный итог comlink по фазе (round == фаза)
        """
        matched = {}
        for zone in tb_result[0].get("finalStat", []):
            map_id = zone.get("mapStatId")
            for ps in zone.get("playerStat", []):
                if ps.get("memberId") == member_id:
                    matched[map_id] = int(ps.get("score", 0))

        zone_data = defaultdict(lambda: defaultdict(lambda: defaultdict(dict)))
        raw_keys = defaultdict(lambda: defaultdict(lambda: defaultdict(dict)))
        global_totals = {}
        round_totals = defaultdict(dict)
        unrecognized = {}

        for map_id, score in matched.items():
            m = TB_ZONE_RE.match(map_id)
            if m:
                gd = m.groupdict()
                phase = str(int(gd["phase"]))  # "01" -> "1", чтобы совпадало с ключом round_totals
                conflict_key = gd["conflict"] + ("_bonus" if gd["bonus"] else "")
                entry_key = f"covert{gd['covert']}" if gd["covert"] else "_"
                zone_data[phase][conflict_key].setdefault(gd["action"], {})[entry_key] = score
                raw_keys[phase][conflict_key].setdefault(gd["action"], {})[entry_key] = map_id
                continue

            if map_id in TB_KNOWN_GLOBAL_ACTIONS:
                global_totals[map_id] = score
                continue

            m2 = TB_ROUND_RE.match(map_id)
            if m2 and m2.group("action") in TB_KNOWN_GLOBAL_ACTIONS:
                round_totals[str(int(m2.group("round")))][m2.group("action")] = score
                continue

            unrecognized[map_id] = score

        return zone_data, global_totals, round_totals, raw_keys, unrecognized

    def _format_tb_player_report(self, player_name, zone_data, global_totals, round_totals, raw_keys):
        """Формирует читаемый отчёт: общее по игроку, затем по фазам (round_N — источник
        итогов) с вложенной детализацией по планетам/бонус-зонам/ОЗ-миссиям (zone_data).
        Показатели из TB_HIDDEN_ZONE_ACTIONS (заведомо недостоверные для этой ТБ) не
        попадают в основной текст, а выводятся сырым ключом одним блоком в конце."""
        rule = "─" * 46
        lines = [f"📊 Территориальная Битва — {player_name}", rule]
        hidden_entries = []

        lines.append("")
        lines.append("ИТОГО ЗА СОБЫТИЕ")
        for action in ("summary", "unit_donated", "strike_encounter", "strike_attempt", "covert_attempt", "disobey"):
            if action in global_totals:
                lines.append(_fmt_line(TB_ACTION_LABELS.get(action, action), global_totals[action], indent=2))

        all_phases = sorted(set(round_totals.keys()) | set(zone_data.keys()), key=int)

        def conflict_sort_key(ck):
            base = ck.split("_")[0]
            return (base, 1 if "_bonus" in ck else 0)

        for phase in all_phases:
            rt = round_totals.get(phase, {})
            lines.append("")
            lines.append(rule)
            lines.append(f"ФАЗА {phase}")
            for action in ("summary", "power", "strike_attempt", "strike_encounter", "covert_attempt", "unit_donated", "disobey"):
                if action in rt:
                    lines.append(_fmt_line(TB_ACTION_LABELS.get(action, action), rt[action], indent=2))

            conflicts = zone_data.get(phase, {})
            if conflicts:
                lines.append("")
                lines.append("  Планеты:")
                for conflict_key in sorted(conflicts.keys(), key=conflict_sort_key):
                    base_conflict = conflict_key.split("_")[0]
                    is_bonus = "_bonus" in conflict_key
                    label = TB_CONFLICT_LABELS.get(base_conflict, f"Conflict {base_conflict}")
                    lines.append(f"    {label}" + (" (бонус)" if is_bonus else ""))

                    actions = conflicts[conflict_key]
                    hidden_actions = TB_HIDDEN_ZONE_ACTIONS.get((phase, conflict_key), set())
                    for action in sorted(actions.keys()):
                        if action in hidden_actions:
                            entries = raw_keys.get(phase, {}).get(conflict_key, {}).get(action, {})
                            for entry_key, value in actions[action].items():
                                raw_key = entries.get(entry_key, f"{action}_phase{phase}_conflict{conflict_key}_{entry_key}")
                                hidden_entries.append((raw_key, value))
                            continue
                        # Несколько ОЗ-миссий на одной планете (covert01, covert02...) —
                        # это разные независимые миссии, суммируем в одну строку.
                        total = sum(actions[action].values())
                        action_label = TB_ACTION_LABELS.get(action, action)
                        lines.append(_fmt_line(action_label, total, indent=6))
            elif rt:
                lines.append("  (детализация по планетам недоступна в comlink для этой фазы)")

        lines.append("")
        lines.append(rule)
        lines.append(
            "Примечание: «ОЗ: попытки» не привязано к планетам — comlink не даёт "
            "зональной детализации для этого показателя, только итог по фазе/игроку."
        )

        if hidden_entries:
            lines.append("")
            lines.append(rule)
            lines.append("Скрытые параметры (недостоверные данные comlink за эту ТБ):")
            for raw_key, value in hidden_entries:
                lines.append(f"  {raw_key} = {_fmt_num(value)}")

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

    @tb_report.sub_command(name="player", description="Статистика игрока за последнюю ТБ по фазам и планетам")
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

            zone_data, global_totals, round_totals, raw_keys, unrecognized = self._decode_tb_stats(result, player_id)
            if not zone_data and not round_totals and not global_totals:
                await inter.edit_original_message(f"{name} не участвовал в последней ТБ.")
                return

            report = self._format_tb_player_report(name, zone_data, global_totals, round_totals, raw_keys)
            if unrecognized:
                report += "\n\nНераспознанные ключи (новая механика?):\n"
                report += "\n".join(f"  {k}: {v}" for k, v in unrecognized.items())

            await self.send_as_file(inter.channel, report, f"tb_{name}.txt")
            embed = disnake.Embed(title=f"📊 ТБ по фазам и планетам: {name}", color=0x3498db)
            embed.set_footer(text="Полная детализация отправлена файлом.")
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