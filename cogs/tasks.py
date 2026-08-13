import disnake
from disnake.ext import commands, tasks
import asyncio
import re
from datetime import datetime, timedelta
import database
import guild_resolver
# Напрямую импортируем готовую рабочую функцию автозаполнения игроков
from cogs.violations import autocomplete_players

# 'UnitDefinitions' — бит из comlink.get_enums()["GameDataItemsEnum"], тот же приём,
# что DATACRON_DEFINITIONS_FLAG в datacron_requirements.py.
UNIT_DEFINITIONS_FLAG = 137438953472

# =====================================================================
# АВТОКОМПЛИТЫ ДЛЯ КОМАНДЫ ПОСТАНОВКИ ЗАДАЧ (ВНЕ КЛАССА)
# =====================================================================
async def units_autocomplete(inter: disnake.ApplicationCommandInteraction, string: str):
    """Ищет персонажей/корабли в локальном справочнике game_units (глобальный, не per-guild)"""
    rows = database.search_game_units(string)
    return [f"{name} [{bid}]" for bid, name in rows]


class TasksCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        # Запускаем фоновый аудит задач
        self.tasks_audit_loop.start()

    def cog_unload(self):
        self.tasks_audit_loop.cancel()

    # =====================================================================
    # СИНХРОНИЗАЦИЯ СПРАВОЧНИКА ЮНИТОВ (get_game_data + русская локализация)
    # =====================================================================
    async def _do_units_synchronization(self) -> int:
        """Стягивает справочник юнитов через comlink.get_game_data(items=UnitDefinitions) —
        тот же приём, что _fetch_datacron_cache в datacron_requirements.py. Старый вариант
        через сырой aiohttp POST /data с минимальным payload молча отдавал пустой 'units'
        (воспроизведено вживую) — library-метод отдаёт полный список без этой проблемы.
        Имена берём из RUS_RU локализации, а не из nameKey — то же, что делает datacron-кэш.
        """
        game_data = await asyncio.to_thread(self.bot.comlink.get_game_data, items=str(UNIT_DEFINITIONS_FLAG))
        units_list = game_data.get('units') or []
        if not units_list:
            raise Exception("В полученных данных отсутствует массив персонажей ('units').")

        loc = await asyncio.to_thread(self.bot.comlink.get_localization, locale="RUS_RU", unzip=True)
        loc_text = loc.get("Loc_RUS_RU.txt", "")
        loc_kv = {}
        for line in loc_text.split("\n"):
            if "|" not in line:
                continue
            k, _, v = line.partition("|")
            loc_kv[k.strip()] = v.strip()

        units_to_db = {}
        for unit in units_list:
            bid = unit.get('baseId')
            if not bid:
                continue
            name_key = unit.get('nameKey', bid)
            name = loc_kv.get(name_key, name_key)
            unit_type = "ship" if unit.get("combatType") == 2 else "character"
            units_to_db[bid] = (name, unit_type)

        database.upsert_game_units(units_to_db)
        return len(units_to_db)

    # =====================================================================
    # ФОНОВЫЙ АУДИТ ЗАДАЧ (КАЖДЫЙ ЧАС)
    # =====================================================================
    @tasks.loop(hours=1)
    async def tasks_audit_loop(self):
        """Ежечасная автоматическая проверка выполнения задач через Comlink —
        по каждой зарегистрированной гильдии отдельно (у фонового цикла нет
        интеракции, чтобы резолвить гильдию через guild_resolver)."""
        print("🔍 Запуск ежечасного аудита задач на прокачку...")
        now = datetime.now()

        for guild_cfg in database.get_all_guild_configs():
            gid = guild_cfg["id"]
            gname = guild_cfg["name"]
            active_tasks = database.get_active_tasks(gid)
            print(f"📊 [{gname}] Аудит: нашёл в базе {len(active_tasks)} active tasks.")

            if not active_tasks:
                continue

            cache = self.bot.guild_roster_caches.get(gid, {})

            for task in active_tasks:
                task_id, ally_code, base_id, target_type, target_value, deadline_str = task

                try:
                    deadline = datetime.strptime(deadline_str, "%Y-%m-%d")
                except ValueError:
                    continue

                if now.date() > deadline.date():
                    database.update_task_status(task_id, "FAILED")
                    player_name = cache.get(ally_code, f"Игрок [{ally_code}]")
                    print(f"⏰ [{gname}] Срок задачи #{task_id} для {player_name} по юниту {base_id} истёк.")
                    continue

                try:
                    player_data = self.bot.comlink.get_player(ally_code)
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
                target_val_int = int(target_value)

                if target_type == 'stars':
                    if unit_data.get('currentRarity', 0) >= target_val_int:
                        is_completed = True

                elif target_type == 'relic':
                    relic_data = unit_data.get('relic', {})
                    current_relic_tier = relic_data.get('currentTier', 0)
                    required_tier = target_val_int + 2 if target_val_int > 0 else 0

                    if current_relic_tier >= required_tier:
                        is_completed = True

                elif target_type == 'omicron':
                    skills = unit_data.get('skills', [])
                    for skill in skills:
                        if skill.get('tier', 0) >= 8:
                            is_completed = True

                if is_completed:
                    database.update_task_status(task_id, "COMPLETED")
                    player_name = cache.get(ally_code, f"Игрок [{ally_code}]")
                    print(f"🎉 [{gname}] Задача #{task_id} ВЫПОЛНЕНА игроком {player_name}!")

    @tasks_audit_loop.before_loop
    async def before_tasks_audit(self):
        await self.bot.wait_until_ready()
        
        print("🔄 [Авто-старт] Начинаю автоматическую синхронизацию справочника юнитов...")
        try:
            total = await self._do_units_synchronization()
            print(f"✅ [Авто-старт] Справочник успешно обновлен! Загружено юнитов: {total}")
        except Exception as e:
            print(f"⚠️ [Авто-старт] Не удалось обновить справочник при запуске: {e}")
            print("Бот продолжит работу на старой/текущей базе данных.")

    # =====================================================================
    # СЛЭШ-КОМАНДА: /sync_units (ПРИНУДИТЕЛЬНОЕ ОБНОВЛЕНИЕ СПРАВОЧНИКА)
    # =====================================================================
    @commands.slash_command(name="sync_units", description="🔄 Принудительно обновить справочник персонажей")
    async def sync_units(self, inter: disnake.ApplicationCommandInteraction):
        await inter.response.defer()
        try:
            total = await self._do_units_synchronization()
            await inter.edit_original_response(f"✅ Справочник успешно обновлен вручную! Записано юнитов: **{total}**.")
        except Exception as e:
            await inter.edit_original_response(f"❌ Ошибка при обновлении: `{e}`")

    # =====================================================================
    # СЛЭШ-КОМАНДА: /task_add (ПОСТАВИТЬ ЗАДАЧУ НА ПРОКАЧКУ)
    # =====================================================================
    @commands.slash_command(name="task_add", description="🎯 Поставить задачу игроку на прокачку юнита")
    async def task_add(
        self,
        inter: disnake.ApplicationCommandInteraction,
        player: str = commands.Param(desc="Выберите игрока", autocomplete=autocomplete_players),
        unit: str = commands.Param(desc="Выберите персонажа/корабль", autocomplete=units_autocomplete),
        target_type: str = commands.Param(
            desc="Критерий прокачки", 
            choices=[
                disnake.OptionChoice(name="⭐ Звёзды (1-7)", value="stars"),
                disnake.OptionChoice(name="♦️ Реликвия (0-9)", value="relic"),
                disnake.OptionChoice(name="🧬 Омикрон", value="omicron")
            ]
        ),
        target_value: str = commands.Param(desc="Целевое значение (например: 7 для звезд, 5 для R5, 1 для омикрона)"),
        days_to_complete: int = commands.Param(desc="Сколько дней даётся на выполнение задачи?", min_value=1, max_value=365)
    ):
        await inter.response.defer()

        guild_id = guild_resolver.resolve_guild_id(inter.author)
        if guild_id is None:
            await inter.edit_original_response("❌ Не удалось определить, к какой гильдии вы относитесь.")
            return

        cache = self.bot.guild_roster_caches.get(guild_id, {})
        ally_code = cache.get(player)

        if not ally_code:
            row = database.get_user_mapping_for_name(guild_id, player)
            if row:
                ally_code = row[0]

        if not ally_code:
            await inter.edit_original_response("❌ Ошибка: Не удалось определить Ally Code выбранного игрока.")
            return

        player_name = cache.get(ally_code, player)

        unit_match = re.search(r"\[(.*?)\]", unit)
        base_id = unit_match.group(1) if unit_match else unit.strip().upper()

        unit_name = database.get_game_unit_name(base_id)
        if not unit_name:
            await inter.edit_original_response(f"❌ Юнит `{base_id}` не найден в справочнике.")
            return

        deadline_date = (datetime.now() + timedelta(days=days_to_complete)).strftime("%Y-%m-%d")

        database.add_task(ally_code, base_id, target_type, target_value, deadline_date, str(inter.author.id), guild_id=guild_id)

        readable_types = {"stars": "Звёзды ⭐", "relic": "Реликвия ♦️", "omicron": "Омикрон 🧬"}
        embed = disnake.Embed(
            title="🎯 Задача на прокачку успешно поставлена",
            description=f"**Игрок:** {player_name} ({ally_code})\n"
                        f"**Юнит:** {unit_name}\n\n"
                        f"📊 **Цель:** {readable_types[target_type]} -> **{target_value}**\n"
                        f"⏱ **Срок:** {days_to_complete} дней (до {deadline_date})",
            color=disnake.Color.blue()
        )
        await inter.edit_original_response(embed=embed)


def setup(bot):
    bot.add_cog(TasksCog(bot))