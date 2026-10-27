import disnake
from disnake.ext import commands, tasks
from datetime import datetime, timedelta
import asyncio
import database
import guild_resolver

# Твоя оригинальная структура нарушений
WARNS_STRUCTURE = {
    "ТБ": ["Не поставил взвод", "Не залил склад", "Поздно залил склад", "Не отбил БЗ", "Не отбил ОЗ"],
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

# --- КОМПОНЕНТЫ ИНТЕРАКТИВНОГО МЕНЮ УДАЛЕНИЯ ДЛЯ /UNWARN ---
class UnwarnSelectView(disnake.ui.View):
    def __init__(self, ally_code, player_name, warns_list, guild_id=1, panel=None):
        super().__init__(timeout=60)
        options = []
        for i, (warn_id, cat, subcat, d_str, comment) in enumerate(warns_list[:25]):
            if comment:
                label = f"{d_str} - {cat}: {subcat} ({comment})"[:100]
            else:
                label = f"{d_str} - {cat}: {subcat}"[:100]

            value = f"{i}|{cat}|{subcat}|{d_str}"
            options.append(disnake.SelectOption(label=label, value=value))
        self.add_item(UnwarnSelect(options, ally_code, player_name, warns_list, guild_id, panel))

class UnwarnSelect(disnake.ui.Select):
    def __init__(self, options, ally_code, player_name, warns_list, guild_id=1, panel=None):
        self.panel = panel
        self.ally_code = ally_code
        self.player_name = player_name
        self.guild_id = guild_id
        super().__init__(placeholder="Выберите конкретное нарушение для удаления...", options=options)

    async def callback(self, inter: disnake.MessageInteraction):
        _, cat, subcat, d_str = self.values[0].split("|", 3)
        database.remove_warn(self.ally_code, cat, subcat, d_str, guild_id=self.guild_id)

        await inter.response.edit_message(content="🔄 Изменения вносятся...", view=None)
        await inter.followup.send(f"🗑️ Нарушение за {d_str} ({cat}: {subcat}) у игрока **{self.player_name}** успешно удалено", ephemeral=False)
        if self.panel is not None:
            try:
                await self.panel.refresh()  # досье на панели /нарушения — без удалённого
            except disnake.HTTPException:
                pass


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
    #
    # Интервал (штатный, без сетевых сбоев) был раньше 1 час — сокращён до 15
    # минут после инцидента, когда кикнутый из гильдии офицер до часа
    # оставался с офицерским доступом (member_level в user_mapping, на
    # который опирается guild_resolver.resolve_access, не успевал
    # обновиться). См. также web/deps.py::_refresh_access — вторая половина
    # того же фикса, убирающая доверие к закэшированному в веб-сессии tier.
    @tasks.loop(minutes=15)
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
                player_data = await asyncio.to_thread(self.bot.comlink.get_player, allycode=str(guild_cfg["ally_code"]))

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
                guild = await asyncio.to_thread(
                    self.bot.comlink.get_guild, guild_id=swgoh_guild_id, include_recent_guild_activity_info=True
                )
                members = guild.get("guild", guild).get("member", [])
                print(f"✅ [{gname}] Шаг 3: Состав гильдии получен, найдено {len(members)} аккаунтов")

                print(f"🌐 [{gname}] Сбор детальных профилей игроков из сети...")
                # Снимок предыдущего состава по playerId — если Comlink в этот раз не отдаст
                # allyCode/имя для кого-то (известный глюк "зануления" участников ростера),
                # подставляем последнее известное значение вместо мусора (сырого playerId).
                previous_by_pid = database.get_roster_by_player_id(gid)
                temp_roster_data = []
                activity_rows = []
                new_cache = {}

                for member in members:
                    p_id = member.get("playerId")
                    p_name = member.get("playerName") or ""
                    a_code = str(member.get("allyCode") or "")

                    for lookup_attempt in range(2):
                        try:
                            prof = await asyncio.to_thread(self.bot.comlink.get_player, player_id=p_id)
                            p_name = prof.get("name") or p_name
                            a_code = str(prof.get("allyCode") or a_code)
                            break
                        except Exception as e:
                            print(f"⚠️ [{gname}] get_player({p_id}) попытка {lookup_attempt + 1} не удалась: {e!r}")
                            if lookup_attempt == 0:
                                await asyncio.sleep(1)

                    if not a_code or not p_name:
                        prev_code, prev_name = previous_by_pid.get(p_id, (None, None))
                        if not a_code and prev_code:
                            a_code = prev_code
                            print(f"↩️ [{gname}] {p_id}: allyCode не пришёл, использован предыдущий {prev_code}")
                        if not p_name and prev_name:
                            p_name = prev_name
                        if not a_code:
                            a_code = p_id  # последний резерв — хотя бы не потерять строку
                        if not p_name:
                            p_name = f"Игрок {p_id[:8]}"

                    new_cache[p_name] = a_code
                    temp_roster_data.append((a_code, a_code, p_name, member.get("memberLevel"), p_id))
                    activity_rows.append((a_code, int(member.get("lastActivityTime") or 0) or None,
                                          int(member.get("guildJoinTime") or 0) or None))
                    await asyncio.sleep(0.1)

                print(f"💾 [{gname}] Шаг 4: Мгновенное сохранение профилей в базу данных...")
                # В потоке: архивация выбывших + запись в SQLite, которую параллельно пишет
                # веб — ожидание блокировки БД (до 5с) не должно вешать весь event loop.
                await asyncio.to_thread(database.sync_guild_roster, gid, temp_roster_data)
                await asyncio.to_thread(database.save_member_activity, gid, activity_rows)
                await self._resolve_departed_names(gid, gname)

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
            if self.update_roster_cache.minutes != 15:
                print("⚙️ Сеть восстановлена для всех гильдий. Устанавливаем штатный интервал обновления: 15 минут")
                self.update_roster_cache.change_interval(hours=0, minutes=15)
            if any_success:
                await self.bot.change_presence(activity=disnake.Activity(
                    type=disnake.ActivityType.watching,
                    name="Следит за игроками AC"
                ))

    async def _resolve_departed_names(self, gid: int, gname: str):
        """Выбывшие, чьё имя не нашлось ни в прошлом составе, ни в /регистрации (обычно
        ушедшие ещё до появления архива выбывших) — добираем имя из Comlink, по несколько
        за проход, чтобы не растягивать синк ростера."""
        for ally_code in database.get_departed_without_name(gid, limit=10):
            try:
                prof = await asyncio.to_thread(self.bot.comlink.get_player, allycode=ally_code)
                name = prof.get("name")
                if name:
                    database.set_departed_player_name(gid, ally_code, name)
            except Exception as e:
                print(f"⚠️ [{gname}] Не удалось узнать имя выбывшего {ally_code}: {e!r}")
            await asyncio.sleep(0.1)

    @update_roster_cache.before_loop
    async def before_loop(self):
        await self.bot.wait_until_ready()

    # ------------------ /нарушения — панель (2026-10-14) ------------------
    # Одна команда вместо добавить/список/снять: режим и нарушения — выпадашками,
    # игрок — параметром с подсказками или выпадашками (А–…/…–Я), запись — окно с датой
    # и комментариями. Факт нарушения, как и раньше, публикуется в канал открыто.
    @commands.slash_command(name="нарушения", description="Нарушения гильдии: таблица, досье игрока, добавить и снять")
    async def violations_panel(
        self,
        inter: disnake.ApplicationCommandInteraction,
        игрок: str = commands.Param(default=None, description="Необязательно: начните вводить ник — панель откроется на игроке",
                                    autocomplete=autocomplete_players),
        тег: disnake.User = commands.Param(default=None, description="Или Discord-тег игрока (у него должна быть /регистрация)"),
    ):
        guild_id = await guild_resolver.require_feature(inter, "violations")
        if guild_id is None:
            return
        cache = self.bot.guild_roster_caches.get(guild_id) or {}
        if not cache:
            await inter.response.send_message("❌ Состав ещё загружается. Попробуйте через минуту.", ephemeral=True)
            return
        players = sorted(((name, code) for name, code in dict(cache).items()), key=lambda p: p[0].lower())
        panel = ViolationsPanel(self, guild_id, inter, players)
        if тег is not None:
            registration = database.get_user_registration(str(тег.id), guild_id=guild_id)
            if not registration:
                await inter.response.send_message(
                    f"❌ {тег.mention} ещё не зарегистрирован(а) — попросите выполнить `/регистрация` или выберите игрока по имени.",
                    ephemeral=True)
                return
            panel.ally_code = registration[0]
        elif игрок:
            panel.ally_code = dict(cache).get(игрок) or next(
                (code for name, code in players if игрок.strip().lower() in name.lower()), None)
        await inter.response.send_message(panel.render(), view=panel.build(), ephemeral=True,
                                          allowed_mentions=disnake.AllowedMentions.none())

    def guild_table(self, guild_id: int, cache: dict) -> str | None:
        three_months_ago = datetime.now() - timedelta(days=90)
        stats = {name: {"ТБ": 0, "ВГ": 0, "Рейд": 0, "Recent": 0} for name in cache.keys()}
        ally_map = {str(code): name for name, code in cache.items()}
        for ally_code, cat, d_str in database.get_all_warns(guild_id=guild_id):
            name = ally_map.get(str(ally_code))
            if not name:
                continue
            try:
                if datetime.strptime(d_str, "%d.%m.%Y") >= three_months_ago:
                    stats[name]["Recent"] += 1
                    if cat in stats[name]:
                        stats[name][cat] += 1
            except ValueError:
                pass
        rows = sorted(((n, d) for n, d in stats.items() if d["Recent"]), key=lambda item: item[1]["Recent"], reverse=True)
        if not rows:
            return None
        table = "📋 НАРУШЕНИЯ ЗА ПОСЛЕДНИЕ 90 ДНЕЙ\n" + f"{'Игрок':<16} | ТБ | ВГ | Рд | Всего\n" + "-" * 40 + "\n"
        for name, d in rows:
            prefix = "🚨" if d["Recent"] >= self.bot.n_limit else "  "
            table += f"{prefix}{name[:14]:<14} | {d['ТБ']:<2} | {d['ВГ']:<2} | {d['Рейд']:<2} | {d['Recent']}\n"
        return table

    def dossier(self, guild_id: int, ally_code: str, name: str) -> tuple[str, int, int, list]:
        """(текст истории, всего, за 90 дней, строки нарушений)."""
        three_months_ago = datetime.now() - timedelta(days=90)
        rows = database.get_player_warns(ally_code, guild_id=guild_id)
        recent = 0
        lines = []
        for _warn_id, cat, subcat, d_str, comment in rows:
            try:
                if datetime.strptime(d_str, "%d.%m.%Y") >= three_months_ago:
                    recent += 1
            except ValueError:
                pass
            lines.append(f"• `{d_str}` — **{cat}** — {subcat}" + (f" | *📝 {comment}*" if comment else ""))
        return "\n".join(lines), len(rows), recent, rows


class ViolationsPanel(disnake.ui.View):
    """Строки: 0 — режим, 1 — нарушения (до 3), 2-3 — игрок (по 25), 4 — кнопки."""
    def __init__(self, cog, guild_id: int, inter: disnake.ApplicationCommandInteraction, players: list):
        super().__init__(timeout=840)  # токен ответа на команду живёт 15 минут
        self.cog = cog
        self.guild_id = guild_id
        self.inter = inter
        self.author_id = inter.author.id
        self.players = players            # [(name, ally_code)]
        self.mode = "ТБ"
        self.violations = []
        self.ally_code = None
        self.page = 0

    async def interaction_check(self, interaction: disnake.MessageInteraction) -> bool:
        return interaction.author.id == self.author_id

    def _name(self) -> str | None:
        return next((n for n, c in self.players if c == self.ally_code), None)

    def render(self) -> str:
        if self.ally_code:
            name = self._name() or self.ally_code
            history, total, recent, _rows = self.cog.dossier(self.guild_id, self.ally_code, name)
            mark = " 🚨" if recent >= self.cog.bot.n_limit else ""
            text = f"📊 **Досье: {name}** — всего {total}, за 90 дней {recent}{mark}\n" + (history or "😇 Нарушений нет.")
        else:
            table = self.cog.guild_table(self.guild_id, dict(self.players))
            text = f"```text\n{table}\n```" if table else "😇 Нарушений в гильдии за последние 3 месяца нет."
        hint = ("\n-# Выберите игрока, режим и нарушения → «Зафиксировать». Без игрока — таблица гильдии.")
        return (text[:1800] + "\n…" if len(text) > 1800 else text) + hint

    def build(self):
        self.clear_items()
        mode_select = disnake.ui.Select(placeholder="Режим", row=0, options=[
            disnake.SelectOption(label=m, value=m, default=m == self.mode) for m in WARNS_STRUCTURE])
        mode_select.callback = self._on_mode
        self.add_item(mode_select)

        options = WARNS_STRUCTURE[self.mode]
        viol_select = disnake.ui.Select(
            placeholder="Нарушения (можно несколько, до 3)", row=1, min_values=0, max_values=min(3, len(options)),
            options=[disnake.SelectOption(label=v, value=v, default=v in self.violations) for v in options])
        viol_select.callback = self._on_violations
        self.add_item(viol_select)

        page = self.players[self.page * 50:(self.page + 1) * 50]
        for i in range(2):
            chunk = page[i * 25:(i + 1) * 25]
            if not chunk:
                break
            select = disnake.ui.Select(
                placeholder=f"Игрок ({chunk[0][0][:1].upper()}–{chunk[-1][0][:1].upper()})", row=2 + i,
                options=[disnake.SelectOption(label=n[:100], value=c, default=c == self.ally_code) for n, c in chunk])
            select.callback = self._on_player
            self.add_item(select)

        for label, style, callback, disabled in (
            ("Зафиксировать", disnake.ButtonStyle.danger, self._on_add, not (self.ally_code and self.violations)),
            ("Снять нарушение", disnake.ButtonStyle.secondary, self._on_remove, not self.ally_code),
            ("Показать всем", disnake.ButtonStyle.secondary, self._on_publish, False),
        ):
            button = disnake.ui.Button(label=label, style=style, row=4, disabled=disabled)
            button.callback = callback
            self.add_item(button)
        if self.ally_code:
            reset = disnake.ui.Button(label="К таблице гильдии", style=disnake.ButtonStyle.secondary, row=4)
            reset.callback = self._on_reset
            self.add_item(reset)
        if len(self.players) > 50:
            nxt = disnake.ui.Button(label="Игроки ▶" if (self.page + 1) * 50 < len(self.players) else "◀ Игроки", row=4)
            nxt.callback = self._on_page
            self.add_item(nxt)
        return self

    async def refresh(self, interaction: disnake.MessageInteraction | None = None):
        kwargs = dict(content=self.render(), view=self.build(), allowed_mentions=disnake.AllowedMentions.none())
        if interaction is not None:
            await interaction.response.edit_message(**kwargs)
        else:
            await self.inter.edit_original_response(**kwargs)

    async def _on_mode(self, interaction):
        self.mode = interaction.values[0]
        self.violations = []
        await self.refresh(interaction)

    async def _on_violations(self, interaction):
        self.violations = list(interaction.values)
        await self.refresh(interaction)

    async def _on_player(self, interaction):
        self.ally_code = interaction.values[0]
        await self.refresh(interaction)

    async def _on_reset(self, interaction):
        self.ally_code = None
        await self.refresh(interaction)

    async def _on_page(self, interaction):
        self.page = self.page + 1 if (self.page + 1) * 50 < len(self.players) else 0
        await self.refresh(interaction)

    async def _on_add(self, interaction):
        await interaction.response.send_modal(AddViolationModal(self))

    async def _on_remove(self, interaction):
        warns = database.get_player_warns(self.ally_code, guild_id=self.guild_id)
        if not warns:
            await interaction.response.send_message(f"У игрока **{self._name()}** нет нарушений.", ephemeral=True)
            return
        await interaction.response.send_message(
            f"Выберите нарушение игрока **{self._name()}**, которое нужно удалить:",
            view=UnwarnSelectView(self.ally_code, self._name(), warns, guild_id=self.guild_id, panel=self), ephemeral=True)

    async def _on_publish(self, interaction):
        if self.ally_code:
            history, total, recent, _rows = self.cog.dossier(self.guild_id, self.ally_code, self._name())
            embed = disnake.Embed(title=f"📊 Досье: {self._name()}",
                                  color=disnake.Color.red() if recent >= self.cog.bot.n_limit else disnake.Color.orange())
            embed.add_field(name="Всего нарушений", value=str(total), inline=True)
            embed.add_field(name="За последние 90 дней", value=str(recent), inline=True)
            embed.add_field(name="История:", value=(history[:1000] if len(history) <= 1000 else history[:997] + "...") or "—", inline=False)
            await interaction.response.send_message(embed=embed)
            return
        table = self.cog.guild_table(self.guild_id, dict(self.players))
        await interaction.response.send_message(f"```text\n{table}\n```" if table else "😇 Нарушений в гильдии за последние 3 месяца нет.")


class AddViolationModal(disnake.ui.Modal):
    def __init__(self, panel: ViolationsPanel):
        self.panel = panel
        components = [disnake.ui.TextInput(label="Дата (ДД.ММ или ДД.ММ.ГГГГ)", custom_id="date", max_length=10,
                                           value=datetime.now().strftime("%d.%m.%Y"))]
        for i, violation in enumerate(panel.violations):
            components.append(disnake.ui.TextInput(label=f"Комментарий: {violation}"[:45], custom_id=f"c{i}",
                                                   required=False, max_length=300))
        super().__init__(title=f"Нарушение: {panel._name()}"[:45], components=components)

    async def callback(self, interaction: disnake.ModalInteraction):
        panel = self.panel
        date = interaction.text_values["date"].strip()
        final_date = f"{date}.{datetime.now().year}" if len(date) <= 5 else date
        try:
            datetime.strptime(final_date, "%d.%m.%Y")
        except ValueError:
            await interaction.response.send_message("❌ Дата должна быть в формате ДД.ММ или ДД.ММ.ГГГГ.", ephemeral=True)
            return
        lines = []
        for i, violation in enumerate(panel.violations):
            comment = (interaction.text_values.get(f"c{i}") or "").strip() or None
            database.add_warn(panel.ally_code, panel.mode, violation, final_date, comment, guild_id=panel.guild_id)
            lines.append(f"• {violation}" + (f" | *📝 Комментарий: {comment}*" if comment else ""))
        await interaction.response.send_message(
            f"✅ **Нарушение успешно зафиксировано!**\n👤 **Игрок:** {panel._name()} (Код: {panel.ally_code})\n"
            f"📅 **Дата:** {final_date}\n🗂 **Режим:** {panel.mode}\n🛑 **Нарушения:**\n" + "\n".join(lines))
        panel.violations = []
        try:
            await panel.refresh()
        except disnake.HTTPException:
            pass  # панель старше 15 минут — нарушение уже записано


def setup(bot):
    bot.add_cog(ViolationsCog(bot))