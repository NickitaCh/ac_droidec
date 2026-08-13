import asyncio
import math
import re

import disnake
from disnake.ext import commands, tasks

import database
import guild_resolver
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
# ВАЖНО: "обобщённые" статы без Physical/Special (Critical Chance, Critical Avoidance,
# Accuracy, Offense, Defense) в финальном расчёте StatCalc НЕ существуют — final-словарь
# отдаёт только расщеплённые Physical.../Special... варианты, генерик-ключи всегда None
# (проверено эмпирически на реальном игроке). Такие пункты сюда не добавлять — требование
# с ними навсегда останется "нет данных".
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
    disnake.OptionChoice(name="Physical Critical Avoidance", value="Physical Critical Avoidance"),
    disnake.OptionChoice(name="Special Critical Avoidance", value="Special Critical Avoidance"),
    disnake.OptionChoice(name="Physical Accuracy", value="Physical Accuracy"),
    disnake.OptionChoice(name="Special Accuracy", value="Special Accuracy"),
    disnake.OptionChoice(name="Health Steal", value="Health Steal"),
]

# Плоская (не растущая с реликвией) часть порога для /статы_релик — значения и сама модель
# взяты из гильдийской Google-таблицы (лист TEST, формула BASESTAT(...)*MODMULT(...)+flat):
# порог считается как flat_offset (роллы с модов, не масштабируются) + остаток порога,
# который масштабируется пропорционально росту голой базы стата между релик-уровнями.
# Статов без записи здесь (Speed, Potency, Tenacity, крит-статы и т.п.) в таблице тоже нет —
# для них норма при пересчёте остаётся как есть, без масштабирования.
RELIC_PROJECTION_FLAT_OFFSET = {
    "Health": 1500,
    "Protection": 3000,
    "Physical Damage": 100,
    "Special Damage": 100,
    "Armor": 20,
}

OPERATOR_CHOICES = [
    disnake.OptionChoice(name=">=", value=">="),
    disnake.OptionChoice(name="<=", value="<="),
    disnake.OptionChoice(name="=", value="="),
]

# Короткие подписи для колонки "Стат" в таблице /статы — полные названия (особенно
# Physical/Special Damage, Critical Chance/Avoidance) не влезают в ширину на мобильных.
# Health/Speed/Armor/Potency и т.п. уже достаточно короткие — оставлены как есть (fallback).
STAT_LABEL_SHORT = {
    "Relic": "Реликвия",
    "Protection": "Защита",
    "Physical Damage": "Атк.Ф",
    "Special Damage": "Атк.О",
    "Critical Chance": "КШ",
    "Physical Critical Chance": "КШ.Ф",
    "Special Critical Chance": "КШ.О",
    "Critical Damage": "КУ",
    "Critical Avoidance": "АКШ",
    "Physical Critical Avoidance": "АКШ.Ф",
    "Special Critical Avoidance": "АКШ.О",
    "Accuracy": "Точность",
    "Physical Accuracy": "Точн.Ф",
    "Special Accuracy": "Точн.О",
    "Health Steal": "Вампиризм",
    "Tenacity": "Стойкость",
    "Offense": "Атака",
    "Defense": "Оборона",
}


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
    return database.get_game_unit_name(base_id) or base_id


def _fmt_value(value: float) -> str:
    return f"{value:g}"


def _fmt_compact(value: float) -> str:
    """Сокращённая запись для ширины таблицы: 8472 -> '8,4к' (отбрасывание, не округление),
    8000 -> '8к'. Значения меньше 1000 (релик, скорость, статы-проценты...) не сокращаются
    буквой "к", но тоже режутся до 1 знака после запятой без округления — иначе проценты
    вроде Potency/Armor вылезают как "106.509"/"72.2766" (StatCalc считает их с большой
    точностью, см. PERCENT_STATS в stat_engine.py)."""
    if abs(value) >= 1000:
        truncated = math.trunc(value / 100) / 10
        text = f"{truncated:.1f}".replace(".", ",")
        if text.endswith(",0"):
            text = text[:-2]
        return text + "к"
    truncated = math.trunc(value * 10) / 10
    if truncated == math.trunc(truncated):
        return str(int(truncated))
    return f"{truncated:.1f}".replace(".", ",")


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
    label = STAT_LABEL_SHORT.get(stat_name, stat_name)
    return f"{label}*" if priority == "optional" else label


