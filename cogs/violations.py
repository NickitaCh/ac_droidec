import disnake
from disnake.ext import commands, tasks
from datetime import datetime, timedelta
import sqlite3
import asyncio
import database

# Твоя оригинальная структура нарушений
WARNS_STRUCTURE = {
    "ТБ": ["Не поставил взвод", "Не залил склад", "Не отбил БЗ", "Не отбил ОЗ"],
    "ВГ": ["Не поставил деф", "Поздно поставил деф (1 час)", "Не зашел в атаку", "Поздно зашел в атаку (12 часов)", "Ошибки в атаке", "Ошибки в дефе"],
    "Рейд": ["Не отбил рейд", "Поздно отбил рейд", "Не выполнил норматив"]
}

class HybridCache(dict):
    """
    Умный гибридный кеш. 
    Для команд нарушений работает как привычный {Имя: Код}, 
    но поддерживает метод .get(код), возвращая Имя для модуля задач.
    """
    def __init__(self, name_to_code):
        super().__init__(name_to_code)
        self.code_to_name = {str(v): k for k, v in name_to_code.items()}
        
    def get(self, key, default=None):
        if key in self:
            return self[key]
        if str(key) in self.code_to_name:
            return self.code_to_name[str(key)]
        return default

# --- ФУНКЦИИ АВТОЗАПОЛНЕНИЯ (AUTOCOMPLETE) ---
async def autocomplete_players(inter: disnake.ApplicationCommandInteraction, string: str):
    cache = inter.bot.guild_roster_cache
    if not cache:
        return ["⏳ Состав еще загружается, подождите..."]
    string = string.lower()
    return [name for name in cache.keys() if string in name.lower()][:25]

async def autocomplete_violations(inter: disnake.ApplicationCommandInteraction, string: str):
    chosen_mode = inter.filled_options.get("режим")
    if not chosen_mode:
        return ["⚠️ СНАЧАЛА выберите режим (ТБ, ВГ или Рейд)!"]
    if chosen_mode not in WARNS_STRUCTURE:
        return ["❌ Ошибка: Выбран неизвестный режим."]
        
    options = WARNS_STRUCTURE[chosen_mode]
    search_string = string.lower().strip()
    matched_options = [opt for opt in options if search_string in opt.lower()]
    
    if not matched_options:
        return options[:25]
        
    return matched_options[:25]

# --- КОМПОНЕНТЫ ИНТЕРАКТИВНОГО МЕНЮ УДАЛЕНИЯ ДЛЯ /UNWARN ---
class UnwarnSelectView(disnake.ui.View):
    def __init__(self, ally_code, player_name, warns_list):
        super().__init__(timeout=60)
        options = []
        for i, (cat, subcat, d_str, comment) in enumerate(warns_list[:25]):
            if comment:
                label = f"{d_str} - {cat}: {subcat} ({comment})"[:100]
            else:
                label = f"{d_str} - {cat}: {subcat}"[:100]
                
            value = f"{i}|{cat}|{subcat}|{d_str}"
            options.append(disnake.SelectOption(label=label, value=value))
        self.add_item(UnwarnSelect(options, ally_code, player_name, warns_list))

class UnwarnSelect(disnake.ui.Select):
    def __init__(self, options, ally_code, player_name, warns_list):
        self.ally_code = ally_code
        self.player_name = player_name  
        super().__init__(placeholder="Выберите конкретное нарушение для удаления...", options=options)

    async def callback(self, inter: disnake.MessageInteraction):
        _, cat, subcat, d_str = self.values[0].split("|", 3)
        database.remove_warn(self.ally_code, cat, subcat, d_str)
        
        await inter.response.edit_message(content="🔄 Изменения вносятся...", view=None)
        await inter.followup.send(f"🗑️ Нарушение за {d_str} ({cat}: {subcat}) у игрока **{self.player_name}** успешно удалено", ephemeral=False)


