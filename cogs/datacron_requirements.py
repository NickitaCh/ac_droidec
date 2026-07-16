import asyncio
import re

import disnake
from disnake.ext import commands, tasks

import database
from cogs.violations import autocomplete_players

# 'DatacronDefinitions' — бит из comlink.get_enums()["GameDataItemsEnum"], проверено вживую.
DATACRON_DEFINITIONS_FLAG = 70368744177664
DATACRON_LEVELS = (3, 6, 9)

DATACRON_ANY = "ANY"
DATACRON_NONE = "NONE"
DATACRON_ANY_LABEL = "Любой (тир разблокирован, бонус неважен)"
DATACRON_NONE_LABEL = "- (не проверять этот уровень)"

# Ключи локализации для подстановки {0} в тексты способностей уровней 3/6 —
# darkside/lightside используют отдельный ключ ForceAlignment_*, остальные —
# CATEGORY_<TAG>_DESC (фракции/стороны) или CATEGORY_ROLE<TAG>_DESC (роли: танк/поддержка/...).
TARGET_LABEL_OVERRIDES = {
    "darkside": "ForceAlignment_Dark",
    "lightside": "ForceAlignment_Light",
}


# =====================================================================
# Построение справочника датакронов (game data + локализация) — тяжёлое,
# кешируется в bot.datacron_cache и обновляется раз в 12 часов.
# =====================================================================
def _parse_loc_kv(loc_text: str) -> dict:
    kv = {}
    for line in loc_text.split("\n"):
        if "|" not in line:
            continue
        k, _, v = line.partition("|")
        kv[k.strip()] = v.strip()
    return kv


def _resolve_ability_desc(ability_id: str, loc_kv: dict):
    prefix = f"{ability_id.upper()}_DESC"
    best = loc_kv.get(prefix)
    for v in range(2, 8):
        candidate = loc_kv.get(f"{prefix}_V{v}")
        if candidate is not None:
            best = candidate
    return best


def _resolve_target_label(target_key: str, loc_kv: dict) -> str:
    if not target_key:
        return ""
    override_key = TARGET_LABEL_OVERRIDES.get(target_key)
    if override_key and override_key in loc_kv:
        return loc_kv[override_key]
    for key in (f"CATEGORY_{target_key.upper()}_DESC", f"CATEGORY_ROLE{target_key.upper()}_DESC"):
        if key in loc_kv:
            return loc_kv[key]
    return target_key


def _build_ability_label(ability_id: str, target_rule: str, loc_kv: dict) -> str:
    desc = _resolve_ability_desc(ability_id, loc_kv)
    if desc is None:
        return ability_id
    if "{0}" in desc:
        target_key = target_rule[len("target_datacron_"):] if target_rule.startswith("target_datacron_") else ""
        desc = desc.replace("{0}", _resolve_target_label(target_key, loc_kv))
    # В сырых текстах способностей встречаются буквальные "\n" (маркер переноса строки
    # для игрового клиента) — схлопываем в пробелы, чтобы метка была однострочной
    # (обязательно для опций автокомплита) и читаемой в финальном отчёте.
    return desc.replace("\\n\\n", " ").replace("\\n", " ")


async def _fetch_datacron_cache(comlink) -> dict:
    game_data = await asyncio.to_thread(comlink.get_game_data, items=str(DATACRON_DEFINITIONS_FLAG))
    loc = await asyncio.to_thread(comlink.get_localization, locale="RUS_RU", unzip=True)
    loc_kv = _parse_loc_kv(loc.get("Loc_RUS_RU.txt", ""))

    affix_sets = {a["id"]: a for a in game_data.get("datacronAffixTemplateSet", [])}

    seasons = {}
    for dset in game_data.get("datacronSet", []):
        set_id = dset.get("id")
        seasons[set_id] = {
            "display_name": loc_kv.get(dset.get("displayName", ""), f"Сезон {set_id}"),
            "level3": {},
            "level6": {},
            "level9": {},
        }

    for template in game_data.get("datacronTemplate", []):
        if template.get("focused"):
            continue
        set_id = template.get("setId")
        if set_id not in seasons:
            seasons[set_id] = {"display_name": f"Сезон {set_id}", "level3": {}, "level6": {}, "level9": {}}
        for tier in template.get("tier", []):
            level = tier.get("id")
            if level not in DATACRON_LEVELS:
                continue
            level_key = f"level{level}"
            for affix_set_id in tier.get("affixTemplateSetId", []):
                affix_set = affix_sets.get(affix_set_id)
                if not affix_set:
                    continue
                for affix in affix_set.get("affix", []):
                    ability_id = affix.get("abilityId")
                    if not ability_id:
                        continue
                    label = _build_ability_label(ability_id, affix.get("targetRule", ""), loc_kv)
                    seasons[set_id][level_key][ability_id] = label

    result_seasons = {}
    for set_id, data in seasons.items():
        result_seasons[set_id] = {
            "display_name": data["display_name"],
            "level3": sorted(data["level3"].items(), key=lambda kv: kv[1]),
            "level6": sorted(data["level6"].items(), key=lambda kv: kv[1]),
            "level9": sorted(data["level9"].items(), key=lambda kv: kv[1]),
        }
    return {"seasons": result_seasons}


