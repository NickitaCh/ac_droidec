import asyncio
import re
import disnake
from disnake.ext import commands, tasks
from datetime import datetime
from zoneinfo import ZoneInfo
import database
import guild_resolver
from services.units_sync import sync_units
from services.equipment_sync import sync_equipment
# Напрямую импортируем готовую рабочую функцию автозаполнения игроков
from cogs.violations import autocomplete_players
from cogs.datacron_requirements import DATACRON_LIST_COLOR, _lines_to_embeds

MSK = ZoneInfo("Europe/Moscow")

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


def _parse_deadline_input(text: str) -> str | None:
    """Дедлайн вводится вручную датой (не количеством дней, см. Discord-тред "Гайд
    по АС Боту", 2026-09-04) — принимает ГГГГ-ММ-ДД или ДД.ММ.ГГГГ, возвращает ISO-
    строку для хранения или None, если формат не распознан/дата в прошлом."""
    text = (text or "").strip()
    parsed = None
    for fmt in ("%Y-%m-%d", "%d.%m.%Y"):
        try:
            parsed = datetime.strptime(text, fmt).date()
            break
        except ValueError:
            continue
    if parsed is None or parsed < datetime.now().date():
        return None
    return parsed.strftime("%Y-%m-%d")


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
        # per-guild "уже отправляли уведомления по дедлайну сегодня" guard для
        # tasks_notify_loop (поминутный опрос, тот же паттерн, что rotation_ping.py)
        self._last_notify_day = {}
        # Запускаем фоновый аудит задач + плановую синхронизацию справочников юнитов/снаряжения
        self.units_sync_loop.start()
        self.equipment_sync_loop.start()
        self.tasks_audit_loop.start()
        self.tasks_notify_loop.start()

    def cog_unload(self):
        self.units_sync_loop.cancel()
        self.equipment_sync_loop.cancel()
        self.tasks_audit_loop.cancel()
        self.tasks_notify_loop.cancel()

    # =====================================================================
    # СИНХРОНИЗАЦИЯ СПРАВОЧНИКА ЮНИТОВ (get_game_data + русская локализация)
    # =====================================================================
    async def _do_units_synchronization(self) -> int:
        """Тонкая обёртка над services.units_sync.sync_units (общая реализация с
        веб-дашбордом, /задачи синхронизировать_юниты) — сама логика (запрос
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
        print("🔄 [Задачи] Плановая синхронизация справочника юнитов...")
        try:
            total = await self._do_units_synchronization()
            print(f"✅ [Задачи] Справочник обновлён: {total} юнитов")
        except Exception as e:
            print(f"⚠️ [Задачи] Не удалось обновить справочник: {e}")

    @units_sync_loop.before_loop
    async def before_units_sync(self):
        await self.bot.wait_until_ready()

    # =====================================================================
    # СИНХРОНИЗАЦИЯ СПРАВОЧНИКА СНАРЯЖЕНИЯ (для /фарм, cogs/gear_farm.py)
    # =====================================================================
    @tasks.loop(hours=24)
    async def equipment_sync_loop(self):
        """Плановое обновление справочника снаряжения/мест фарма — тот же цикл 24ч,
        что units_sync_loop выше, отдельным независимым тиком (свои try/except), чтобы
        сбой одного справочника не мешал другому."""
        print("🔄 [Задачи] Плановая синхронизация справочника снаряжения...")
        try:
            total = await sync_equipment(self.bot.comlink)
            print(f"✅ [Задачи] Справочник снаряжения обновлён: {total} деталей")
        except Exception as e:
            print(f"⚠️ [Задачи] Не удалось обновить справочник снаряжения: {e}")

    @equipment_sync_loop.before_loop
    async def before_equipment_sync(self):
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

    @staticmethod
    def _status_label(status: str, in_progress) -> tuple[str, str]:
        """(эмодзи, текст) — ACTIVE различается на "Назначено"/"В работе" в
        зависимости от in_progress (см. database.update_task_progress)."""
        if status == "COMPLETED":
            return "✅", "Выполнено"
        if status == "FAILED":
            return "❌", "Провалено"
        if in_progress:
            return "🟡", "В работе"
        return "🔵", "Назначено"

    def _progress_label(self, initial_value, current_value) -> str:
        if initial_value is None or current_value is None:
            return "—"
        if initial_value == current_value:
            return str(current_value)
        return f"{initial_value} → {current_value}"

    async def _fetch_unit_data(self, ally_code: str, base_id: str) -> dict | None:
        """Comlink-ростер игрока -> запись нужного юнита, или None (Comlink недоступен,
        либо юнит ещё не в ростере). Общая точка для аудита, постановки задачи
        (проверка дублей/"уже выполнено") и любого будущего кода, которому нужен
        живой снимок юнита."""
        try:
            player_data = await asyncio.to_thread(self.bot.comlink.get_player, ally_code)
        except Exception:
            return None
        roster = player_data.get('rosterUnit') or player_data.get('roster')
        if not roster:
            return None
        for u in roster:
            u_id = u.get('baseId') or u.get('definitionId', '').split(':')[0]
            if u_id == base_id:
                return u
        return None

    def _current_progress_value(self, unit_data: dict, target_type: str, target_value: str) -> str:
        """Значение, сравнимое с initial_value/target_value — "текущее состояние"
        для отчётов (пп. 8-9 из Discord-треда "Гайд по АС Боту")."""
        if target_type == 'stars':
            return str(unit_data.get('currentRarity', 0))
        if target_type == 'relic':
            return str(unit_data.get('relic', {}).get('currentTier', 0))
        if target_type == 'omicron':
            for skill in unit_data.get('skill', []):
                if skill.get('id') == target_value:
                    return str(skill.get('tier', 0))
            return "0"
        return ""

    def _is_target_completed(self, unit_data: dict, target_type: str, target_value: str, skill_thresholds: dict) -> bool:
        if target_type == 'stars':
            return unit_data.get('currentRarity', 0) >= int(target_value)
        if target_type == 'relic':
            current_relic_tier = unit_data.get('relic', {}).get('currentTier', 0)
            target_val_int = int(target_value)
            required_tier = target_val_int + 2 if target_val_int > 0 else 0
            return current_relic_tier >= required_tier
        if target_type == 'omicron':
            # Раньше здесь проверялось "есть ли у юнита ЛЮБОЙ skill с tier>=8" по
            # несуществующему ключу 'skills' (реальный ключ Comlink — 'skill', см.
            # services/activity_diff.py::diff_unit) — из-за опечатки список всегда
            # был пуст, и омикрон-задачи не завершались НИКОГДА. Теперь проверяем
            # конкретную способность (target_value = её skill_id, выбранный через
            # автокомплит при постановке) против её же реального омикрон-порога
            # tier'а из skill_tier_thresholds.
            omicron_tier = skill_thresholds.get(target_value, (None, None))[1]
            if omicron_tier is None:
                return False
            return any(
                skill.get('id') == target_value and skill.get('tier', -1) >= omicron_tier
                for skill in unit_data.get('skill', [])
            )
        return False

    async def _notify_task_result(self, guild_cfg: dict, base_id: str, target_type: str,
                                   target_value: str, status: str, player_name: str):
        """Запись в лог-канал гильдии (guilds.tasks_log_channel_id, /настройки задачи_канал)
        о завершении/провале задачи. ЛС игроку намеренно не шлём (решено 2026-09-04,
        Discord-тред "Гайд по АС Боту") — только выбранный канал."""
        unit_name = database.get_game_unit_name(base_id) or base_id
        verb = "выполнена 🎉" if status == "COMPLETED" else "провалена — истёк срок ⏰"
        target_label = self._format_target_label(target_type, target_value)
        text = f"Задача **{unit_name}** ({target_label}) для **{player_name}** {verb}"

        log_channel_id = guild_cfg.get("tasks_log_channel_id")
        if not log_channel_id:
            return
        channel = self.bot.get_channel(int(log_channel_id))
        if not channel:
            return
        try:
            await channel.send(text)
        except Exception as e:
            print(f"⚠️ [Задачи] Не удалось отправить лог в канал: {e}")

    async def _send_reminder(self, guild_cfg: dict, base_id: str, target_type: str,
                              target_value: str, deadline: str, player_name: str):
        """Только в лог-канал гильдии — см. _notify_task_result про отказ от ЛС."""
        unit_name = database.get_game_unit_name(base_id) or base_id
        target_label = self._format_target_label(target_type, target_value)
        text = (f"⏳ Напоминание: у задачи **{unit_name}** ({target_label}) для **{player_name}** "
                f"истекает срок {deadline}.")

        log_channel_id = guild_cfg.get("tasks_log_channel_id")
        if not log_channel_id:
            return
        channel = self.bot.get_channel(int(log_channel_id))
        if not channel:
            return
        try:
            await channel.send(text)
        except Exception as e:
            print(f"⚠️ [Задачи] Не удалось отправить напоминание в канал: {e}")

    # =====================================================================
    # ФОНОВЫЙ АУДИТ ЗАДАЧ (КАЖДЫЙ ЧАС) + НАПОМИНАНИЯ О ДЕДЛАЙНЕ
    # =====================================================================
    @tasks.loop(hours=1)
    async def tasks_audit_loop(self):
        """Ежечасная автоматическая проверка ВЫПОЛНЕНИЯ задач через Comlink (сверка
        ростера) — по каждой зарегистрированной гильдии отдельно (у фонового цикла нет
        интеракции, чтобы резолвить гильдию через guild_resolver). Просрочка и
        напоминания о дедлайне вынесены в tasks_notify_loop ниже (фиксированное время,
        не завязанное на момент рестарта бота) — здесь только "юнит уже прокачан?"."""
        print("🔍 Запуск ежечасного аудита заданий на прокачку...")
        # Per-skill индекс омикрон-ступени — единого порога вроде tier>=8 не существует
        # (см. services/activity_diff.py), грузим один раз на весь проход, не на задачу.
        skill_thresholds = database.get_all_skill_tier_thresholds()

        for guild_cfg in database.get_all_guild_configs():
            gid = guild_cfg["id"]
            gname = guild_cfg["name"]
            active_tasks = database.get_active_tasks(gid)
            if not active_tasks:
                continue
            cache = self.bot.guild_roster_caches.get(gid, {})
            print(f"📊 [{gname}] Аудит: нашёл в базе {len(active_tasks)} active tasks.")

            for task in active_tasks:
                task_id, ally_code, base_id, target_type, target_value, deadline_str, initial_value, _current_value, _in_progress = task
                player_name = cache.get(ally_code, f"Игрок [{ally_code}]")

                unit_data = await self._fetch_unit_data(ally_code, base_id)
                if unit_data is None:
                    continue

                if self._is_target_completed(unit_data, target_type, target_value, skill_thresholds):
                    database.update_task_status(task_id, "COMPLETED")
                    print(f"🎉 [{gname}] Задача #{task_id} ВЫПОЛНЕНА игроком {player_name}!")
                    await self._notify_task_result(guild_cfg, base_id, target_type, target_value, "COMPLETED", player_name)
                    continue

                current_value = self._current_progress_value(unit_data, target_type, target_value)
                in_progress = initial_value is not None and current_value != initial_value
                database.update_task_progress(task_id, current_value, in_progress)

    @tasks_audit_loop.before_loop
    async def before_tasks_audit(self):
        await self.bot.wait_until_ready()

    # =====================================================================
    # УВЕДОМЛЕНИЯ ПО ДЕДЛАЙНУ (просрочка + "скоро дедлайн") — раз в сутки, в
    # фиксированное per-guild время (guilds.tasks_notify_time, задаётся только в
    # вебе /settings, по умолчанию bot.TASKS_DEFAULT_NOTIFY_TIME = "10:00" МСК).
    # Раньше оба уведомления шли на каждом часовом Comlink-проходе аудита выше —
    # время рассылки плавало в зависимости от момента последнего рестарта бота.
    # Поминутный опрос + per-guild "уже отправляли сегодня" guard — тот же
    # паттерн, что rotation_ping.py/birthday.py (см. CLAUDE.md).
    # =====================================================================
    @tasks.loop(seconds=30)
    async def tasks_notify_loop(self):
        now_msk = datetime.now(MSK)
        today_key = now_msk.strftime("%Y-%m-%d")

        for guild_cfg in database.get_all_guild_configs():
            gid = guild_cfg["id"]
            notify_time = guild_cfg.get("tasks_notify_time") or self.bot.TASKS_DEFAULT_NOTIFY_TIME
            try:
                target_hour, target_minute = map(int, notify_time.split(":"))
            except (ValueError, AttributeError):
                target_hour, target_minute = map(int, self.bot.TASKS_DEFAULT_NOTIFY_TIME.split(":"))

            if (now_msk.hour, now_msk.minute) != (target_hour, target_minute):
                continue
            if self._last_notify_day.get(gid) == today_key:
                continue
            self._last_notify_day[gid] = today_key

            active_tasks = database.get_active_tasks(gid)
            if not active_tasks:
                continue
            gname = guild_cfg["name"]
            cache = self.bot.guild_roster_caches.get(gid, {})
            today_date = now_msk.date()

            for task_id, ally_code, base_id, target_type, target_value, deadline_str in active_tasks:
                try:
                    deadline_date = datetime.strptime(deadline_str, "%Y-%m-%d").date()
                except ValueError:
                    continue
                if today_date > deadline_date:
                    player_name = cache.get(ally_code, f"Игрок [{ally_code}]")
                    database.update_task_status(task_id, "FAILED")
                    print(f"⏰ [{gname}] Срок задачи #{task_id} для {player_name} по юниту {base_id} истёк.")
                    await self._notify_task_result(guild_cfg, base_id, target_type, target_value, "FAILED", player_name)

            reminder_rows = database.get_tasks_needing_reminder(gid, self.bot.TASK_REMINDER_DAYS_BEFORE)
            for task_id, ally_code, base_id, target_type, target_value, deadline_str in reminder_rows:
                player_name = cache.get(ally_code, f"Игрок [{ally_code}]")
                await self._send_reminder(guild_cfg, base_id, target_type, target_value, deadline_str, player_name)
                database.mark_task_reminder_sent(task_id)

    @tasks_notify_loop.before_loop
    async def before_tasks_notify(self):
        await self.bot.wait_until_ready()

    # =====================================================================
    # ГРУППА /задачи
    # =====================================================================
    @commands.slash_command(name="задачи", description="Постановка и проверка задач на прокачку")
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
        дедлайн: str = commands.Param(desc="Дата дедлайна: ГГГГ-ММ-ДД или ДД.ММ.ГГГГ"),
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

        deadline_date = _parse_deadline_input(дедлайн)
        if deadline_date is None:
            await inter.edit_original_response("❌ Некорректная или прошедшая дата. Формат: ГГГГ-ММ-ДД или ДД.ММ.ГГГГ.")
            return

        # Запрет дублей — если у игрока уже есть активная задача на этот же юнит
        # (независимо от типа/значения цели), новую не ставим (решено 2026-09-04,
        # Discord-тред "Гайд по АС Боту").
        if database.get_active_task_for_unit(guild_id, ally_code, base_id):
            await inter.edit_original_response(f"❌ Задача по юниту **{unit_name}** для **{player_name}** уже назначена.")
            return

        # Не создаём задачу, если цель уже выполнена на момент постановки (финальное
        # решение в том же треде: "можно вообще задачу в список не добавлять, если
        # условия по ней уже выполнены" / "зачем плодить сущности"). Comlink
        # недоступен/юнит не в ростере — не блокируем постановку, просто без
        # initial_value для отслеживания прогресса.
        target_label = self._format_target_label(тип_цели, значение)
        initial_value = None
        unit_data = await self._fetch_unit_data(ally_code, base_id)
        if unit_data is not None:
            skill_thresholds = database.get_all_skill_tier_thresholds() if тип_цели == "omicron" else {}
            if self._is_target_completed(unit_data, тип_цели, значение, skill_thresholds):
                await inter.edit_original_response(
                    f"❕ У **{player_name}** юнит **{unit_name}** уже соответствует цели ({target_label}) — задача не создана."
                )
                return
            initial_value = self._current_progress_value(unit_data, тип_цели, значение)

        database.add_task(ally_code, base_id, тип_цели, значение, deadline_date, str(inter.author.id),
                           guild_id=guild_id, initial_value=initial_value)

        embed = disnake.Embed(
            title="🎯 Задача на прокачку успешно поставлена",
            description=f"**Игрок:** {player_name} ({ally_code})\n"
                        f"**Юнит:** {unit_name}\n\n"
                        f"📊 **Цель:** {target_label}\n"
                        f"⏱ **Срок:** до {deadline_date}",
            color=disnake.Color.blue()
        )
        await inter.edit_original_response(embed=embed)

    @tasks_group.sub_command(name="отчёт", description="Прогресс по задачам — свой открыт всем, чужой и по всей гильдии — только офицерам")
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

            # "Текущие" задачи — активные, или завершённые/проваленные не старше
            # database.TASK_ARCHIVE_AFTER_DAYS дней (архивные не выводятся, см. пункт
            # "Архив" из Discord-треда "Гайд по АС Боту"). Провальные включены
            # намеренно — Ricardo: "Включая провальные".
            rows = [
                r for r in database.get_all_tasks(guild_id)
                if r[6] in ("ACTIVE", "FAILED") and not database.is_task_archived(r[12])
            ]
            if not rows:
                await inter.edit_original_response("🎉 Активных или проваленных задач нет — все чисто.")
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

            embeds = _lines_to_embeds("📋 Задачи — гильдия", DATACRON_LIST_COLOR, lines)
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
                    await inter.edit_original_response("❌ Просмотр чужих задач доступен только офицерам.")
                    return

        rows = [r for r in database.get_tasks_for_ally(ally_code, guild_id=guild_id) if not database.is_task_archived(r[11])]
        if not rows:
            await inter.edit_original_response(f"У игрока **{игрок}** нет текущих задач.")
            return

        lines = []
        for (_task_id, _ally_code, base_id, target_type, target_value, deadline, status,
             initial_value, current_value, in_progress, created_by, _resolved_at) in rows:
            unit_name = database.get_game_unit_name(base_id) or base_id
            target_label = self._format_target_label(target_type, target_value)
            emoji, status_text = self._status_label(status, in_progress)
            progress = self._progress_label(initial_value, current_value)
            author_name = database.get_username_for_discord_id(created_by) if created_by else None
            lines.append(
                f"{emoji} **{unit_name}** — {target_label} ({status_text}) · прогресс: {progress} · "
                f"до {deadline}" + (f" · назначил: {author_name}" if author_name else "")
            )

        embeds = _lines_to_embeds(f"📋 Задачи — {игрок}", DATACRON_LIST_COLOR, lines)
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
