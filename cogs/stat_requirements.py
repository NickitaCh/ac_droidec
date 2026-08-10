import asyncio
import math
import re
import sqlite3

import disnake
from disnake.ext import commands, tasks

import database
import stat_engine
from cogs.violations import autocomplete_players
from cogs.tasks import units_autocomplete
from cogs.datacron_requirements import (
    PRIORITY_REQUIRED,
    PRIORITY_LABELS,
    PRIORITY_CHOICES,
    PRIORITY_EMOJI,
    DATACRON_LIST_COLOR,
    DATACRON_CHECK_COLOR_FULL,
    DATACRON_CHECK_COLOR_PARTIAL,
    DATACRON_CHECK_COLOR_NONE,
    _lines_to_embeds,
)

# Полный список названий статов у StatCalc (swgoh_comlink/helpers/_stat_data.py, таблица
# STATS) шире — здесь только те, что реально фигурируют в гильдийских билд-требованиях.
# "Relic" — не настоящий стат StatCalc, спец-значение для требования к уровню реликвии.
STAT_CHOICES = [
    disnake.OptionChoice(name="Relic (уровень реликвии)", value="Relic"),
    disnake.OptionChoice(name="Health", value="Health"),
    disnake.OptionChoice(name="Protection", value="Protection"),
    disnake.OptionChoice(name="Speed", value="Speed"),
    disnake.OptionChoice(name="Physical Damage", value="Physical Damage"),
    disnake.OptionChoice(name="Special Damage", value="Special Damage"),
    disnake.OptionChoice(name="Armor", value="Armor"),
    disnake.OptionChoice(name="Resistance", value="Resistance"),
    disnake.OptionChoice(name="Potency", value="Potency"),
    disnake.OptionChoice(name="Tenacity", value="Tenacity"),
    disnake.OptionChoice(name="Critical Damage", value="Critical Damage"),
    disnake.OptionChoice(name="Physical Critical Chance", value="Physical Critical Chance"),
    disnake.OptionChoice(name="Special Critical Chance", value="Special Critical Chance"),
    disnake.OptionChoice(name="Critical Avoidance", value="Critical Avoidance"),
    disnake.OptionChoice(name="Physical Critical Avoidance", value="Physical Critical Avoidance"),
    disnake.OptionChoice(name="Special Critical Avoidance", value="Special Critical Avoidance"),
    disnake.OptionChoice(name="Accuracy", value="Accuracy"),
    disnake.OptionChoice(name="Physical Accuracy", value="Physical Accuracy"),
    disnake.OptionChoice(name="Special Accuracy", value="Special Accuracy"),
    disnake.OptionChoice(name="Health Steal", value="Health Steal"),
    disnake.OptionChoice(name="Offense", value="Offense"),
    disnake.OptionChoice(name="Defense", value="Defense"),
]

OPERATOR_CHOICES = [
    disnake.OptionChoice(name=">=", value=">="),
    disnake.OptionChoice(name="<=", value="<="),
    disnake.OptionChoice(name="=", value="="),
]


# =====================================================================
# Мелкие хелперы (парсинг "Имя [BASE_ID]", форматирование, сравнение) —
# та же схема, что unit_match в cogs/tasks.py и _parse_leading_hash_int в datacron_requirements.py
# =====================================================================
def _parse_bracket_id(text: str) -> str:
    m = re.search(r"\[(.*?)\]", text or "")
    return m.group(1) if m else (text or "").strip()


def _parse_req_id(text: str):
    if not text:
        return None
    m = re.match(r"#(\d+)", text.strip())
    return int(m.group(1)) if m else None


def _unit_display_name(base_id: str) -> str:
    conn = sqlite3.connect(database.DB_NAME)
    cursor = conn.cursor()
    cursor.execute("SELECT cached_name FROM game_units WHERE base_id = ?", (base_id,))
    row = cursor.fetchone()
    conn.close()
    return row[0] if row else base_id


def _fmt_value(value: float) -> str:
    return f"{value:g}"