# =====================================================================
# Мелкие хелперы форматирования/парсинга, общие для команд и автокомплитов
# =====================================================================
def _parse_trailing_bracket_int(text: str):
    if not text:
        return None
    m = re.search(r"\[(\d+)\]\s*$", text)
    return int(m.group(1)) if m else None


def _parse_leading_hash_int(text: str):
    if not text:
        return None
    m = re.match(r"#(\d+)", text.strip())
    return int(m.group(1)) if m else None


def _season_label(cache, set_id) -> str:
    if cache and set_id in cache.get("seasons", {}):
        return cache["seasons"][set_id]["display_name"]
    return f"Сезон {set_id}"


def _level_label(level_options, value) -> str:
    if value == DATACRON_ANY:
        return "Любой"
    if value == DATACRON_NONE:
        return "-"
    for ability_id, label in level_options or []:
        if ability_id == value:
            return label
    return value


def _format_requirement_summary(set_id, l3, l6, l9, cache) -> str:
    season_data = cache["seasons"].get(set_id) if cache else None
    l3_label = _level_label(season_data["level3"] if season_data else [], l3)
    l6_label = _level_label(season_data["level6"] if season_data else [], l6)
    l9_label = _level_label(season_data["level9"] if season_data else [], l9)
    return f"{_season_label(cache, set_id)}: {l3_label} → {l6_label} → {l9_label}"


def _chunk_lines(lines, limit=1900):
    chunks = []
    current = ""
    for line in lines:
        candidate = f"{current}\n{line}" if current else line
        if len(candidate) > limit:
            if current:
                chunks.append(current)
            current = line
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks


# =====================================================================
# Подбор подходящих датакронов игрока под требования сезона
# =====================================================================
def _extract_player_base_datacrons(player_json: dict, set_id: int):
    result = []
    for d in player_json.get("datacron", []) or []:
        if d.get("setId") != set_id or d.get("focused"):
            continue
        if not str(d.get("templateId", "")).endswith("_base"):
            continue
        affix = d.get("affix", [])
        levels = {}
        for level_num, idx in ((3, 2), (6, 5), (9, 8)):
            levels[level_num] = affix[idx].get("abilityId") if len(affix) > idx else None
        result.append({"id": d.get("id"), "levels": levels})
    return result


def _level_matches(requirement_value, owned_ability_id) -> bool:
    if requirement_value == DATACRON_NONE:
        return True
    if requirement_value == DATACRON_ANY:
        return owned_ability_id is not None
    return owned_ability_id == requirement_value


def _requirement_specificity(row) -> int:
    _, _, l3, l6, l9, _, _, _ = row
    return sum(v not in (DATACRON_ANY, DATACRON_NONE) for v in (l3, l6, l9))


def _match_requirements(requirements, owned_datacrons):
    """Жадный подбор: сначала самые конкретные требования, каждый ДК занимает не больше одного слота."""
    sorted_reqs = sorted(requirements, key=lambda r: (-_requirement_specificity(r), r[0]))
    used_ids = set()
    pairs = []
    for req in sorted_reqs:
        _, _, l3, l6, l9, _, _, _ = req
        match = None
        for dc in owned_datacrons:
            if dc["id"] in used_ids:
                continue
            levels = dc["levels"]
            if (_level_matches(l3, levels[3]) and _level_matches(l6, levels[6]) and _level_matches(l9, levels[9])):
                match = dc
                break
        if match:
            used_ids.add(match["id"])
        pairs.append((req, match))
    pairs.sort(key=lambda p: p[0][0])
    return pairs