def _load_char_rows(plate_name: str, base_id: str, guild_id: int = 1):
    """Общий префикс для обоих режимов расчёта: сохранённые требования персонажа в плейте,
    его отображаемое имя, требуемый по плейту релик, комментарии и лёгенда "опционально".
    Возвращает None, если для этого персонажа нет сохранённых требований."""
    rows = database.get_stat_requirements(plate_name, base_id, guild_id=guild_id)
    if not rows:
        return None
    char_name = _unit_display_name(base_id)
    relic_reqs = [r for r in rows if r[3] == "Relic"]
    required_relic = int(relic_reqs[0][5]) if relic_reqs else None
    comments = [r[8] for r in rows if r[8]]
    legend = " · * опционально" if any(r[6] == "optional" for r in rows) else ""
    return rows, char_name, required_relic, comments, legend


async def _evaluate_character_player(bot, plate_name: str, base_id: str, ally_code, force_refresh: bool, player_label, guild_id: int = 1):
    """Возвращает (char_name, block, matched, total, updated_at) для одного персонажа плейта
    у конкретного игрока — статы берутся из его реальных модов/шмота, прогноз на релик плейта.
    Возвращает None, если для этого персонажа нет сохранённых требований (пропускается в отчёте)."""
    loaded = _load_char_rows(plate_name, base_id, guild_id)
    if loaded is None:
        return None
    rows, char_name, required_relic, comments, legend = loaded
    matched = 0
    total = 0

    unit, updated_at = await _get_unit_for_player(bot, ally_code, base_id, force_refresh)
    if not unit:
        block = f"⚠️ нет юнита у игрока «{player_label}» (не открыт либо ещё не синхронизирован)"
        return char_name, block, 0, 0, None

    current_relic = stat_engine.get_current_relic_level(unit)
    current_values = dict(stat_engine.calc_final_stats(bot.stat_calc, unit))
    current_values["Relic"] = current_relic

    # Показываем прогноз не только вверх (у игрока релик ниже требуемого), но и вниз
    # (у игрока уже выше — интересно, каким был бы стат ровно на уровне плейта).
    # Формула "Нужно" (порог минус дельта) не зависит от направления: дельта от релика
    # к релику при тех же модах/шмоте фиксирована в обе стороны.
    show_projection = required_relic is not None and required_relic != current_relic
    target_relic = required_relic if show_projection else current_relic
    projected_values = None
    if show_projection:
        projected_unit = stat_engine.project_unit_relic(unit, target_relic)
        projected_values = dict(stat_engine.calc_final_stats(bot.stat_calc, projected_unit))
        projected_values["Relic"] = target_relic

    caption = f"Релик игрока: {current_relic}"
    if show_projection:
        if target_relic > current_relic:
            caption += f" → цель по плейту: {target_relic}"
        else:
            caption += f" → плейт требует {target_relic} (у игрока выше)"
    caption += legend
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
    if comments:
        block += "\n" + "\n".join(f"💠 _{c}_" for c in comments)

    return char_name, block, matched, total, updated_at


