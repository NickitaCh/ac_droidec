"""Обновление глобального справочника юнитов (game_units) из Comlink — общая
реализация для бота (cogs/tasks.py::_do_units_synchronization, тонкая обёртка
поверх этой функции) и веб-дашборда (кнопка "Обновить справочник" на /tasks,
где свой Comlink-клиент — процесс не поднимает диска-бота, см.
web/routes/registration.py::_get_comlink). Явный comlink-параметр вместо
self.bot.comlink — единственное отличие от исходной версии в cogs/tasks.py."""

import asyncio

import database

# 'UnitDefinitions' — тот же бит, что в cogs/tasks.py::UNIT_DEFINITIONS_FLAG.
UNIT_DEFINITIONS_FLAG = 137438953472
# 'SkillDefinitions' — добыто эмпирически 2026-08-19 через comlink.get_enums()
# ["GameDataItemsEnum"]["SkillDefinitions"], тем же способом, что и UNIT_DEFINITIONS_FLAG.
# Отдельный вызов get_game_data, а не побитовое ИЛИ с UNIT_DEFINITIONS_FLAG — комбинированный
# запрос (Unit+Skill сразу) периодически возвращает HTTP 400 от comlink, видимо из-за
# объёма ответа; раздельные запросы отрабатывают надёжно.
SKILL_DEFINITIONS_FLAG = 4
# 'AbilityDefinitions' — тоже добыто через comlink.get_enums()["GameDataItemsEnum"], нужно
# ради человекочитаемых названий зет/омикронов (см. services/activity_diff.py). Проверено
# живыми данными на VPS 2026-08-24: skill.nameKey у SkillDefinitions — НЕ настоящее имя
# способности, а общий "DEFENSE_UP_NAME_KEY"/плейсхолдер, одинаковый у ВСЕХ способностей
# одного персонажа — использовать его нельзя. Настоящее имя лежит в отдельном каталоге
# способностей (ability.nameKey), который резолвится через
# skill.abilityReference -> ability.id (напр. "uniqueskill_GENERALSYNDULLA01" ->
# "uniqueability_generalsyndulla01"). Этот же ability.id (в нижнем регистре, как есть в
# данных) — точный слаг ссылки на swgoh.gg: https://swgoh.gg/units/{BASE_ID}/ability/{ability_id}/1/
# (подтверждено поиском реальных проиндексированных URL). Payload тяжёлый (~19 МБ, ~3 c на
# VPS) — обёрнут в тот же try/except, что и SkillDefinitions ниже, чтобы не валить весь sync_units.
ABILITY_DEFINITIONS_FLAG = 2097152

# Comlink's UnitDefinitions ('units') содержит НЕСКОЛЬКО записей на один baseId: по одной
# на каждый уровень редкости (rarity 1..7, только у реальных юнитов растёт baseStat с
# редкостью) плюс NPC/PVE-only боевые копии того же персонажа (PVE_-префикс, "_DUEL",
# "_GLEVENT", "_STARKILLER" и т.п. суффиксы), использующиеся в кампаниях/дуэлях/ивентах —
# они делят то же nameKey (то же отображаемое имя), из-за чего наивная синхронизация без
# фильтра сажает в game_units 4-8 дублей на популярного персонажа (найдено 2026-08-24 на
# "Верховный лидер Кайло Рен" — 5 baseId с одинаковым именем). Найдено эмпирически (не
# угадано): это ровно тот же фильтр, что уже применяет сама swgoh_comlink при построении
# калькулятора статов — swgoh_comlink/StatCalc/data_builder/_builder_base.py::_build_unit_data
# берёт `unit.obtainable and unit.obtainableTime == "0"`, затем оставляет только запись с
# rarity == 1 как каноническую на baseId. Переиспользуем его один-в-один, а не изобретаем
# свой (например по PVE_-префиксу — он ловит не все случаи, "_DUEL"/"_GLEVENT" NPC-варианты
# без этого префикса тоже встречаются) — так game_units всегда содержит ровно те baseId,
# для которых StatCalc реально умеет считать статы (см. stat_engine.build_stat_calc), не
# больше и не меньше.
_RARITY_ENUM = {
    "ONE_STAR": 1, "TWO_STAR": 2, "THREE_STAR": 3, "FOUR_STAR": 4,
    "FIVE_STAR": 5, "SIX_STAR": 6, "SEVEN_STAR": 7,
}


def _is_canonical_playable_unit(unit: dict) -> bool:
    if not unit.get("obtainable", False) or unit.get("obtainableTime") != "0":
        return False
    rarity = unit.get("rarity", 0)
    if isinstance(rarity, str):
        rarity = _RARITY_ENUM.get(rarity, 0)
    return rarity == 1


async def _fetch_skill_definitions(comlink) -> list:
    """Сырой список способностей (comlink SkillDefinitions) — общий фетч для
    _omicron_capable_base_ids и _skill_tier_thresholds ниже, чтобы не тянуть один и тот же
    справочник дважды за цикл синка."""
    skill_data = await asyncio.to_thread(comlink.get_game_data, items=str(SKILL_DEFINITIONS_FLAG))
    return skill_data.get("skill") or []


async def _fetch_ability_definitions(comlink) -> list:
    """Сырой каталог способностей (comlink AbilityDefinitions) — источник настоящих имён
    зет/омикронов для активности гильдии, см. ABILITY_DEFINITIONS_FLAG выше."""
    ability_data = await asyncio.to_thread(comlink.get_game_data, items=str(ABILITY_DEFINITIONS_FLAG))
    return ability_data.get("ability") or []