# =====================================================================
# Автокомплиты (модульные функции — как autocomplete_players/autocomplete_violations)
# =====================================================================
async def autocomplete_datacron_season(inter: disnake.ApplicationCommandInteraction, string: str):
    cache = inter.bot.datacron_cache
    if not cache:
        return ["⏳ Справочник датакронов ещё загружается, подождите..."]
    search = string.lower().strip()
    options = []
    for set_id, data in sorted(cache["seasons"].items(), key=lambda kv: -kv[0]):
        label = f"{data['display_name']} [{set_id}]"
        if not search or search in label.lower():
            options.append(label)
    return options[:25]


async def _autocomplete_datacron_level(inter: disnake.ApplicationCommandInteraction, string: str, level_num: int):
    cache = inter.bot.datacron_cache
    if not cache:
        return ["⏳ Справочник датакронов ещё загружается, подождите..."]
    set_id = _parse_trailing_bracket_int(inter.filled_options.get("сезон"))
    if set_id is None:
        return ["⚠️ СНАЧАЛА выберите сезон!"]
    season_data = cache["seasons"].get(set_id)
    if not season_data:
        return ["❌ Нет данных по этому сезону в справочнике."]

    options = [
        disnake.OptionChoice(name=DATACRON_ANY_LABEL, value=DATACRON_ANY),
        disnake.OptionChoice(name=DATACRON_NONE_LABEL, value=DATACRON_NONE),
    ]
    search = string.lower().strip()
    for ability_id, label in season_data[f"level{level_num}"]:
        if not search or search in label.lower():
            options.append(disnake.OptionChoice(name=label[:100], value=ability_id))
    return options[:25]


async def autocomplete_datacron_level3(inter, string):
    return await _autocomplete_datacron_level(inter, string, 3)


async def autocomplete_datacron_level6(inter, string):
    return await _autocomplete_datacron_level(inter, string, 6)


async def autocomplete_datacron_level9(inter, string):
    return await _autocomplete_datacron_level(inter, string, 9)


async def autocomplete_datacron_req_id(inter: disnake.ApplicationCommandInteraction, string: str):
    rows = database.get_all_datacron_requirements()
    if not rows:
        return ["❌ Список требований пуст."]
    cache = inter.bot.datacron_cache
    search = string.lower().strip()
    options = []
    for row in rows:
        req_id, set_id, l3, l6, l9, comment, created_by, created_at = row
        label = f"#{req_id} — {_format_requirement_summary(set_id, l3, l6, l9, cache)}"
        if not search or search in label.lower():
            options.append(disnake.OptionChoice(name=label[:100], value=f"#{req_id}"))
    return options[:25]