async def _project_character_relic(bot, plate_name: str, base_id: str, target_relic: int, guild_id: int = 1):
    """Возвращает (char_name, block) — пересчёт уже заданных в плейте норм на другой релик.
    Модель — та же, что в гильдийской Google-таблице (BASESTAT*MODMULT+flat): порог на
    исходном релике раскладывается на плоскую часть (RELIC_PROJECTION_FLAT_OFFSET — роллы
    с модов, не растут с базой) и оставшуюся часть, которая масштабируется пропорционально
    голой безмодовой базе стата. Множитель находится из уже заданного порога:
        multiplier = (threshold − flat) / base(исходный релик)
        projected  = base(целевой релик) × multiplier + flat
    Для статов без записи в RELIC_PROJECTION_FLAT_OFFSET (Speed, Potency, крит-статы и т.п.)
    норма не пересчитывается — как и в самой таблице, она просто переносится как есть.
    Возвращает None, если для этого персонажа нет сохранённых требований в плейте."""
    loaded = _load_char_rows(plate_name, base_id, guild_id)
    if loaded is None:
        return None
    rows, char_name, required_relic, comments, legend = loaded

    if required_relic is None:
        block = "⚠️ В требованиях плейта нет строки Relic — не от чего считать прибавку."
        return char_name, block

    ref_unit = _build_synthetic_unit(base_id, required_relic)
    target_unit = _build_synthetic_unit(base_id, target_relic)
    ref_values = dict(stat_engine.calc_final_stats(bot.stat_calc, ref_unit))
    target_values = dict(stat_engine.calc_final_stats(bot.stat_calc, target_unit))

    headers = ["Стат", f"Норма Р{required_relic}", f"Норма Р{target_relic}"]
    table_rows = []
    for row in rows:
        _, _, _, stat_name, operator, threshold, priority, raw_text, comment, _, _ = row
        label = _stat_label(stat_name, priority)
        orig_cell = f"{operator} {_fmt_compact(threshold)}"

        if stat_name == "Relic":
            table_rows.append([label, orig_cell, f"{operator} {_fmt_compact(target_relic)}"])
            continue

        flat = RELIC_PROJECTION_FLAT_OFFSET.get(stat_name)
        if flat is None:
            table_rows.append([label, orig_cell, orig_cell])
            continue

        ref_val = ref_values.get(stat_name)
        tgt_val = target_values.get(stat_name)
        if not ref_val or tgt_val is None:
            table_rows.append([label, orig_cell, "нет данных"])
            continue

        multiplier = (threshold - flat) / ref_val
        projected = tgt_val * multiplier + flat
        table_rows.append([label, orig_cell, f"{operator} {_fmt_compact(projected)}"])

    block = f"Норма пересчитана с релика {required_relic} на {target_relic} (плоская часть + пропорциональный рост базы)" + legend + "\n" + _build_table(headers, table_rows)
    if comments:
        block += "\n" + "\n".join(f"💠 _{c}_" for c in comments)

    return char_name, block


# =====================================================================
# Автокомплиты (модульные функции — как autocomplete_players/autocomplete_datacron_*)
# =====================================================================
async def autocomplete_stat_plate(inter: disnake.ApplicationCommandInteraction, string: str):
    guild_id = guild_resolver.resolve_guild_id(inter.author)
    if guild_id is None:
        return []
    plates = database.get_all_stat_requirement_plates(guild_id=guild_id)
    if not plates:
        return ["❌ Список плейтов пуст."]
    search = string.lower().strip()
    return [p for p in plates if not search or search in p.lower()][:25]


