import disnake
from disnake.ext import commands, tasks
from datetime import datetime, timedelta
import asyncio
import database
import guild_resolver

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
    guild_id = guild_resolver.resolve_guild_id(inter.author)
    if guild_id is None:
        return []
    cache = inter.bot.guild_roster_caches.get(guild_id)
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
    def __init__(self, ally_code, player_name, warns_list, guild_id=1):
        super().__init__(timeout=60)
        options = []
        for i, (cat, subcat, d_str, comment) in enumerate(warns_list[:25]):
            if comment:
                label = f"{d_str} - {cat}: {subcat} ({comment})"[:100]
            else:
                label = f"{d_str} - {cat}: {subcat}"[:100]

            value = f"{i}|{cat}|{subcat}|{d_str}"
            options.append(disnake.SelectOption(label=label, value=value))
        self.add_item(UnwarnSelect(options, ally_code, player_name, warns_list, guild_id))

class UnwarnSelect(disnake.ui.Select):
    def __init__(self, options, ally_code, player_name, warns_list, guild_id=1):
        self.ally_code = ally_code
        self.player_name = player_name
        self.guild_id = guild_id
        super().__init__(placeholder="Выберите конкретное нарушение для удаления...", options=options)

    async def callback(self, inter: disnake.MessageInteraction):
        _, cat, subcat, d_str = self.values[0].split("|", 3)
        database.remove_warn(self.ally_code, cat, subcat, d_str, guild_id=self.guild_id)

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

    # --- ФОНОВАЯ ЗАДАЧА С ДИНАМИЧЕСКИМ ИНТЕРВАЛОМ И СТАТУСОМ [КЕШ] ---
    # Обновляет ростер КАЖДОЙ зарегистрированной гильдии по очереди, каждую —
    # в своём try/except (сбой Comlink для одной гильдии не должен срывать
    # обновление остальных). guild_roster_cache (легаси, без guild_id) держим
    # зеркалом гильдии id=1 — на неё пока завязаны не переведённые cogs.
    @tasks.loop(hours=1)
    async def update_roster_cache(self):
        guild_configs = database.get_all_guild_configs()
        if not guild_configs:
            print("⚠️ [Ростер] В реестре guilds нет ни одной гильдии — нечего обновлять")
            return

        any_success = False
        any_failure = False

        for guild_cfg in guild_configs:
            gid = guild_cfg["id"]
            gname = guild_cfg["name"]
            print(f"🔄 [{gname}] Запуск безопасного обновления состава...")
            try:
                print(f"🔎 [{gname}] Шаг 1: Запрос профиля игрока для Ally Code: {guild_cfg['ally_code']}...")
                player_data = self.bot.comlink.get_player(allycode=str(guild_cfg["ally_code"]))

                swgoh_guild_id = player_data.get("guildId")
                if not swgoh_guild_id:
                    print(f"⚠️ [{gname}] Шаг 2 ФЕЙЛ: У игрока-зацепки нет guildId (он не состоит в гильдии)")
                    any_failure = True
                    continue
                print(f"✅ [{gname}] Шаг 2: Guild ID успешно получен: {swgoh_guild_id}")

                # Самовосстановление swgoh_guild_id: раньше эта колонка нигде не
                # заполнялась, из-за чего ТБ-подсистема (guild_events.py) молча не
                # работала — мы и так уже получили актуальный ID выше, просто
                # сохраняем его, если он расходится с тем, что в БД.
                if str(guild_cfg.get("swgoh_guild_id") or "") != str(swgoh_guild_id):
                    database.update_guild_config(gid, swgoh_guild_id=str(swgoh_guild_id))

                print(f"🔎 [{gname}] Шаг 3: Запрос данных гильдии из comlink...")
                guild = self.bot.comlink.get_guild(guild_id=swgoh_guild_id)
                members = guild.get("guild", guild).get("member", [])
                print(f"✅ [{gname}] Шаг 3: Состав гильдии получен, найдено {len(members)} аккаунтов")

                print(f"🌐 [{gname}] Сбор детальных профилей игроков из сети...")
                temp_roster_data = []
                new_cache = {}

                for member in members:
                    p_id = member.get("playerId")
                    p_name = member.get("playerName", f"Игрок {p_id[:8]}")
                    a_code = str(member.get("allyCode", p_id))
                    member_level = member.get("memberLevel")

                    try:
                        prof = self.bot.comlink.get_player(player_id=p_id)
                        p_name = prof.get("name", p_name)
                        a_code = str(prof.get("allyCode", a_code))
                    except:
                        pass

                    new_cache[p_name] = a_code
                    temp_roster_data.append((a_code, a_code, p_name, member_level))
                    await asyncio.sleep(0.1)

                print(f"💾 [{gname}] Шаг 4: Мгновенное сохранение профилей в базу данных...")
                database.sync_guild_roster(gid, temp_roster_data)

                hybrid_cache = HybridCache(new_cache)
                self.bot.guild_roster_caches[gid] = hybrid_cache
                if gid == 1:
                    self.bot.guild_roster_cache = hybrid_cache  # легаси-зеркало, см. комментарий выше
                print(f"✅ [{gname}] Синхронизировано {len(new_cache)} игроков.")
                any_success = True

            except Exception as e:
                print(f"❌ [{gname}] Критическая ошибка на этапе выполнения функции: {e}")
                print(f"🚨 [{gname}] Аварийный режим: Попытка восстановить состав из локальной базы данных...")
                any_failure = True
                try:
                    rows = database.get_all_user_mappings(gid)
                    if rows:
                        cached_dict = {name: code for _, code, name in rows}
                        hybrid_cache = HybridCache(cached_dict)
                        self.bot.guild_roster_caches[gid] = hybrid_cache
                        if gid == 1:
                            self.bot.guild_roster_cache = hybrid_cache
                        print(f"⚠️ [{gname}] Восстановлено {len(cached_dict)} игроков из кеша БД. Бот продолжит работать на старых данных!")
                    else:
                        print(f"❌ [{gname}] Локальная база данных пуста. Восстановление невозможно.")
                except Exception as db_err:
                    print(f"❌ [{gname}] Не удалось прочитать БД для аварийного восстановления: {db_err}")

        if any_failure:
            if self.update_roster_cache.minutes != 5:
                print("⚙️ Есть гильдии со сбоем сети. Устанавливаем частый интервал проверки: 5 минут")
                self.update_roster_cache.change_interval(hours=0, minutes=5)
            await self.bot.change_presence(activity=disnake.Activity(
                type=disnake.ActivityType.watching,
                name="Следит за игроками AC [кеш]"
            ))
        else:
            if self.update_roster_cache.hours != 1:
                print("⚙️ Сеть восстановлена для всех гильдий. Устанавливаем штатный интервал обновления: 1 час")
                self.update_roster_cache.change_interval(hours=1, minutes=0)
            if any_success:
                await self.bot.change_presence(activity=disnake.Activity(
                    type=disnake.ActivityType.watching,
                    name="Следит за игроками AC"
                ))

    @update_roster_cache.before_loop
    async def before_loop(self):
        await self.bot.wait_until_ready()

    # --- ТВОЯ ОРИГИНАЛЬНАЯ КОМАНДА /WARN С 3 НАРУШЕНИЯМИ ЗА РАЗ ---
    @commands.slash_command(name="нарушение", description="Зафиксировать нарушение у игрока")
    async def warn(
        self,
        inter: disnake.ApplicationCommandInteraction,
        режим: str = commands.Param(description="Игровое событие, в котором произошло нарушение", choices=["ТБ", "ВГ", "Рейд"]),
        нарушение_1: str = commands.Param(description="Выберите основное нарушение", autocomplete=autocomplete_violations),
        игрок: str = commands.Param(default=None, description="Игрок из состава гильдии (или укажите тег ниже)", autocomplete=autocomplete_players),
        тег: disnake.User = commands.Param(default=None, description="Discord-тег игрока вместо имени (у игрока должна быть /регистрация)"),
        комментарий_1: str = commands.Param(description="Детали для первого нарушения (опционально)", default=None),
        нарушение_2: str = commands.Param(description="Второе нарушение (опционально)", default=None, autocomplete=autocomplete_violations),
        комментарий_2: str = commands.Param(description="Детали для второго нарушения (опционально)", default=None),
        нарушение_3: str = commands.Param(description="Третье нарушение (опционально)", default=None, autocomplete=autocomplete_violations),
        комментарий_3: str = commands.Param(description="Детали для третьего нарушения (опционально)", default=None),
        дата: str = commands.Param(description="Формат: ДД.ММ или ДД.ММ.ГГГГ. По умолчанию — сегодня", default=None)
    ):
        guild_id = guild_resolver.resolve_guild_id(inter.author)
        if guild_id is None:
            await inter.response.send_message("❌ Не удалось определить, к какой гильдии вы относитесь.", ephemeral=True)
            return

        if дата is None:
            final_date = datetime.now().strftime("%d.%m.%Y")
        else:
            дата = дата.strip()
            if len(дата) <= 5:
                final_date = f"{дата}.{datetime.now().year}"
            else:
                final_date = дата

        if тег is not None:
            registration = database.get_user_registration(str(тег.id), guild_id=guild_id)
            if not registration:
                await inter.response.send_message(
                    f"❌ {тег.mention} ещё не зарегистрирован(а) — попросите выполнить `/регистрация`, либо укажите игрока по имени.",
                    ephemeral=True,
                )
                return
            ally_code, игрок = registration
        elif игрок:
            cache = self.bot.guild_roster_caches.get(guild_id, {})
            if игрок not in cache:
                await inter.response.send_message("❌ Ошибка: Игрок не найден в составе гильдии.", ephemeral=True)
                return
            ally_code = cache[игрок]
        else:
            await inter.response.send_message("❌ Укажите игрока по имени или тегом.", ephemeral=True)
            return

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
            database.add_warn(ally_code, режим, v, final_date, c, guild_id=guild_id)

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
        игрок: str = commands.Param(description="Оставьте пустым для общей таблицы или выберите игрока для детального досье", default=None, autocomplete=autocomplete_players),
        тег: disnake.User = commands.Param(default=None, description="Discord-тег игрока вместо имени (у игрока должна быть /регистрация)"),
    ):
        await inter.response.defer(ephemeral=False)
        three_months_ago = datetime.now() - timedelta(days=90)

        guild_id = guild_resolver.resolve_guild_id(inter.author)
        if guild_id is None:
            await inter.followup.send("❌ Не удалось определить, к какой гильдии вы относитесь.", ephemeral=True)
            return

        if тег is not None or игрок:
            if тег is not None:
                registration = database.get_user_registration(str(тег.id), guild_id=guild_id)
                if not registration:
                    await inter.followup.send(
                        f"❌ {тег.mention} ещё не зарегистрирован(а) — попросите выполнить `/регистрация`, либо укажите игрока по имени.",
                        ephemeral=True,
                    )
                    return
                ally_code, actual_name = registration
            else:
                row = database.get_user_mapping_for_name(guild_id, игрок)

                if not row:
                    await inter.followup.send(f"❌ Игрок '{игрок}' не найден.", ephemeral=True)
                    return

                ally_code, actual_name = row
            rows = database.get_player_warns(ally_code, guild_id=guild_id)

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
            cache = self.bot.guild_roster_caches.get(guild_id)
            if not cache:
                await inter.followup.send("❌ Состав еще загружается. Попробуйте через минуту", ephemeral=True)
                return

            all_warns = database.get_all_warns(guild_id=guild_id)
            stats = {name: {"ТБ": 0, "ВГ": 0, "Рейд": 0, "Recent": 0} for name in cache.keys()}
            ally_map = {str(code): name for name, code in cache.items()}

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
        игрок: str = commands.Param(default=None, description="Игрок, у которого хотите аннулировать нарушение (или укажите тег ниже)", autocomplete=autocomplete_players),
        тег: disnake.User = commands.Param(default=None, description="Discord-тег игрока вместо имени (у игрока должна быть /регистрация)"),
    ):
        guild_id = guild_resolver.resolve_guild_id(inter.author)
        if guild_id is None:
            await inter.response.send_message("❌ Не удалось определить, к какой гильдии вы относитесь.", ephemeral=True)
            return

        if тег is not None:
            registration = database.get_user_registration(str(тег.id), guild_id=guild_id)
            if not registration:
                await inter.response.send_message(
                    f"❌ {тег.mention} ещё не зарегистрирован(а) — попросите выполнить `/регистрация`, либо укажите игрока по имени.",
                    ephemeral=True,
                )
                return
            ally_code, actual_name = registration
        elif игрок:
            row = database.get_user_mapping_for_name(guild_id, игрок)

            if not row:
                await inter.response.send_message("Игрок не найден в базе данных", ephemeral=True)
                return

            ally_code, actual_name = row
        else:
            await inter.response.send_message("❌ Укажите игрока по имени или тегом.", ephemeral=True)
            return

        player_warns = database.get_player_warns(ally_code, guild_id=guild_id)

        if not player_warns:
            await inter.response.send_message(f"У игрока **{actual_name}** нет нарушений для удаления", ephemeral=True)
            return

        view = UnwarnSelectView(ally_code, actual_name, player_warns, guild_id=guild_id)
        await inter.response.send_message(f"Выберите, какое нарушение игрока **{actual_name}** нужно удалить:", view=view, ephemeral=True)

def setup(bot):
    bot.add_cog(ViolationsCog(bot))