def _ability_names(abilities_list: list, loc_kv: dict) -> dict:
    """{ability_id: человекочитаемое имя} через ability.nameKey -> локализация."""
    names = {}
    for a in abilities_list:
        ability_id = a.get("id")
        if not ability_id:
            continue
        name_key = a.get("nameKey")
        names[ability_id] = loc_kv.get(name_key, ability_id) if name_key else ability_id
    return names


def _omicron_capable_base_ids(skills_list: list, units_list: list) -> set:
    """base_id всех юнитов, у которых хотя бы одна реферснутая (unit.skillReference)
    способность имеет тир с isOmicronTier=True."""
    omicron_skill_ids = {
        sk["id"] for sk in skills_list
        if any(t.get("isOmicronTier") for t in (sk.get("tier") or []))
    }
    capable = set()
    for unit in units_list:
        bid = unit.get("baseId")
        if not bid or bid in capable:
            continue
        for ref in (unit.get("skillReference") or []):
            if ref.get("skillId") in omicron_skill_ids:
                capable.add(bid)
                break
    return capable


def _skill_tier_thresholds(skills_list: list, ability_names: dict) -> dict:
    """{skill_id: (zeta_tier|None, omicron_tier|None, name, ability_id)} — 0-based индекс
    ступени способности, на которой она помечена isZetaTier/isOmicronTier=True. Проверено
    живыми данными 2026-08-21 (docker exec на проде): число ступеней и позиция зета/омикрона
    свои у КАЖДОЙ способности (напр. у одной способности зета на индексе 6 из 7, у другой
    омикрон на индексе 7 из 8) — глобального порога вроде "tier >= 8" не существует, что и
    было причиной бага "дзеты/омикроны не пишутся в активности" (services/activity_diff.py
    раньше сравнивал с константами ZETA_TIER=8/OMICRON_MIN_TIER=9, которых игра никогда
    не достигает).

    name/ability_id — для отображения на /activity вместо сырого skill_id и для ссылки на
    swgoh.gg (https://swgoh.gg/units/{base_id}/ability/{ability_id}/1/); резолвятся через
    skill.abilityReference -> ability_names (см. ABILITY_DEFINITIONS_FLAG выше)."""
    thresholds = {}
    for sk in skills_list:
        skill_id = sk.get("id")
        if not skill_id:
            continue
        zeta_tier = None
        omicron_tier = None
        for idx, t in enumerate(sk.get("tier") or []):
            if t.get("isZetaTier"):
                zeta_tier = idx
            if t.get("isOmicronTier"):
                omicron_tier = idx
        if zeta_tier is not None or omicron_tier is not None:
            ability_id = sk.get("abilityReference") or ""
            name = ability_names.get(ability_id, skill_id)
            thresholds[skill_id] = (zeta_tier, omicron_tier, name, ability_id)
    return thresholds


async def sync_units(comlink) -> int:
    game_data = await asyncio.to_thread(comlink.get_game_data, items=str(UNIT_DEFINITIONS_FLAG))
    units_list = game_data.get('units') or []
    if not units_list:
        raise Exception("В полученных данных отсутствует массив персонажей ('units').")

    def _parse_loc(raw: dict, filename: str) -> dict:
        loc_kv = {}
        for line in raw.get(filename, "").split("\n"):
            if "|" not in line:
                continue
            k, _, v = line.partition("|")
            loc_kv[k.strip()] = v.strip()
        return loc_kv

    loc_ru = await asyncio.to_thread(comlink.get_localization, locale="RUS_RU", unzip=True)
    loc_ru_kv = _parse_loc(loc_ru, "Loc_RUS_RU.txt")
    # Английское имя — только для поиска (см. database.search_game_units), чтобы
    # персонажа можно было найти и набрав его имя на английской раскладке.
    loc_en = await asyncio.to_thread(comlink.get_localization, locale="ENG_US", unzip=True)
    loc_en_kv = _parse_loc(loc_en, "Loc_ENG_US.txt")

    units_to_db = {}
    for unit in units_list:
        if not _is_canonical_playable_unit(unit):
            continue
        bid = unit.get('baseId')
        if not bid:
            continue
        name_key = unit.get('nameKey', bid)
        name = loc_ru_kv.get(name_key, name_key)
        name_en = loc_en_kv.get(name_key, name_key)
        unit_type = "ship" if unit.get("combatType") == 2 else "character"
        units_to_db[bid] = (name, unit_type, name_en)

    database.upsert_game_units(units_to_db)
    # Убираем из справочника то, что осталось от синков до фильтра выше (NPC/дубли по
    # редкости) — без этого upsert (INSERT OR REPLACE) их не тронет, они просто зависнут.
    database.prune_game_units(units_to_db.keys())

    # Отдельно от основного справочника (не валим весь sync_units из-за этого) — тот же
    # comlink /data периодически отвечает HTTP 400 на SkillDefinitions, а обновление
    # имён важнее и не должно срываться из-за флага "есть ли омикрон".
    try:
        skills_list = await _fetch_skill_definitions(comlink)
        database.set_omicron_capable_base_ids(_omicron_capable_base_ids(skills_list, units_list))
        abilities_list = await _fetch_ability_definitions(comlink)
        ability_names = _ability_names(abilities_list, loc_ru_kv)
        database.set_skill_tier_thresholds(_skill_tier_thresholds(skills_list, ability_names))
    except Exception as e:
        print(f"⚠️ [Справочник] Не удалось обновить данные об омикронах/зетах: {e}")

    return len(units_to_db)