def _fmt_compact(value: float) -> str:
    """Сокращённая запись для ширины таблицы: 8472 -> '8,4к' (отбрасывание, не округление),
    8000 -> '8к'. Значения меньше 1000 (релик, скорость, потенция...) не сокращаются."""
    if abs(value) < 1000:
        return _fmt_value(value)
    truncated = math.trunc(value / 100) / 10
    text = f"{truncated:.1f}".replace(".", ",")
    if text.endswith(",0"):
        text = text[:-2]
    return text + "к"


def _compare(current: float, operator: str, threshold: float) -> bool:
    if operator == ">=":
        return current >= threshold
    if operator == "<=":
        return current <= threshold
    if operator == "=":
        return current == threshold
    return False


def _build_synthetic_unit(base_id: str, relic_level: int) -> dict:
    """Без модов/шмота конкретного игрока: макс. редкость/уровень/шестерня + бонус реликвии.
    Используется только когда команда вызвана без игрока (абстрактный лукап)."""
    return {
        "defId": base_id,
        "rarity": 7,
        "level": 85,
        "gear": 13,
        "equipped": [],
        "skills": [],
        "relic": {"currentTier": relic_level + 2},
    }


async def _fetch_player_units(comlink, ally_code: str) -> dict:
    """base_id -> сырой rosterUnit из comlink.get_player. Блокирующий сетевой вызов — через to_thread."""
    player_data = await asyncio.to_thread(comlink.get_player, allycode=str(ally_code))
    roster = player_data.get("rosterUnit") or player_data.get("roster") or []
    units = {}
    for u in roster:
        base_id = u.get("baseId") or (u.get("definitionId", "") or "").split(":")[0]
        if base_id:
            units[base_id] = u
    return units


async def _get_unit_for_player(bot, ally_code: str, base_id: str, force_refresh: bool):
    """Возвращает (unit_dict, updated_at) либо (None, None)."""
    if force_refresh:
        units = await _fetch_player_units(bot.comlink, ally_code)
        if units:
            database.upsert_player_units(ally_code, units)
        unit = units.get(base_id)
        if unit:
            return unit, "только что"
    cached = database.get_player_unit(ally_code, base_id)
    if cached:
        return cached
    return None, None


def _build_table(headers: list, table_rows: list) -> str:
    """Моноширинная таблица в code-block'е (Discord embed'ы не умеют настоящие таблицы)."""
    widths = [len(h) for h in headers]
    for row in table_rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(cell))

    def fmt_row(cells):
        return "  ".join(cell.ljust(widths[i]) for i, cell in enumerate(cells))

    lines = [fmt_row(headers), fmt_row(["-" * w for w in widths])]
    lines.extend(fmt_row(row) for row in table_rows)
    return "```\n" + "\n".join(lines) + "\n```"


def _stat_label(stat_name: str, priority: str) -> str:
    label = "Реликвия" if stat_name == "Relic" else stat_name
    return f"(опц.) {label}" if priority == "optional" else label