async def autocomplete_stat_character(inter: disnake.ApplicationCommandInteraction, string: str):
    guild_id = guild_resolver.resolve_guild_id(inter.author)
    if guild_id is None:
        return []
    plate = inter.filled_options.get("плейт")
    if not plate:
        return ["⚠️ СНАЧАЛА выберите плейт!"]
    char_keys = database.get_stat_requirement_characters(plate, guild_id=guild_id)
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
    guild_id = guild_resolver.resolve_guild_id(inter.author)
    if guild_id is None:
        return []
    rows = database.get_all_stat_requirements(guild_id=guild_id)
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
        # player_unit_cache — не per-guild (ключ ally_code глобально уникален), поэтому
        # синхронизируем объединённый ростер по всем зарегистрированным гильдиям сразу.
        if not self.bot.guild_roster_caches:
            return
        ally_codes = set()
        for cache in self.bot.guild_roster_caches.values():
            ally_codes.update(cache.values())
        ally_codes = list(ally_codes)
        if not ally_codes:
            return
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
    @commands.check(lambda inter: guild_resolver.is_officer_for_resolved_guild(inter.author))
    async def stat_req(self, inter: disnake.ApplicationCommandInteraction):
        pass

    @stat_req.sub_command(name="добавить", description="Добавить требование к стату персонажа в плейт")
    async def stat_req_add(
        self,
        inter: disnake.ApplicationCommandInteraction,
        плейт: str = commands.Param(description="Плейт (как в HotUtils, например AC_ALL)", autocomplete=autocomplete_stat_plate),
        персонаж: str = commands.Param(description="Персонаж", autocomplete=units_autocomplete),
        стат: str = commands.Param(description="Какой стат проверяем (или Relic для уровня реликвии)", choices=STAT_CHOICES),
        оператор: str = commands.Param(description="Оператор сравнения", choices=OPERATOR_CHOICES),
        значение: float = commands.Param(description="Пороговое значение"),
        приоритет: str = commands.Param(default=PRIORITY_REQUIRED, description="Приоритет требования", choices=PRIORITY_CHOICES),
        комментарий: str = commands.Param(default=None, description="Заметка"),
    ):
        guild_id = guild_resolver.resolve_guild_id(inter.author)
        if guild_id is None:
            await inter.response.send_message("❌ Не удалось определить, к какой гильдии вы относитесь.", ephemeral=True)
            return

        if плейт not in database.get_all_stat_requirement_plates(guild_id=guild_id):
            await inter.response.send_message(
                f"❌ Плейт «{плейт}» не найден — выберите вариант из списка автодополнения либо создайте его сначала через /статы_требования создать.",
                ephemeral=True,
            )
            return

        base_id = _parse_bracket_id(персонаж)
        char_name = _unit_display_name(base_id)
        raw_text = f"{char_name} {стат} {оператор} {_fmt_value(значение)}"
        req_id = database.add_stat_requirement(
            плейт, base_id, стат, оператор, значение, приоритет, raw_text, комментарий, str(inter.author.id), guild_id=guild_id
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
        guild_id = guild_resolver.resolve_guild_id(inter.author)
        if guild_id is None:
            await inter.response.send_message("❌ Не удалось определить, к какой гильдии вы относитесь.", ephemeral=True)
            return

        req_id = _parse_req_id(id)
        if req_id is None:
            await inter.response.send_message("❌ Некорректный id — выберите вариант из списка автодополнения.", ephemeral=True)
            return
        row = database.get_stat_requirement(req_id, guild_id=guild_id)
        if not row:
            await inter.response.send_message(f"❌ Требование #{req_id} не найдено.", ephemeral=True)
            return

        if удалить:
            database.delete_stat_requirement(req_id, guild_id=guild_id)
            await inter.response.send_message(f"🗑️ Требование #{req_id} удалено.", ephemeral=True)
            return

        _, plate_name, character_key, stat_name, cur_operator, cur_threshold, cur_priority, _raw_text, cur_comment, _, _ = row
        new_operator = оператор if оператор is not None else cur_operator
        new_threshold = значение if значение is not None else cur_threshold
        new_priority = приоритет if приоритет is not None else cur_priority
        new_comment = комментарий if комментарий is not None else cur_comment
        char_name = _unit_display_name(character_key)
        new_raw_text = f"{char_name} {stat_name} {new_operator} {_fmt_value(new_threshold)}"
        database.update_stat_requirement(req_id, plate_name, character_key, stat_name, new_operator, new_threshold, new_priority, new_comment, guild_id=guild_id)
        await inter.response.send_message(f"✅ Требование #{req_id} обновлено: {new_raw_text}", ephemeral=True)

    @stat_req.sub_command(name="список", description="Показать сохранённые требования по плейту (и опционально персонажу)")
    async def stat_req_list(
        self,
        inter: disnake.ApplicationCommandInteraction,
        плейт: str = commands.Param(description="Плейт", autocomplete=autocomplete_stat_plate),
        персонаж: str = commands.Param(default=None, description="Персонаж (если не указан — весь плейт)", autocomplete=autocomplete_stat_character),
    ):
        await inter.response.defer(ephemeral=True)
        guild_id = guild_resolver.resolve_guild_id(inter.author)
        if guild_id is None:
            await inter.edit_original_response("❌ Не удалось определить, к какой гильдии вы относитесь.")
            return

        char_keys = [_parse_bracket_id(персонаж)] if персонаж else database.get_stat_requirement_characters(плейт, guild_id=guild_id)
        if not char_keys:
            await inter.edit_original_response("❌ Нет требований для этого плейта.")
            return

        lines = []
        for base_id in char_keys:
            rows = database.get_stat_requirements(плейт, base_id, guild_id=guild_id)
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

    @stat_req.sub_command(name="создать", description="Зарегистрировать новый плейт (набор норм статов)")
    async def stat_req_create_plate(
        self,
        inter: disnake.ApplicationCommandInteraction,
        плейт: str = commands.Param(description="Название нового плейта (как в HotUtils, например AC_ALL)"),
        описание: str = commands.Param(default=None, description="Заметка о плейте"),
    ):
        guild_id = guild_resolver.resolve_guild_id(inter.author)
        if guild_id is None:
            await inter.response.send_message("❌ Не удалось определить, к какой гильдии вы относитесь.", ephemeral=True)
            return

        created = database.create_stat_plate(плейт, описание, str(inter.author.id), guild_id=guild_id)
        if not created:
            await inter.response.send_message(f"❌ Плейт «{плейт}» уже существует.", ephemeral=True)
            return
        suffix = f" · _{описание}_" if описание else ""
        await inter.response.send_message(f"✅ Плейт «{плейт}» создан.{suffix}", ephemeral=True)

    @stat_req.sub_command(name="плейты", description="Показать список всех плейтов")
    async def stat_req_list_plates(self, inter: disnake.ApplicationCommandInteraction):
        await inter.response.defer(ephemeral=True)
        guild_id = guild_resolver.resolve_guild_id(inter.author)
        if guild_id is None:
            await inter.edit_original_response("❌ Не удалось определить, к какой гильдии вы относитесь.")
            return

        rows = database.get_all_stat_plates_detailed(guild_id=guild_id)
        if not rows:
            await inter.edit_original_response("❌ Плейтов пока нет — создайте через /статы_требования создать.")
            return

        lines = []
        for name, description, char_count, req_count in rows:
            desc_part = f" — _{description}_" if description else ""
            lines.append(f"`{name}`{desc_part} · персонажей: {char_count}, требований: {req_count}")

        embeds = _lines_to_embeds("📋 Плейты", DATACRON_LIST_COLOR, lines)
        await inter.edit_original_response(embed=embeds[0])
        for e in embeds[1:]:
            await inter.followup.send(embed=e, ephemeral=True)

    @stat_req.sub_command(name="переименовать", description="Переименовать плейт")
    async def stat_req_rename_plate(
        self,
        inter: disnake.ApplicationCommandInteraction,
        плейт: str = commands.Param(description="Плейт для переименования", autocomplete=autocomplete_stat_plate),
        новое_имя: str = commands.Param(description="Новое название плейта"),
    ):
        guild_id = guild_resolver.resolve_guild_id(inter.author)
        if guild_id is None:
            await inter.response.send_message("❌ Не удалось определить, к какой гильдии вы относитесь.", ephemeral=True)
            return

        ok = database.rename_stat_plate(плейт, новое_имя, guild_id=guild_id)
        if not ok:
            await inter.response.send_message(
                f"❌ Не удалось переименовать: плейт «{плейт}» не найден либо «{новое_имя}» уже занято другим плейтом.",
                ephemeral=True,
            )
            return
        await inter.response.send_message(f"✅ Плейт «{плейт}» переименован в «{новое_имя}».", ephemeral=True)

    @stat_req.sub_command(name="удалить_плейт", description="Удалить плейт целиком со всеми его требованиями")
    async def stat_req_delete_plate(
        self,
        inter: disnake.ApplicationCommandInteraction,
        плейт: str = commands.Param(description="Плейт для удаления", autocomplete=autocomplete_stat_plate),
        подтвердить: bool = commands.Param(default=False, description="Установите true только после проверки количества требований для удаления"),
    ):
        guild_id = guild_resolver.resolve_guild_id(inter.author)
        if guild_id is None:
            await inter.response.send_message("❌ Не удалось определить, к какой гильдии вы относитесь.", ephemeral=True)
            return

        count = database.count_stat_requirements_by_plate(плейт, guild_id=guild_id)
        if database.get_stat_plate(плейт, guild_id=guild_id) is None and count == 0:
            await inter.response.send_message(f"❌ Плейт «{плейт}» не найден.", ephemeral=True)
            return

        if not подтвердить:
            await inter.response.send_message(
                f"⚠️ Будет удалён плейт «{плейт}» и его требований: {count}. "
                f"Повторите команду с подтвердить=True, чтобы подтвердить удаление.",
                ephemeral=True,
            )
            return

        deleted = database.delete_stat_plate(плейт, guild_id=guild_id)
        await inter.response.send_message(f"🗑️ Плейт «{плейт}» удалён вместе с требованиями: {deleted}.", ephemeral=True)

    # ------------------ /статы (открытая команда) ------------------
    @commands.slash_command(name="статы", description="Прогноз статов персонажа(ей) игрока на релик плейта относительно требований")
    async def stats_check(
        self,
        inter: disnake.ApplicationCommandInteraction,
        плейт: str = commands.Param(description="Плейт (набор требований)", autocomplete=autocomplete_stat_plate),
        игрок: str = commands.Param(default=None, description="Игрок гильдии — если не указан, берётся ваша регистрация (/регистрация)", autocomplete=autocomplete_players),
        персонаж: str = commands.Param(default=None, description="Персонаж из плейта (если не указан — весь плейт)", autocomplete=autocomplete_stat_character),
        обновить: bool = commands.Param(default=False, description="Обновить данные игрока из игры перед расчётом"),
    ):
        await inter.response.defer()

        guild_id = guild_resolver.resolve_guild_id(inter.author)
        if guild_id is None:
            await inter.edit_original_response("❌ Не удалось определить, к какой гильдии вы относитесь.")
            return

        if not self.bot.stat_calc:
            await inter.edit_original_response("⏳ Калькулятор статов ещё загружается, попробуйте через минуту.")
            return

        char_keys = [_parse_bracket_id(персонаж)] if персонаж is not None else database.get_stat_requirement_characters(плейт, guild_id=guild_id)
        if not char_keys:
            await inter.edit_original_response("❌ Нет сохранённых требований для этого плейта.")
            return

        if игрок is None:
            registration = database.get_user_registration(str(inter.author.id))
            if not registration:
                await inter.edit_original_response(
                    "❌ Игрок не указан, а вы не зарегистрированы — используйте `/регистрация` или укажите игрока явно."
                )
                return
            ally_code, игрок = registration
        else:
            cache = self.bot.guild_roster_caches.get(guild_id, {})
            ally_code = cache.get(игрок)
            if not ally_code:
                await inter.edit_original_response("❌ Игрок не найден в составе гильдии.")
                return

        lines = []
        matched_total = 0
        rows_total = 0
        updated_ats = []
        any_char_shown = False
        for base_id in char_keys:
            result = await _evaluate_character_player(self.bot, плейт, base_id, ally_code, обновить, игрок, guild_id=guild_id)
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

        title = f"📋 {плейт} (игрок: {игрок})"
        embeds = _lines_to_embeds(title, color, lines)
        if not embeds:
            await inter.edit_original_response("❌ Нечего показать.")
            return

        footer = f"Данные игрока обновлены: {updated_ats[0]}" if updated_ats else "⚠️ Нет кэшированных данных ни по одному из этих юнитов у игрока"
        embeds[-1].set_footer(text=footer)

        await inter.edit_original_response(embed=embeds[0])
        for e in embeds[1:]:
            await inter.followup.send(embed=e)

    # ------------------ /статы_релик (пересчёт нормы плейта на другой релик) ------------------
    @commands.slash_command(name="статы_релик", description="Пересчитать уже заданную в плейте норму персонажа на другой релик")
    async def stats_relic_check(
        self,
        inter: disnake.ApplicationCommandInteraction,
        плейт: str = commands.Param(description="Плейт (набор требований)", autocomplete=autocomplete_stat_plate),
        персонаж: str = commands.Param(description="Персонаж из плейта", autocomplete=autocomplete_stat_character),
        релик: int = commands.Param(description="Целевой уровень реликвии", ge=0, le=10),
    ):
        await inter.response.defer()

        guild_id = guild_resolver.resolve_guild_id(inter.author)
        if guild_id is None:
            await inter.edit_original_response("❌ Не удалось определить, к какой гильдии вы относитесь.")
            return

        if not self.bot.stat_calc:
            await inter.edit_original_response("⏳ Калькулятор статов ещё загружается, попробуйте через минуту.")
            return

        base_id = _parse_bracket_id(персонаж)
        result = await _project_character_relic(self.bot, плейт, base_id, релик, guild_id=guild_id)
        if result is None:
            await inter.edit_original_response("❌ Нет сохранённых требований для этого персонажа в плейте.")
            return

        char_name, block = result
        title = f"📋 {плейт}: {char_name} → релик {релик}"
        embeds = _lines_to_embeds(title, DATACRON_LIST_COLOR, [block])
        if not embeds:
            await inter.edit_original_response("❌ Нечего показать.")
            return

        embeds[-1].set_footer(text="Модель: плоская часть порога (роллы модов) не растёт, остальное масштабируется вместе с базой персонажа")

        await inter.edit_original_response(embed=embeds[0])
        for e in embeds[1:]:
            await inter.followup.send(embed=e)


def setup(bot):
    bot.add_cog(StatRequirementsCog(bot))