# =====================================================================
#                    ОСНОВНОЙ МОДУЛЬ ТРЕБОВАНИЙ К ДАТАКРОНАМ
# =====================================================================
class DatacronRequirementsCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        if not hasattr(bot, "datacron_cache"):
            bot.datacron_cache = None
        self.datacron_cache_loop.start()

    def cog_unload(self):
        self.datacron_cache_loop.cancel()

    @tasks.loop(hours=12)
    async def datacron_cache_loop(self):
        try:
            self.bot.datacron_cache = await _fetch_datacron_cache(self.bot.comlink)
            print(f"✅ [ДК] Справочник датакронов обновлён: {len(self.bot.datacron_cache['seasons'])} сезонов")
        except Exception as e:
            print(f"❌ [ДК] Ошибка обновления справочника датакронов: {e}")

    @datacron_cache_loop.before_loop
    async def _before_datacron_cache_loop(self):
        await self.bot.wait_until_ready()

    # ------------------ Slash-команды ------------------
    @commands.slash_command(name="дк_требования", description="Управление требованиями к датакронам по сезонам ТБ")
    @commands.has_any_role(1153753506772164629)
    async def datacron_req(self, inter: disnake.ApplicationCommandInteraction):
        pass

    @datacron_req.sub_command(name="добавить", description="Добавить требование к датакрону в список сезона")
    async def datacron_req_add(
        self,
        inter: disnake.ApplicationCommandInteraction,
        сезон: str = commands.Param(description="Сезон датакрона", autocomplete=autocomplete_datacron_season),
        уровень3: str = commands.Param(default=DATACRON_NONE, description="Бонус 3 уровня", autocomplete=autocomplete_datacron_level3),
        уровень6: str = commands.Param(default=DATACRON_NONE, description="Бонус 6 уровня", autocomplete=autocomplete_datacron_level6),
        уровень9: str = commands.Param(default=DATACRON_NONE, description="Бонус 9 уровня", autocomplete=autocomplete_datacron_level9),
        комментарий: str = commands.Param(default=None, description="Заметка по приоритетным % статам (не проверяется автоматически)"),
    ):
        set_id = _parse_trailing_bracket_int(сезон)
        if set_id is None:
            await inter.response.send_message("❌ Некорректный сезон — выберите вариант из списка автодополнения.", ephemeral=True)
            return
        if уровень3 == DATACRON_NONE and уровень6 == DATACRON_NONE and уровень9 == DATACRON_NONE:
            await inter.response.send_message("❌ Хотя бы один уровень (3/6/9) должен быть указан, иначе требование бессмысленно.", ephemeral=True)
            return

        req_id = database.add_datacron_requirement(set_id, уровень3, уровень6, уровень9, комментарий, str(inter.author.id))
        summary = _format_requirement_summary(set_id, уровень3, уровень6, уровень9, self.bot.datacron_cache)
        await inter.response.send_message(f"✅ Требование #{req_id} добавлено: {summary}", ephemeral=True)

    @datacron_req.sub_command(name="редактировать", description="Изменить требование или удалить его из списка")
    async def datacron_req_edit(
        self,
        inter: disnake.ApplicationCommandInteraction,
        id: str = commands.Param(description="Требование для изменения", autocomplete=autocomplete_datacron_req_id),
        сезон: str = commands.Param(default=None, description="Новый сезон", autocomplete=autocomplete_datacron_season),
        уровень3: str = commands.Param(default=None, description="Новый бонус 3 уровня", autocomplete=autocomplete_datacron_level3),
        уровень6: str = commands.Param(default=None, description="Новый бонус 6 уровня", autocomplete=autocomplete_datacron_level6),
        уровень9: str = commands.Param(default=None, description="Новый бонус 9 уровня", autocomplete=autocomplete_datacron_level9),
        комментарий: str = commands.Param(default=None, description="Новая заметка"),
        удалить: bool = commands.Param(default=False, description="Удалить это требование вместо редактирования"),
    ):
        req_id = _parse_leading_hash_int(id)
        if req_id is None:
            await inter.response.send_message("❌ Некорректный id — выберите вариант из списка автодополнения.", ephemeral=True)
            return
        row = database.get_datacron_requirement(req_id)
        if not row:
            await inter.response.send_message(f"❌ Требование #{req_id} не найдено.", ephemeral=True)
            return

        if удалить:
            database.delete_datacron_requirement(req_id)
            await inter.response.send_message(f"🗑️ Требование #{req_id} удалено.", ephemeral=True)
            return

        _, cur_set_id, cur_l3, cur_l6, cur_l9, cur_comment, _, _ = row

        new_set_id = cur_set_id
        if сезон is not None:
            parsed = _parse_trailing_bracket_int(сезон)
            if parsed is None:
                await inter.response.send_message("❌ Некорректный сезон — выберите вариант из списка автодополнения.", ephemeral=True)
                return
            new_set_id = parsed

        new_l3 = уровень3 if уровень3 is not None else cur_l3
        new_l6 = уровень6 if уровень6 is not None else cur_l6
        new_l9 = уровень9 if уровень9 is not None else cur_l9
        new_comment = комментарий if комментарий is not None else cur_comment

        database.update_datacron_requirement(req_id, new_set_id, new_l3, new_l6, new_l9, new_comment)
        summary = _format_requirement_summary(new_set_id, new_l3, new_l6, new_l9, self.bot.datacron_cache)
        await inter.response.send_message(f"✅ Требование #{req_id} обновлено: {summary}", ephemeral=True)

    @datacron_req.sub_command(name="очистить_сезон", description="Удалить весь список требований неактивного сезона")
    async def datacron_req_clear_season(
        self,
        inter: disnake.ApplicationCommandInteraction,
        сезон: str = commands.Param(description="Сезон для очистки", autocomplete=autocomplete_datacron_season),
        подтвердить: bool = commands.Param(default=False, description="Установите true только после проверки количества требований для удаления"),
    ):
        set_id = _parse_trailing_bracket_int(сезон)
        if set_id is None:
            await inter.response.send_message("❌ Некорректный сезон — выберите вариант из списка автодополнения.", ephemeral=True)
            return

        season_label = _season_label(self.bot.datacron_cache, set_id)
        count = database.count_datacron_requirements_by_set(set_id)
        if count == 0:
            await inter.response.send_message(f"ℹ️ У сезона {season_label} нет сохранённых требований.", ephemeral=True)
            return

        if not подтвердить:
            await inter.response.send_message(
                f"⚠️ Будет удалено требований: {count} ({season_label}). "
                f"Повторите команду с подтвердить=True, чтобы подтвердить удаление.",
                ephemeral=True,
            )
            return

        deleted = database.delete_datacron_requirements_by_set(set_id)
        await inter.response.send_message(f"🗑️ Удалено требований: {deleted} ({season_label}).", ephemeral=True)

    @datacron_req.sub_command(name="проверить", description="Проверить, есть ли у игрока подходящие датакроны под все требования сезона")
    async def datacron_req_check(
        self,
        inter: disnake.ApplicationCommandInteraction,
        сезон: str = commands.Param(description="Сезон для проверки", autocomplete=autocomplete_datacron_season),
        игрок: str = commands.Param(description="Выберите игрока", autocomplete=autocomplete_players),
    ):
        await inter.response.defer(ephemeral=True)

        set_id = _parse_trailing_bracket_int(сезон)
        if set_id is None:
            await inter.edit_original_message("❌ Некорректный сезон — выберите вариант из списка автодополнения.")
            return

        cache = self.bot.guild_roster_cache
        if not cache or игрок not in cache:
            await inter.edit_original_message("❌ Игрок не найден в кэше состава.")
            return
        allycode = cache[игрок]

        season_label = _season_label(self.bot.datacron_cache, set_id)
        requirements = database.get_datacron_requirements_by_set(set_id)
        if not requirements:
            await inter.edit_original_message(f"ℹ️ У сезона {season_label} нет сохранённых требований.")
            return

        try:
            player = await asyncio.wait_for(
                asyncio.to_thread(self.bot.comlink.get_player, allycode=allycode),
                timeout=15.0,
            )
        except asyncio.TimeoutError:
            await inter.edit_original_message("⏰ Запрос к Comlink занял слишком много времени.")
            return
        except Exception as e:
            await inter.edit_original_message(f"Ошибка получения данных игрока: {e}")
            return

        owned = _extract_player_base_datacrons(player, set_id)
        pairs = _match_requirements(requirements, owned)

        season_data = self.bot.datacron_cache["seasons"].get(set_id) if self.bot.datacron_cache else None

        def level_label(level_num, value):
            options = season_data[f"level{level_num}"] if season_data else []
            return _level_label(options, value)

        lines = [f"📋 Проверка датакронов: **{игрок}** — {season_label}", ""]
        matched_count = 0
        for req, match in pairs:
            req_id, _, l3, l6, l9, comment, _, _ = req
            header = f"#{req_id}: {level_label(3, l3)} → {level_label(6, l6)} → {level_label(9, l9)}"
            if comment:
                header += f" _(заметка: {comment})_"
            if match:
                matched_count += 1
                m = match["levels"]
                m3 = level_label(3, m[3]) if m[3] else "—"
                m6 = level_label(6, m[6]) if m[6] else "—"
                m9 = level_label(9, m[9]) if m[9] else "—"
                lines.append(f"✅ {header}")
                lines.append(f"   Закрыто ДК: {m3} → {m6} → {m9}")
            else:
                lines.append(f"❌ {header}")

        lines.append("")
        lines.append(f"Итого: {matched_count} / {len(pairs)} требований закрыто.")

        chunks = _chunk_lines(lines)
        await inter.edit_original_message(chunks[0])
        for chunk in chunks[1:]:
            await inter.followup.send(chunk, ephemeral=True)


def setup(bot):
    bot.add_cog(DatacronRequirementsCog(bot))