async def _evaluate_character(bot, plate_name: str, base_id: str, ally_code, force_refresh: bool, relic_param, player_label):
    """Возвращает (char_name, block, matched, total, updated_at) для одного персонажа плейта —
    block уже отформатирован как таблица (моноширинный code-block), готовая к вставке в embed.
    Возвращает None, если для этого персонажа нет сохранённых требований (пропускается в отчёте)."""
    rows = database.get_stat_requirements(plate_name, base_id)
    if not rows:
        return None

    char_name = _unit_display_name(base_id)
    relic_reqs = [r for r in rows if r[3] == "Relic"]
    required_relic = int(relic_reqs[0][5]) if relic_reqs else None
    updated_at = None
    matched = 0
    total = 0
    comments = [r[8] for r in rows if r[8]]

    if ally_code is not None:
        unit, updated_at = await _get_unit_for_player(bot, ally_code, base_id, force_refresh)
        if not unit:
            block = f"⚠️ нет юнита у игрока «{player_label}» (не открыт либо ещё не синхронизирован)"
            return char_name, block, 0, 0, None

        current_relic = stat_engine.get_current_relic_level(unit)
        current_values = dict(stat_engine.calc_final_stats(bot.stat_calc, unit))
        current_values["Relic"] = current_relic

        show_projection = required_relic is not None and required_relic > current_relic
        target_relic = required_relic if show_projection else current_relic
        projected_values = None
        if show_projection:
            projected_unit = stat_engine.project_unit_relic(unit, target_relic)
            projected_values = dict(stat_engine.calc_final_stats(bot.stat_calc, projected_unit))
            projected_values["Relic"] = target_relic

        caption = f"Релик игрока: {current_relic}" + (f" → цель по плейту: {target_relic}" if show_projection else "")
        if show_projection:
            headers = ["Стат", "Сейчас", "Нужно", f"Релик {target_relic}", "Норма"]
        else:
            headers = ["Стат", "Сейчас", "Норма"]

        table_rows = []
        for row in rows:
            _, _, _, stat_name, operator, threshold, priority, raw_text, comment, _, _ = row
            label = _stat_label(stat_name, priority)
            req_cell = f"{operator} {_fmt_compact(threshold)}"
            cur_val = current_values.get(stat_name)
            if cur_val is None:
                table_rows.append([label, "нет данных", "—", "—", req_cell] if show_projection else [label, "нет данных", req_cell])
                continue
            total += 1
            cur_ok = _compare(cur_val, operator, threshold)
            if cur_ok:
                matched += 1
            cur_cell = f"{_fmt_compact(cur_val)} {'✅' if cur_ok else '❌'}"

            if not show_projection:
                table_rows.append([label, cur_cell, req_cell])
                continue

            needed_cell = "—"
            proj_cell = "—"
            if stat_name != "Relic":
                proj_val = projected_values.get(stat_name) if projected_values else None
                if proj_val is not None:
                    proj_ok = _compare(proj_val, operator, threshold)
                    proj_cell = f"{_fmt_compact(proj_val)} {'✅' if proj_ok else '❌'}"
                    # На сколько бы вырос стат к целевому релику (delta) — фиксированная величина
                    # при тех же модах/шмоте, не зависит от текущего значения (см. план фичи).
                    # needed = порог минус этот рост = сколько нужно ИМЕННО СЕЙЧАС, чтобы после
                    # апа реликвии стат дотянул до нормы плейта.
                    delta = proj_val - cur_val
                    needed_now = threshold - delta
                    needed_ok = _compare(cur_val, operator, needed_now)
                    needed_cell = f"{_fmt_compact(needed_now)} {'✅' if needed_ok else '❌'}"
            table_rows.append([label, cur_cell, needed_cell, proj_cell, req_cell])

        block = caption + "\n" + _build_table(headers, table_rows)
    else:
        unit = _build_synthetic_unit(base_id, relic_param)
        current_values = dict(stat_engine.calc_final_stats(bot.stat_calc, unit))
        current_values["Relic"] = relic_param

        headers = ["Стат", f"Релик {relic_param}", "Норма"]
        table_rows = []
        for row in rows:
            _, _, _, stat_name, operator, threshold, priority, raw_text, comment, _, _ = row
            label = _stat_label(stat_name, priority)
            req_cell = f"{operator} {_fmt_compact(threshold)}"
            cur_val = current_values.get(stat_name)
            if cur_val is None:
                table_rows.append([label, "нет данных", req_cell])
                continue
            total += 1
            cur_ok = _compare(cur_val, operator, threshold)
            if cur_ok:
                matched += 1
            cur_cell = f"{_fmt_compact(cur_val)} {'✅' if cur_ok else '❌'}"
            table_rows.append([label, cur_cell, req_cell])

        block = "⚠️ без модов/шмота игрока (только база + реликвия)\n" + _build_table(headers, table_rows)

    if comments:
        block += "\n" + "\n".join(f"💠 _{c}_" for c in comments)

    return char_name, block, matched, total, updated_at


# =====================================================================
# Автокомплиты (модульные функции — как autocomplete_players/autocomplete_datacron_*)
# =====================================================================
async def autocomplete_stat_plate(inter: disnake.ApplicationCommandInteraction, string: str):
    plates = database.get_all_stat_requirement_plates()
    if not plates:
        return ["❌ Список плейтов пуст."]
    search = string.lower().strip()
    return [p for p in plates if not search or search in p.lower()][:25]


