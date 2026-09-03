import asyncio
import re
import disnake
from disnake.ext import commands, tasks
from datetime import datetime, timedelta
import database
import guild_resolver
from services.units_sync import sync_units
# Напрямую импортируем готовую рабочую функцию автозаполнения игроков
from cogs.violations import autocomplete_players
from cogs.datacron_requirements import DATACRON_LIST_COLOR, _lines_to_embeds

# =====================================================================
# АВТОКОМПЛИТЫ ДЛЯ КОМАНДЫ ПОСТАНОВКИ ЗАДАЧ (ВНЕ КЛАССА)
# =====================================================================
async def units_autocomplete(inter: disnake.ApplicationCommandInteraction, string: str):
    """Ищет персонажей/корабли в локальном справочнике game_units (глобальный, не per-guild)"""
    rows = database.search_game_units(string)
    return [f"{name} [{bid}]" for bid, name in rows]


def _parse_unit_base_id(unit_field: str) -> str:
    m = re.search(r"\[(.*?)\]", unit_field or "")
    return m.group(1) if m else (unit_field or "").strip().upper()


async def autocomplete_task_target_value(inter: disnake.ApplicationCommandInteraction, string: str):
    """Для звёзд/реликвии значение вводится вручную (число) — автокомплит не нужен.
    Для омикрона нельзя просто ввести число: у юнита может быть больше одного омикрона,
    и аудит должен знать, ЗА КАКУЮ КОНКРЕТНО способность проверять tier (см. баг, из-за
    которого омикрон-задачи раньше не завершались никогда — cogs/tasks.py::tasks_audit_loop).
    Поэтому при тип_цели=omicron подсказывает конкретные омикроны выбранного юнита, а
    сохраняется реальный skill_id (не название)."""
    if inter.filled_options.get("тип_цели") != "omicron":
        return []
    unit_field = inter.filled_options.get("юнит")
    if not unit_field:
        return ["⚠️ Сначала выберите юнита"]
    base_id = _parse_unit_base_id(unit_field)
    skill_ids = database.get_all_unit_omicron_skills().get(base_id, [])
    if not skill_ids:
        return ["❌ У этого юнита нет омикрона"]
    info = database.get_skill_display_info(skill_ids)
    search = string.lower().strip()
    options = []
    for skill_id in skill_ids:
        name, _ability_id, ability_type, _omicron_mode = info.get(skill_id, (skill_id, None, None, None))
        label = f"{name or skill_id}" + (f" ({ability_type})" if ability_type else "")
        if not search or search in label.lower():
            options.append(disnake.OptionChoice(name=label[:100], value=skill_id))
    return options[:25]


class TasksCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        # Запускаем фоновый аудит задач + плановую синхронизацию справочника юнитов
        self.units_sync_loop.start()
        self.tasks_audit_loop.start()

    def cog_unload(self):
        self.units_sync_loop.cancel()
        self.tasks_audit_loop.cancel()

    # =====================================================================
    # СИНХРОНИЗАЦИЯ СПРАВОЧНИКА ЮНИТОВ (get_game_data + русская локализация)
    # =====================================================================
    async def _do_units_synchronization(self) -> int:
        """Тонкая обёртка над services.units_sync.sync_units (общая реализация с
        веб-дашбордом, /задания синхронизировать_юниты) — сама логика (запрос
        UnitDefinitions + RUS_RU локализация, см. докстринг sync_units) живёт там,
        чтобы бот и веб не расходились при будущих правках."""
        return await sync_units(self.bot.comlink)

    @tasks.loop(hours=24)
    async def units_sync_loop(self):
        """Плановое обновление справочника юнитов раз в сутки — раньше синхронизация
        была только при старте бота (см. before_tasks_audit ниже), и при стабильном
        аптайме без рестартов новые персонажи/корабли просто не появлялись в
        автокомплите. disnake.ext.tasks запускает тело цикла сразу при старте (не ждёт
        первый интервал), так что отдельный "первый прогон при старте" теперь не нужен —
        этот цикл сам закрывает и старт, и дальнейшее ежедневное обновление."""
        print("🔄 [Задания] Плановая синхронизация справочника юнитов...")
        try:
            total = await self._do_units_synchronization()
            print(f"✅ [Задания] Справочник обновлён: {total} юнитов")
        except Exception as e:
            print(f"⚠️ [Задания] Не удалось обновить справочник: {e}")

    @units_sync_loop.before_loop
    async def before_units_sync(self):
        await self.bot.wait_until_ready()

    def _format_target_label(self, target_type: str, target_value: str) -> str:
        if target_type == "stars":
            return f"⭐ Звёзды {target_value}"
        if target_type == "relic":
            return f"♦️ Реликвия {target_value}"
        if target_type == "omicron":
            info = database.get_skill_display_info([target_value]).get(target_value)
            name = info[0] if info and info[0] else target_value
            return f"🧬 Омикрон: {name}"
        return target_value

    async def _notify_task_result(self, guild_cfg: dict, ally_code: str, base_id: str, target_type: str,
                                   target_value: str, status: str, player_name: str, discord_id_map: dict):
        """Личное сообщение игроку (если привязан через /регистрация) + запись в
        лог-канал гильдии (guilds.tasks_log_channel_id, /настройки задания_канал) —
        раньше завершение/провал задачи молча меняли статус в БД, никто не узнавал."""
        unit_name = database.get_game_unit_name(base_id) or base_id
        verb = "выполнена 🎉" if status == "COMPLETED" else "провалена — истёк срок ⏰"
        target_label = self._format_target_label(target_type, target_value)
        text = f"Задание **{unit_name}** ({target_label}) для **{player_name}** {verb}"

        discord_id = discord_id_map.get(ally_code)
        if discord_id:
            try:
                user = self.bot.get_user(int(discord_id)) or await self.bot.fetch_user(int(discord_id))
                await user.send(text)
            except Exception:
                pass  # ЛС закрыты/недоступны — не критично, лог-канал всё равно есть

        log_channel_id = guild_cfg.get("tasks_log_channel_id")
        if log_channel_id:
            channel = self.bot.get_channel(int(log_channel_id))
            if channel:
                try:
                    await channel.send(text)
                except Exception as e:
                    print(f"⚠️ [Задания] Не удалось отправить лог в канал: {e}")

    async def _send_reminder(self, guild_cfg: dict, ally_code: str, base_id: str, target_type: str,
                              target_value: str, deadline: str, player_name: str, discord_id_map: dict):
        unit_name = database.get_game_unit_name(base_id) or base_id
        target_label = self._format_target_label(target_type, target_value)
        text = (f"⏳ Напоминание: до дедлайна задания **{unit_name}** ({target_label}) "
                f"для **{player_name}** остаётся {self.bot.TASK_REMINDER_DAYS_BEFORE} дн. или меньше (срок: {deadline}).")

        discord_id = discord_id_map.get(ally_code)
        if discord_id:
            try:
                user = self.bot.get_user(int(discord_id)) or await self.bot.fetch_user(int(discord_id))
                await user.send(text)
            except Exception:
                pass

        log_channel_id = guild_cfg.get("tasks_log_channel_id")
        if log_channel_id:
            channel = self.bot.get_channel(int(log_channel_id))
            if channel:
                try:
                    await channel.send(text)
                except Exception as e:
                    print(f"⚠️ [Задания] Не удалось отправить напоминание в канал: {e}")

    # =====================================================================
    # ФОНОВЫЙ АУДИТ ЗАДАЧ (КАЖДЫЙ ЧАС) + НАПОМИНАНИЯ О ДЕДЛАЙНЕ
    # =====================================================================
    @tasks.loop(hours=1)
    async def tasks_audit_loop(self):
        """Ежечасная автоматическая проверка выполнения задач через Comlink —
        по каждой зарегистрированной гильдии отдельно (у фонового цикла нет
        интеракции, чтобы резолвить гильдию через guild_resolver)."""
        print("🔍 Запуск ежечасного аудита заданий на прокачку...")
        now = datetime.now()
        # Per-skill индекс омикрон-ступени — единого порога вроде tier>=8 не существует
        # (см. services/activity_diff.py), грузим один раз на весь проход, не на задачу.
        skill_thresholds = database.get_all_skill_tier_thresholds()

        for guild_cfg in database.get_all_guild_configs():
            gid = guild_cfg["id"]
            gname = guild_cfg["name"]
            active_tasks = database.get_active_tasks(gid)
            cache = self.bot.guild_roster_caches.get(gid, {})
            discord_id_map = None

            if active_tasks:
                print(f"📊 [{gname}] Аудит: нашёл в базе {len(active_tasks)} active tasks.")
                discord_id_map = {ally_code: discord_id for discord_id, ally_code, _n in database.get_all_main_registrations(gid)}

                for task in active_tasks:
                    task_id, ally_code, base_id, target_type, target_value, deadline_str = task
                    player_name = cache.get(ally_code, f"Игрок [{ally_code}]")

                    try:
                        deadline = datetime.strptime(deadline_str, "%Y-%m-%d")
                    except ValueError:
                        continue

                    if now.date() > deadline.date():
                        database.update_task_status(task_id, "FAILED")
                        print(f"⏰ [{gname}] Срок задачи #{task_id} для {player_name} по юниту {base_id} истёк.")
                        await self._notify_task_result(guild_cfg, ally_code, base_id, target_type, target_value, "FAILED", player_name, discord_id_map)
                        continue

                    try:
                        player_data = await asyncio.to_thread(self.bot.comlink.get_player, ally_code)
                        roster = player_data.get('rosterUnit') or player_data.get('roster')
                        if not roster:
                            continue
                    except Exception as e:
                        print(f"⚠️ [{gname}] Аудит не смог достучаться до Comlink для {ally_code}: {e}")
                        continue

                    unit_data = None
                    for u in roster:
                        u_id = u.get('baseId') or u.get('definitionId', '').split(':')[0]
                        if u_id == base_id:
                            unit_data = u
                            break

                    if not unit_data:
                        continue

                    is_completed = False

                    if target_type == 'stars':
                        if unit_data.get('currentRarity', 0) >= int(target_value):
                            is_completed = True

                    elif target_type == 'relic':
                        relic_data = unit_data.get('relic', {})
                        current_relic_tier = relic_data.get('currentTier', 0)
                        target_val_int = int(target_value)
                        required_tier = target_val_int + 2 if target_val_int > 0 else 0
                        if current_relic_tier >= required_tier:
                            is_completed = True

                    elif target_type == 'omicron':
                        # Раньше здесь проверялось "есть ли у юнита ЛЮБОЙ skill с tier>=8" по
                        # несуществующему ключу 'skills' (реальный ключ Comlink — 'skill',
                        # см. services/activity_diff.py::diff_unit) — из-за опечатки в ключе
                        # список всегда был пуст, и омикрон-задания не завершались НИКОГДА.
                        # Теперь проверяем конкретную способность (target_value = её skill_id,
                        # выбранный через автокомплит при постановке) против её же реального
                        # омикрон-порога tier'а из skill_tier_thresholds.
                        omicron_tier = skill_thresholds.get(target_value, (None, None))[1]
                        if omicron_tier is not None:
                            for skill in unit_data.get('skill', []):
                                if skill.get('id') == target_value and skill.get('tier', -1) >= omicron_tier:
                                    is_completed = True
                                    break

                    if is_completed:
                        database.update_task_status(task_id, "COMPLETED")
                        print(f"🎉 [{gname}] Задача #{task_id} ВЫПОЛНЕНА игроком {player_name}!")
                        await self._notify_task_result(guild_cfg, ally_code, base_id, target_type, target_value, "COMPLETED", player_name, discord_id_map)

            reminder_rows = database.get_tasks_needing_reminder(gid, self.bot.TASK_REMINDER_DAYS_BEFORE)
            if reminder_rows:
                if discord_id_map is None:
                    discord_id_map = {ally_code: discord_id for discord_id, ally_code, _n in database.get_all_main_registrations(gid)}
                for task_id, ally_code, base_id, target_type, target_value, deadline_str in reminder_rows:
                    player_name = cache.get(ally_code, f"Игрок [{ally_code}]")
                    await self._send_reminder(guild_cfg, ally_code, base_id, target_type, target_value, deadline_str, player_name, discord_id_map)
                    database.mark_task_reminder_sent(task_id)

    @tasks_audit_loop.before_loop
    async def before_tasks_audit(self):
        await self.bot.wait_until_ready()

    # =====================================================================
    # ГРУППА /задания
    # =====================================================================
    @commands.slash_command(name="задания", description="Постановка и проверка заданий на прокачку")
    async def tasks_group(self, inter: disnake.ApplicationCommandInteraction):
        pass

    @tasks_group.sub_command(name="добавить", description="Поставить задачу игроку на прокачку юнита")
    async def task_add(
        self,
        inter: disnake.ApplicationCommandInteraction,
        игрок: str = commands.Param(desc="Выберите игрока", autocomplete=autocomplete_players),
        юнит: str = commands.Param(desc="Выберите персонажа/корабль", autocomplete=units_autocomplete),
        тип_цели: str = commands.Param(
            desc="Критерий прокачки",
            choices=[
                disnake.OptionChoice(name="⭐ Звёзды (1-7)", value="stars"),
                disnake.OptionChoice(name="♦️ Реликвия (0-9)", value="relic"),
                disnake.OptionChoice(name="🧬 Омикрон", value="omicron")
            ]
        ),
        значение: str = commands.Param(
            desc="Число для звёзд/реликвии, либо способность для омикрона — выберите из подсказки",
            autocomplete=autocomplete_task_target_value,
        ),
        дней: int = commands.Param(desc="Сколько дней даётся на выполнение задачи?", min_value=1, max_value=365),
    ):
        await inter.response.defer()

        guild_id = await guild_resolver.require_guild_id(inter)
        if guild_id is None:
            return

        cache = self.bot.guild_roster_caches.get(guild_id, {})
        ally_code = cache.get(игрок)

        if not ally_code:
            row = database.get_user_mapping_for_name(guild_id, игрок)
            if row:
                ally_code = row[0]

        if not ally_code:
            await inter.edit_original_response("❌ Ошибка: Не удалось определить Ally Code выбранного игрока.")
            return

        player_name = cache.get(ally_code, игрок)

        base_id = _parse_unit_base_id(юнит)
        unit_name = database.get_game_unit_name(base_id)
        if not unit_name:
            await inter.edit_original_response(f"❌ Юнит `{base_id}` не найден в справочнике.")
            return

        значение = значение.strip()
        if тип_цели == "omicron":
            valid_skills = database.get_all_unit_omicron_skills().get(base_id, [])
            if значение not in valid_skills:
                await inter.edit_original_response("❌ Для омикрона выберите конкретную способность из подсказки автокомплита (не свободный текст).")
                return
        elif not значение.isdigit():
            await inter.edit_original_response("❌ Для звёзд/реликвии значение должно быть числом.")
            return

        deadline_date = (datetime.now() + timedelta(days=дней)).strftime("%Y-%m-%d")

        database.add_task(ally_code, base_id, тип_цели, значение, deadline_date, str(inter.author.id), guild_id=guild_id)

        target_label = self._format_target_label(тип_цели, значение)
        embed = disnake.Embed(
            title="🎯 Задача на прокачку успешно поставлена",
            description=f"**Игрок:** {player_name} ({ally_code})\n"
                        f"**Юнит:** {unit_name}\n\n"
                        f"📊 **Цель:** {target_label}\n"
                        f"⏱ **Срок:** {дней} дней (до {deadline_date})",
            color=disnake.Color.blue()
        )
        await inter.edit_original_response(embed=embed)

    @tasks_group.sub_command(name="отчёт", description="Прогресс по заданиям — свой открыт всем, чужой и по всей гильдии — только офицерам")
    async def tasks_report(
        self,
        inter: disnake.ApplicationCommandInteraction,
        игрок: str = commands.Param(default=None, description="Игрок гильдии — если не указан, берётся ваша регистрация (/регистрация)", autocomplete=autocomplete_players),
        гильдия: bool = commands.Param(default=False, description="Сводка по всей гильдии вместо одного игрока — только для офицеров"),
    ):
        await inter.response.defer()

        guild_id = await guild_resolver.require_guild_id(inter)
        if guild_id is None:
            return

        is_officer = guild_resolver.is_officer_for_resolved_guild(inter.author)

        if гильдия:
            if not is_officer:
                await inter.edit_original_response("❌ Сводка по всей гильдии доступна только офицерам.")
                return

            rows = [r for r in database.get_all_tasks(guild_id) if r[6] in ("ACTIVE", "FAILED")]
            if not rows:
                await inter.edit_original_response("🎉 Активных или проваленных заданий нет — все чисто.")
                return

            cache = self.bot.guild_roster_caches.get(guild_id, {})
            by_player = {}
            for row in rows:
                ally_code, status = row[1], row[6]
                by_player.setdefault(ally_code, []).append(status)

            lines = []
            for ally_code, statuses in sorted(by_player.items(), key=lambda kv: -len(kv[1])):
                name = cache.get(ally_code, ally_code)
                parts = []
                active_n = statuses.count("ACTIVE")
                failed_n = statuses.count("FAILED")
                if active_n:
                    parts.append(f"в работе: {active_n}")
                if failed_n:
                    parts.append(f"провалено: {failed_n}")
                lines.append(f"• {name} — {', '.join(parts)}")

            embeds = _lines_to_embeds("📋 Задания — гильдия", DATACRON_LIST_COLOR, lines)
            await inter.edit_original_response(embed=embeds[0])
            for e in embeds[1:]:
                await inter.followup.send(embed=e)
            return

        if игрок is None:
            registration = database.get_user_registration(str(inter.author.id), guild_id=guild_id)
            if not registration:
                await inter.edit_original_response("❌ Игрок не указан, а вы не зарегистрированы — используйте `/регистрация` или укажите игрока явно.")
                return
            ally_code, игрок = registration
        else:
            cache = self.bot.guild_roster_caches.get(guild_id, {})
            ally_code = cache.get(игрок)
            if not ally_code:
                await inter.edit_original_response("❌ Игрок не найден в составе гильдии.")
                return
            if not is_officer:
                self_reg = database.get_user_registration(str(inter.author.id), guild_id=guild_id)
                if not self_reg or self_reg[0] != ally_code:
                    await inter.edit_original_response("❌ Просмотр чужих заданий доступен только офицерам.")
                    return

        rows = database.get_tasks_for_ally(ally_code, guild_id=guild_id)
        if not rows:
            await inter.edit_original_response(f"У игрока **{игрок}** нет заданий.")
            return

        status_emoji = {"ACTIVE": "🔵", "COMPLETED": "✅", "FAILED": "❌"}
        lines = []
        for _task_id, _ally_code, base_id, target_type, target_value, deadline, status in rows:
            unit_name = database.get_game_unit_name(base_id) or base_id
            target_label = self._format_target_label(target_type, target_value)
            lines.append(f"{status_emoji.get(status, '⚪')} **{unit_name}** — {target_label} (до {deadline})")

        embeds = _lines_to_embeds(f"📋 Задания — {игрок}", DATACRON_LIST_COLOR, lines)
        await inter.edit_original_response(embed=embeds[0])
        for e in embeds[1:]:
            await inter.followup.send(embed=e)

    @tasks_group.sub_command(name="синхронизировать_юниты", description="Принудительно обновить справочник персонажей/кораблей")
    async def sync_units_cmd(self, inter: disnake.ApplicationCommandInteraction):
        await inter.response.defer()
        try:
            total = await self._do_units_synchronization()
            await inter.edit_original_response(f"✅ Справочник успешно обновлен вручную! Записано юнитов: **{total}**.")
        except Exception as e:
            await inter.edit_original_response(f"❌ Ошибка при обновлении: `{e}`")


def setup(bot):
    bot.add_cog(TasksCog(bot))
