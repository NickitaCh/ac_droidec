import disnake
from disnake.ext import commands, tasks
import sqlite3
import re
from datetime import datetime, timedelta
import database
import aiohttp
# Напрямую импортируем готовую рабочую функцию автозаполнения игроков
from cogs.violations import autocomplete_players  

# =====================================================================
# АВТОКОМПЛИТЫ ДЛЯ КОМАНДЫ ПОСТАНОВКИ ЗАДАЧ (ВНЕ КЛАССА)
# =====================================================================
async def units_autocomplete(inter: disnake.ApplicationCommandInteraction, string: str):
    """Ищет персонажей/корабли в локальном справочнике game_units"""
    string = string.lower()
    conn = sqlite3.connect(database.DB_NAME)
    cursor = conn.cursor()
    
    # Ищем совпадения по названию или по BaseID персонажа
    cursor.execute("""
        SELECT base_id, cached_name FROM game_units 
        WHERE LOWER(cached_name) LIKE ? OR LOWER(base_id) LIKE ? 
        LIMIT 25
    """, (f"%{string}%", f"%{string}%"))
    rows = cursor.fetchall()
    conn.close()
    
    return [f"{name} [{bid}]" for bid, name in rows]


class TasksCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        # Запускаем фоновый аудит задач
        self.tasks_audit_loop.start()

    def cog_unload(self):
        self.tasks_audit_loop.cancel()

    # =====================================================================
    # СИНХРОНИЗАЦИЯ ПО API COMLINK 0.40.0 (POST /data)
    # =====================================================================
    async def _do_units_synchronization(self) -> int:
        """Стягивает справочник юнитов напрямую из Comlink через HTTP POST /data.
        Использует минималистичный payload для исключения внутренних ошибок сервера.
        """
        comlink_url = "http://localhost:3000"  # Дефолтное значение
        for attr in ['url', 'base_url', 'host', '_url']:
            if hasattr(self.bot.comlink, attr):
                val = getattr(self.bot.comlink, attr)
                if isinstance(val, str) and val.startswith('http'):
                    comlink_url = val
                    break
        
        # Получаем актуальную версию gamedata
        raw_version = None
        try:
            metadata = self.bot.comlink.get_metadata()
            raw_version = metadata.get('latestGamedataVersion')
        except Exception as e:
            print(f"⚠️ Не удалось получить версию через библиотеку: {e}. Пробуем HTTP...")
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.get(f"{comlink_url.rstrip('/')}/metadata", timeout=10) as m_resp:
                        if m_resp.status == 200:
                            m_data = await m_resp.json()
                            raw_version = m_data.get('latestGamedataVersion')
            except Exception as http_err:
                print(f"❌ Ошибка получения метаданных через HTTP: {http_err}")

        if not raw_version:
            raise Exception("Не удалось определить актуальную версию игры (version) для Comlink API.")

        endpoint = f"{comlink_url.rstrip('/')}/data"
        game_data = None
        
        # Перебираем два варианта версии: полный (0.40.1:ХЭШ) и чистый (только ХЭШ)
        versions_to_try = [raw_version]
        if ":" in raw_version:
            versions_to_try.append(raw_version.split(":", 1)[1])

        async with aiohttp.ClientSession() as session:
            for v in versions_to_try:
                print(f"📦 [Comlink API] Пробуем POST-запрос на {endpoint} с версией: {v}...")
                
                # Абсолютно чистый и минимальный payload, требуемый новой спецификацией
                payload = {
                    "enums": False,
                    "payload": {
                        "version": v
                    }
                }
                
                try:
                    async with session.post(endpoint, json=payload, timeout=60) as resp:
                        if resp.status == 200:
                            game_data = await resp.json()
                            print("✅ Справочник данных успешно получен от Comlink!")
                            break
                        else:
                            print(f"⚠️ Попытка с версией {v} вернула статус {resp.status}")
                except Exception as req_err:
                    print(f"⚠️ Ошибка запроса с версией {v}: {req_err}")

        if not game_data:
            raise Exception("Comlink API отклонил все варианты структуры запроса.")

        # Поддерживаем оба возможных формата ответа (новый 'units' и старый 'unitsList')
        units_list = game_data.get('units') or game_data.get('unitsList') or []
        if not units_list:
            raise Exception("В полученных данных отсутствует массив персонажей ('units'/'unitsList').")

        # Обработка системных ключей в читаемые имена
        units_to_db = {}
        for unit in units_list:
            if unit.get('baseId'):
                bid = unit['baseId']
                raw_name = unit.get('nameKey', bid)
                
                # Очищаем системные префиксы и суффиксы
                name = raw_name
                for prefix in ["UNIT_", "SHIP_"]:
                    if name.startswith(prefix):
                        name = name.replace(prefix, "")
                if name.endswith("_NAME"):
                    name = name.replace("_NAME", "")
                
                # Форматируем в нормальный вид: DARTH_VADER -> Darth Vader
                clean_name = name.replace("_", " ").title().strip()
                units_to_db[bid] = clean_name

        # Записываем всё собранное в базу данных SQLite
        conn = sqlite3.connect(database.DB_NAME)
        cursor = conn.cursor()
        
        for bid, name in units_to_db.items():
            cursor.execute("""
                INSERT OR REPLACE INTO game_units (base_id, cached_name)
                VALUES (?, ?)
            """, (bid, name))
            
        conn.commit()
        conn.close()
        return len(units_to_db)

    # =====================================================================
    # ФОНОВЫЙ АУДИТ ЗАДАЧ (КАЖДЫЙ ЧАС)
    # =====================================================================
    @tasks.loop(hours=1)
    async def tasks_audit_loop(self):
        """Ежечасная автоматическая проверка выполнения задач через Comlink"""
        print("🔍 Запуск ежечасного аудита задач на прокачку...")
        
        conn = sqlite3.connect(database.DB_NAME)
        cursor = conn.cursor()
        
        cursor.execute("""
            SELECT task_id, ally_code, base_id, target_type, target_value, deadline 
            FROM tasks 
            WHERE status = 'ACTIVE'
        """)
        active_tasks = cursor.fetchall()
        
        print(f"📊 Аудит: нашел в базе {len(active_tasks)} active tasks.")

        if not active_tasks:
            conn.close()
            return

        now = datetime.now()

        for task in active_tasks:
            task_id, ally_code, base_id, target_type, target_value, deadline_str = task
            
            try:
                deadline = datetime.strptime(deadline_str, "%Y-%m-%d")
            except ValueError:
                continue

            if now.date() > deadline.date():
                cursor.execute("UPDATE tasks SET status = 'FAILED' WHERE task_id = ?", (task_id,))
                conn.commit()
                player_name = self.bot.guild_roster_cache.get(ally_code, f"Игрок [{ally_code}]")
                print(f"⏰ Срок задачи #{task_id} для {player_name} по юниту {base_id} истёк.")
                continue

            try:
                player_data = self.bot.comlink.get_player(ally_code)
                roster = player_data.get('rosterUnit') or player_data.get('roster')
                if not roster:
                    continue
            except Exception as e:
                print(f"⚠️ Аудит не смог достучаться до Comlink для {ally_code}: {e}")
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
                cursor.execute("UPDATE tasks SET status = 'COMPLETED' WHERE task_id = ?", (task_id,))
                conn.commit()
                player_name = self.bot.guild_roster_cache.get(ally_code, f"Игрок [{ally_code}]")
                print(f"🎉 Задача #{task_id} ВЫПОЛНЕНА игроком {player_name}!")

        conn.close()

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

        if not self.bot.guild_roster_cache:
            ally_code = None
        else:
            ally_code = self.bot.guild_roster_cache.get(player)

        if not ally_code:
            conn = sqlite3.connect(database.DB_NAME)
            cursor = conn.cursor()
            cursor.execute('SELECT ally_code FROM user_mapping WHERE ingame_name = ?', (player,))
            row = cursor.fetchone()
            conn.close()
            if row:
                ally_code = row[0]

        if not ally_code:
            await inter.edit_original_response("❌ Ошибка: Не удалось определить Ally Code выбранного игрока.")
            return
            
        player_name = self.bot.guild_roster_cache.get(ally_code, player) if self.bot.guild_roster_cache else player

        unit_match = re.search(r"\[(.*?)\]", unit)
        base_id = unit_match.group(1) if unit_match else unit.strip().upper()

        conn = sqlite3.connect(database.DB_NAME)
        cursor = conn.cursor()
        cursor.execute("SELECT cached_name FROM game_units WHERE base_id = ?", (base_id,))
        unit_row = cursor.fetchone()
        
        if not unit_row:
            conn.close()
            await inter.edit_original_response(f"❌ Юнит `{base_id}` не найден в справочнике.")
            return
            
        unit_name = unit_row[0]

        deadline_date = (datetime.now() + timedelta(days=days_to_complete)).strftime("%Y-%m-%d")
        created_at = datetime.now().strftime("%Y-%m-%d %H:%M")

        cursor.execute("""
            INSERT INTO tasks (ally_code, base_id, target_type, target_value, deadline, status, created_by, date_created)
            VALUES (?, ?, ?, ?, ?, 'ACTIVE', ?, ?)
        """, (ally_code, base_id, target_type, target_value, deadline_date, inter.author.id, created_at))
        
        conn.commit()
        conn.close()

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