async def autocomplete_stat_character(inter: disnake.ApplicationCommandInteraction, string: str):
    plate = inter.filled_options.get("плейт")
    if not plate:
        return ["⚠️ СНАЧАЛА выберите плейт!"]
    char_keys = database.get_stat_requirement_characters(plate)
    if not char_keys:
        return ["❌ У этого плейта нет сохранённых требований."]
    search = string.lower().strip()
    options = []
    for base_id in char_keys:
        label = f"{_unit_display_name(base_id)} [{base_id}]"
        if not search or search in label.lower():
            options.append(disnake.OptionChoice(name=label[:100], value=label))
    return options[:25]


async def autocomplete_stat_req_id(inter: disnake.ApplicationCommandInteraction, string: str):
    rows = database.get_all_stat_requirements()
    if not rows:
        return ["❌ Список требований пуст."]
    search = string.lower().strip()
    options = []
    for row in rows:
        req_id, plate_name, character_key, stat_name, operator, threshold, priority, raw_text, comment, _, _ = row
        char_name = _unit_display_name(character_key)
        label = f"#{req_id} [{PRIORITY_LABELS.get(priority, priority)}] {plate_name}: {char_name} {stat_name} {operator} {_fmt_value(threshold)}"
        if not search or search in label.lower():
            options.append(disnake.OptionChoice(name=label[:100], value=f"#{req_id}"))
    return options[:25]