# =====================================================================
#                        ОСНОВНОЙ МОДУЛЬ НАРУШЕНИЙ
# =====================================================================
class ViolationsCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        # Твой оригинальный фоновый цикл обновлений
        self.update_roster_cache.start()

    def cog_unload(self):
        self.update_roster_cache.cancel()

    # --- ТВОЯ ФОНОВАЯ ЗАДАЧА С ДИНАМИЧЕСКИМ ИНТЕРВАЛОМ И СТАТУСОМ [КЕШ] ---
    @tasks.loop(hours=1)
    async def update_roster_cache(self):
        print("🔄 Запуск безопасного обновления состава...")
        try:
            print(f"🔎 Шаг 1: Запрос профиля игрока для Ally Code: {self.bot.ally_code}...")
            player_data = self.bot.comlink.get_player(allycode=str(self.bot.ally_code)) 
            
            guild_id = player_data.get("guildId")
            if not guild_id:
                print("⚠️ Шаг 2 ФЕЙЛ: У игрока-зацепки нет guildId (он не состоит в гильдии)")
                return
            print(f"✅ Шаг 2: Guild ID успешно получен: {guild_id}")

            print("🔎 Шаг 3: Запрос данных гильдии из comlink...")
            guild = self.bot.comlink.get_guild(guild_id=guild_id)
            members = guild.get("guild", guild).get("member", [])
            print(f"✅ Шаг 3: Состав гильдии получен, найдено {len(members)} аккаунтов")
            
            print("🌐 Сбор детальных профилей игроков из сети...")
            temp_roster_data = []
            new_cache = {}

            for member in members:
                p_id = member.get("playerId")
                p_name = member.get("playerName", f"Игрок {p_id[:8]}")
                a_code = str(member.get("allyCode", p_id))
                
                try:
                    prof = self.bot.comlink.get_player(player_id=p_id)
                    p_name = prof.get("name", p_name)
                    a_code = str(prof.get("allyCode", a_code))
                except:
                    pass 
                
                new_cache[p_name] = a_code
                temp_roster_data.append((a_code, a_code, p_name))
                await asyncio.sleep(0.1)

            print("💾 Шаг 4: Мгновенное сохранение профилей в базу данных...")
            conn = sqlite3.connect(database.DB_NAME)
            cursor = conn.cursor()
            cursor.execute('PRAGMA journal_mode=WAL;')
            cursor.execute('DELETE FROM user_mapping')
            cursor.executemany(
                'INSERT OR REPLACE INTO user_mapping (discord_id, ally_code, ingame_name) VALUES (?, ?, ?)',
                temp_roster_data
            )
            conn.commit()
            conn.close()
            
            # Сохраняем в умный гибридный кеш бота
            self.bot.guild_roster_cache = HybridCache(new_cache)
            print(f"✅ Синхронизировано {len(new_cache)} игроков.")
            
            if self.update_roster_cache.hours != 1:
                print("⚙️ Сеть восстановлена. Устанавливаем штатный интервал обновления: 1 час")
                self.update_roster_cache.change_interval(hours=1, minutes=0)

            await self.bot.change_presence(activity=disnake.Activity(
                type=disnake.ActivityType.watching, 
                name="Следит за игроками AC"
            ))
            
        except Exception as e:
            print(f"❌ Критическая ошибка на этапе выполнения функции: {e}")
            print("🚨 Аварийный режим: Попытка восстановить состав из локальной базы данных...")
            try:
                conn = sqlite3.connect(database.DB_NAME)
                cursor = conn.cursor()
                cursor.execute('SELECT ingame_name, ally_code FROM user_mapping')
                rows = cursor.fetchall()
                conn.close()
                
                if rows:
                    cached_dict = {name: code for name, code in rows}
                    self.bot.guild_roster_cache = HybridCache(cached_dict)
                    print(f"⚠️ Восстановлено {len(cached_dict)} игроков из кеша БД. Бот продолжит работать на старых данных!")
                    
                    if self.update_roster_cache.minutes != 5:
                        print("⚙️ Включен аварийный режим. Устанавливаем частый интервал проверки сети: 5 минут")
                        self.update_roster_cache.change_interval(hours=0, minutes=5)

                    await self.bot.change_presence(activity=disnake.Activity(
                        type=disnake.ActivityType.watching, 
                        name="Следит за игроками AC [кеш]"
                    ))
                else:
                    print("❌ Локальная база данных пуста. Восстановление невозможно.")
            except Exception as db_err:
                print(f"❌ Не удалось прочитать БД для аварийного восстановления: {db_err}")

    # --- ТВОЯ ОРИГИНАЛЬНАЯ КОМАНДА /WARN С 3 НАРУШЕНИЯМИ ЗА РАЗ ---
    @commands.slash_command(name="нарушение", description="Зафиксировать нарушение у игрока")
    async def warn(
        self,
        inter: disnake.ApplicationCommandInteraction,
        игрок: str = commands.Param(description="Выберите игрока из списка состава гильдии", autocomplete=autocomplete_players),
        режим: str = commands.Param(description="Игровое событие, в котором произошло нарушение", choices=["ТБ", "ВГ", "Рейд"]),
        нарушение_1: str = commands.Param(description="Выберите основное нарушение", autocomplete=autocomplete_violations),
        комментарий_1: str = commands.Param(description="Детали для первого нарушения (опционально)", default=None),
        нарушение_2: str = commands.Param(description="Второе нарушение (опционально)", default=None, autocomplete=autocomplete_violations),
        комментарий_2: str = commands.Param(description="Детали для второго нарушения (опционально)", default=None),
        нарушение_3: str = commands.Param(description="Третье нарушение (опционально)", default=None, autocomplete=autocomplete_violations),
        комментарий_3: str = commands.Param(description="Детали для третьего нарушения (опционально)", default=None),
        дата: str = commands.Param(description="Формат: ДД.ММ или ДД.ММ.ГГГГ. По умолчанию — сегодня", default=None)
    ):
        if дата is None:
            final_date = datetime.now().strftime("%d.%m.%Y")
        else:
            дата = дата.strip()
            if len(дата) <= 5: 
                final_date = f"{дата}.{datetime.now().year}"
            else: 
                final_date = дата

        if игрок not in self.bot.guild_roster_cache:
            await inter.response.send_message("❌ Ошибка: Игрок не найден в составе гильдии.", ephemeral=True)
            return
            
        ally_code = self.bot.guild_roster_cache[игрок]

        pairs = [
            {"violation": нарушение_1, "comment": комментарий_1},
            {"violation": нарушение_2, "comment": комментарий_2},
            {"violation": нарушение_3, "comment": комментарий_3}
        ]

        valid_pairs = []
        for pair in pairs:
            v = pair["violation"]
            if v and v in WARNS_STRUCTURE[режим]:
                valid_pairs.append(pair)

        if not valid_pairs:
            await inter.response.send_message("❌ Ошибка: Выбраны некорректные нарушения", ephemeral=True)
            return

        unique_pairs = []
        seen_violations = set()
        duplicate_warning = ""

        for pair in valid_pairs:
            v = pair["violation"]
            if v not in seen_violations:
                seen_violations.add(v)
                unique_pairs.append(pair)
            else:
                duplicate_warning = "⚠️ *Внимание: Одинаковые пункты нарушений были автоматически объединены*\n\n"

        for pair in unique_pairs:
            v = pair["violation"]
            c = pair["comment"].strip() if pair["comment"] else None
            database.add_warn(ally_code, режим, v, final_date, c)

        result_text = (
            f"{duplicate_warning}✅ **Нарушение успешно зафиксировано!**\n"
            f"👤 **Игрок:** {игрок} (Код: {ally_code})\n"
            f"📅 **Дата:** {final_date}\n"
            f"🗂 **Режим:** {режим}\n"
            f"🛑 **Нарушения:**\n"
        )
        
        formatted_list = []
        for pair in unique_pairs:
            v = pair["violation"]
            c = pair["comment"]
            if c:
                formatted_list.append(f"• {v} | *📝 Комментарий: {c.strip()}*")
            else:
                formatted_list.append(f"• {v}")
                
            result_text += "\n".join(formatted_list)
        await inter.response.send_message(result_text, ephemeral=False)

    # --- ТВОЯ ОРИГИНАЛЬНАЯ КОМАНДА /WARNS С ТЕКСТОВОЙ ТАБЛИЦЕЙ СТАТИСТИКИ И ЗНАЧКАМИ 🚨 ---
    @commands.slash_command(name="нарушения", description="Просмотр таблицы всех нарушений или досье конкретного игрока")
    async def warns(
        self,
        inter: disnake.ApplicationCommandInteraction, 
        игрок: str = commands.Param(description="Оставьте пустым для общей таблицы или выберите игрока для детального досье", default=None, autocomplete=autocomplete_players)
    ):
        await inter.response.defer(ephemeral=False)
        three_months_ago = datetime.now() - timedelta(days=90)

        if игрок:
            conn = sqlite3.connect(database.DB_NAME)
            cursor = conn.cursor()
            cursor.execute('SELECT ally_code, ingame_name FROM user_mapping WHERE ingame_name = ?', (игрок,))
            row = cursor.fetchone()
            conn.close()

            if not row:
                await inter.followup.send(f"❌ Игрок '{игрок}' не найден.", ephemeral=True)
                return

            ally_code, actual_name = row
            rows = database.get_player_warns(ally_code)

            if not rows:
                await inter.followup.send(f"😇 У игрока **{actual_name}** нет нарушений", ephemeral=False)
                return

            recent_count = 0
            details = ""
            for cat, subcat, d_str, comment in rows:
                try:
                    w_date = datetime.strptime(d_str, "%d.%m.%Y")
                    if w_date >= three_months_ago: recent_count += 1
                except ValueError: pass
                
                if comment:
                    details += f"• `{d_str}` — **{cat}** — {subcat} | *📝 {comment}*\n"
                else:
                    details += f"• `{d_str}` — **{cat}** — {subcat}\n"

            embed = disnake.Embed(title=f"📊 Досье: {actual_name}", color=disnake.Color.red() if recent_count >= self.bot.n_limit else disnake.Color.orange())
            embed.add_field(name="Всего нарушений", value=str(len(rows)), inline=True)
            embed.add_field(name="За последние 90 дней", value=str(recent_count), inline=True)
            embed.add_field(name="История:", value=details[:1000] if len(details) < 1000 else details[:997] + "...", inline=False)
            await inter.followup.send(embed=embed, ephemeral=False)

        else:
            if not self.bot.guild_roster_cache:
                await inter.followup.send("❌ Состав еще загружается. Попробуйте через минуту", ephemeral=True)
                return

            all_warns = database.get_all_warns()
            stats = {name: {"ТБ": 0, "ВГ": 0, "Рейд": 0, "Recent": 0} for name in self.bot.guild_roster_cache.keys()}
            ally_map = {str(code): name for name, code in self.bot.guild_roster_cache.items()}

            for ally_code, cat, d_str in all_warns:
                name = ally_map.get(str(ally_code))
                if not name: continue

                try:
                    w_date = datetime.strptime(d_str, "%d.%m.%Y")
                    if w_date >= three_months_ago: 
                        stats[name]["Recent"] += 1   
                        if cat in stats[name]: 
                            stats[name][cat] += 1    
                except: 
                    pass

            table = "📋 НАРУШЕНИЯ ЗА ПОСЛЕДНИЕ 90 ДНЕЙ\n"
            table += f"{'Игрок':<16} | ТБ | ВГ | Рд | Всего\n" + "-"*40 + "\n"
            has_offenders = False

            sorted_stats = sorted(
                stats.items(), 
                key=lambda item: item[1]["Recent"], 
                reverse=True
            )

            for name, data in sorted_stats:
                if data["Recent"] == 0: 
                    continue
                    
                has_offenders = True
                prefix = "🚨" if data["Recent"] >= self.bot.n_limit else "  "
                table += f"{prefix}{name[:14]:<14} | {data['ТБ']:<2} | {data['ВГ']:<2} | {data['Рейд']:<2} | {data['Recent']}\n"
            
            if not has_offenders:
                await inter.followup.send("😇 Нарушений в гильдии за последние 3 месяца не найдено", ephemeral=False)
                return
                
            await inter.followup.send(f"```text\n{table}\n```", ephemeral=False)

    # --- ТВОЯ ОРИГИНАЛЬНАЯ КОМАНДА /UNWARN С ИНТЕРАКТИВНЫМ ВЫБОРОМ ИЗ СПИСКА ---
    @commands.slash_command(name="снять_нарушение", description="Удалить конкретное нарушение у выбранного игрока")
    async def unwarn(
        self,
        inter: disnake.ApplicationCommandInteraction, 
        игрок: str = commands.Param(description="Выберите игрока, у которого хотите аннулировать нарушение", autocomplete=autocomplete_players)
    ):
        conn = sqlite3.connect(database.DB_NAME)
        cursor = conn.cursor()
        cursor.execute('SELECT ally_code, ingame_name FROM user_mapping WHERE ingame_name LIKE ?', (игрок,))
        row = cursor.fetchone()
        conn.close()

        if not row:
            await inter.response.send_message("Игрок не найден в базе данных", ephemeral=True)
            return

        ally_code, actual_name = row
        player_warns = database.get_player_warns(ally_code)

        if not player_warns:
            await inter.response.send_message(f"У игрока **{actual_name}** нет нарушений для удаления", ephemeral=True)
            return

        view = UnwarnSelectView(ally_code, actual_name, player_warns)
        await inter.response.send_message(f"Выберите, какое нарушение игрока **{actual_name}** нужно удалить:", view=view, ephemeral=True)

def setup(bot):
    bot.add_cog(ViolationsCog(bot))