# =====================================================================
#                       ОСНОВНОЙ МОДУЛЬ /статы
# =====================================================================
class StatRequirementsCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        if not hasattr(bot, "stat_calc"):
            bot.stat_calc = None
        self.stat_calc_loop.start()
        self.player_units_sync_loop.change_interval(hours=bot.PLAYER_STATS_SYNC_HOURS)
        self.player_units_sync_loop.start()

    def cog_unload(self):
        self.stat_calc_loop.cancel()
        self.player_units_sync_loop.cancel()

    @tasks.loop(hours=12)
    async def stat_calc_loop(self):
        try:
            self.bot.stat_calc = await stat_engine.build_stat_calc(self.bot.comlink)
            print("✅ [Статы] Калькулятор статов обновлён")
        except Exception as e:
            print(f"❌ [Статы] Ошибка обновления калькулятора статов: {e}")

    @stat_calc_loop.before_loop
    async def _before_stat_calc_loop(self):
        await self.bot.wait_until_ready()

    @tasks.loop(hours=6)
    async def player_units_sync_loop(self):
        if not self.bot.guild_roster_cache:
            return
        ally_codes = list(self.bot.guild_roster_cache.values())
        print(f"🔄 [Статы] Синхронизация ростеров игроков ({len(ally_codes)})...")
        synced = 0
        for ally_code in ally_codes:
            try:
                units = await _fetch_player_units(self.bot.comlink, ally_code)
                if units:
                    database.upsert_player_units(ally_code, units)
                    synced += 1
            except Exception as e:
                print(f"⚠️ [Статы] Не удалось обновить ростер {ally_code}: {e}")
            await asyncio.sleep(0.1)
        print(f"✅ [Статы] Синхронизировано ростеров: {synced}/{len(ally_codes)}")

    @player_units_sync_loop.before_loop
    async def _before_player_units_sync_loop(self):
        await self.bot.wait_until_ready()

    # ------------------ /статы_требования (офицеры) ------------------
    @commands.slash_command(name="статы_требования", description="Управление требованиями к статам персонажей по плейтам")
    @commands.has_any_role(1153753506772164629)
    async def stat_req(self, inter: disnake.ApplicationCommandInteraction):
        pass

    @stat_req.sub_command(name="добавить", description="Добавить требование к стату персонажа в плейт")
    async def stat_req_add(
        self,
        inter: disnake.ApplicationCommandInteraction,
        плейт: str = commands.Param(description="Название плейта (как в HotUtils, например AC_ALL)", autocomplete=autocomplete_stat_plate),
        персонаж: str = commands.Param(description="Персонаж", autocomplete=units_autocomplete),
        стат: str = commands.Param(description="Какой стат проверяем (или Relic для уровня реликвии)", choices=STAT_CHOICES),
        оператор: str = commands.Param(description="Оператор сравнения", choices=OPERATOR_CHOICES),
        значение: float = commands.Param(description="Пороговое значение"),
        приоритет: str = commands.Param(default=PRIORITY_REQUIRED, description="Приоритет требования", choices=PRIORITY_CHOICES),
        комментарий: str = commands.Param(default=None, description="Заметка"),
    ):
        base_id = _parse_bracket_id(персонаж)
        char_name = _unit_display_name(base_id)
        raw_text = f"{char_name} {стат} {оператор} {_fmt_value(значение)}"
        req_id = database.add_stat_requirement(
            плейт, base_id, стат, оператор, значение, приоритет, raw_text, комментарий, str(inter.author.id)
        )
        await inter.response.send_message(f"✅ Требование #{req_id} [{PRIORITY_LABELS[приоритет]}] добавлено: {raw_text}", ephemeral=True)

    @stat_req.sub_command(name="редактировать", description="Изменить требование к статам или удалить его")
    async def stat_req_edit(
        self,
        inter: disnake.ApplicationCommandInteraction,
        id: str = commands.Param(description="Требование для изменения", autocomplete=autocomplete_stat_req_id),
        значение: float = commands.Param(default=None, description="Новое пороговое значение"),
        оператор: str = commands.Param(default=None, description="Новый оператор", choices=OPERATOR_CHOICES),
        приоритет: str = commands.Param(default=None, description="Новый приоритет", choices=PRIORITY_CHOICES),
        комментарий: str = commands.Param(default=None, description="Новый комментарий"),
        удалить: bool = commands.Param(default=False, description="Удалить это требование вместо редактирования"),
    ):
        req_id = _parse_req_id(id)
        if req_id is None:
            await inter.response.send_message("❌ Некорректный id — выберите вариант из списка автодополнения.", ephemeral=True)
            return
        row = database.get_stat_requirement(req_id)
        if not row:
            await inter.response.send_message(f"❌ Требование #{req_id} не найдено.", ephemeral=True)
            return

        if удалить:
            database.delete_stat_requirement(req_id)
            await inter.response.send_message(f"🗑️ Требование #{req_id} удалено.", ephemeral=True)
            return

        _, plate_name, character_key, stat_name, cur_operator, cur_threshold, cur_priority, _raw_text, cur_comment, _, _ = row
        new_operator = оператор if оператор is not None else cur_operator
        new_threshold = значение if значение is not None else cur_threshold
        new_priority = приоритет if приоритет is not None else cur_priority
        new_comment = комментарий if комментарий is not None else cur_comment
        char_name = _unit_display_name(character_key)
        new_raw_text = f"{char_name} {stat_name} {new_operator} {_fmt_value(new_threshold)}"
        database.update_stat_requirement(req_id, plate_name, character_key, stat_name, new_operator, new_threshold, new_priority, new_comment)
        await inter.response.send_message(f"✅ Требование #{req_id} обновлено: {new_raw_text}", ephemeral=True)

    @stat_req.sub_command(name="список", description="Показать сохранённые требования по плейту (и опционально персонажу)")
    async def stat_req_list(
        self,
        inter: disnake.ApplicationCommandInteraction,
        плейт: str = commands.Param(description="Плейт", autocomplete=autocomplete_stat_plate),
        персонаж: str = commands.Param(default=None, description="Персонаж (если не указан — весь плейт)", autocomplete=autocomplete_stat_character),
    ):
        await inter.response.defer(ephemeral=True)
        char_keys = [_parse_bracket_id(персонаж)] if персонаж else database.get_stat_requirement_characters(плейт)
        if not char_keys:
            await inter.edit_original_response("❌ Нет требований для этого плейта.")
            return

        lines = []
        for base_id in char_keys:
            rows = database.get_stat_requirements(плейт, base_id)
            if not rows:
                continue
            lines.append(f"## {_unit_display_name(base_id)}")
            for row in rows:
                req_id, _, _, _, _, _, priority, raw_text, comment, _, _ = row
                comment_part = f" · _{comment}_" if comment else ""
                lines.append(f"`#{req_id}` {PRIORITY_EMOJI.get(priority, '')} {raw_text}{comment_part}")

        embeds = _lines_to_embeds(f"📋 {плейт}", DATACRON_LIST_COLOR, lines)
        if not embeds:
            await inter.edit_original_response("❌ Нет требований для этого плейта.")
            return
        await inter.edit_original_response(embed=embeds[0])
        for e in embeds[1:]:
            await inter.followup.send(embed=e, ephemeral=True)

    # ------------------ /статы (открытая команда) ------------------
    @commands.slash_command(name="статы", description="Прогноз статов персонажа(ей) на другой релик относительно требований плейта")
    async def stats_check(
        self,
        inter: disnake.ApplicationCommandInteraction,
        плейт: str = commands.Param(description="Плейт (набор требований)", autocomplete=autocomplete_stat_plate),
        персонаж: str = commands.Param(default=None, description="Персонаж из плейта (если не указан — весь плейт)", autocomplete=autocomplete_stat_character),
        игрок: str = commands.Param(default=None, description="Игрок гильдии — статы посчитаются с его реальными модами/шмотом", autocomplete=autocomplete_players),
        релик: int = commands.Param(default=None, description="Целевой уровень реликвии (обязателен, если «игрок» не указан)", ge=0, le=9),
        обновить: bool = commands.Param(default=False, description="Обновить данные игрока из игры перед расчётом (только вместе с «игрок»)"),
    ):
        await inter.response.defer()

        if игрок is None and релик is None:
            await inter.edit_original_response(
                "❌ Укажите «игрок» (релик для прогноза возьмётся из требования плейта) либо «релик» явно."
            )
            return

        if not self.bot.stat_calc:
            await inter.edit_original_response("⏳ Калькулятор статов ещё загружается, попробуйте через минуту.")
            return

        char_keys = [_parse_bracket_id(персонаж)] if персонаж is not None else database.get_stat_requirement_characters(плейт)
        if not char_keys:
            await inter.edit_original_response("❌ Нет сохранённых требований для этого плейта.")
            return

        ally_code = None
        if игрок is not None:
            ally_code = self.bot.guild_roster_cache.get(игрок) if self.bot.guild_roster_cache else None
            if not ally_code:
                await inter.edit_original_response("❌ Игрок не найден в составе гильдии.")
                return

        lines = []
        matched_total = 0
        rows_total = 0
        updated_ats = []
        any_char_shown = False
        for base_id in char_keys:
            result = await _evaluate_character(self.bot, плейт, base_id, ally_code, обновить, релик, игрок)
            if result is None:
                continue
            char_name, block, matched, total, updated_at = result
            any_char_shown = True
            lines.append(f"## {char_name}")
            lines.append(block)
            matched_total += matched
            rows_total += total
            if updated_at:
                updated_ats.append(updated_at)

        if not any_char_shown:
            await inter.edit_original_response("❌ Нет сохранённых требований для этого плейта.")
            return

        if rows_total and matched_total == rows_total:
            color = DATACRON_CHECK_COLOR_FULL
        elif matched_total == 0:
            color = DATACRON_CHECK_COLOR_NONE
        else:
            color = DATACRON_CHECK_COLOR_PARTIAL

        subtitle = f"игрок: {игрок}" if игрок is not None else "без учёта модов/шмота игрока"
        title = f"📋 {плейт} ({subtitle})"
        embeds = _lines_to_embeds(title, color, lines)
        if not embeds:
            await inter.edit_original_response("❌ Нечего показать.")
            return

        if updated_ats:
            footer = f"Данные игрока обновлены: {updated_ats[0]}"
        elif ally_code is None:
            footer = "⚠️ Синтетический расчёт — без модов/шмота конкретного игрока"
        else:
            footer = "⚠️ Нет кэшированных данных ни по одному из этих юнитов у игрока"
        embeds[-1].set_footer(text=footer)

        await inter.edit_original_response(embed=embeds[0])
        for e in embeds[1:]:
            await inter.followup.send(embed=e)


def setup(bot):
    bot.add_cog(StatRequirementsCog(bot))
