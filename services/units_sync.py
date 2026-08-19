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


async def _fetch_omicron_capable_base_ids(comlink, units_list: list) -> set:
    """base_id всех юнитов, у которых хотя бы одна реферснутая (unit.skillReference)
    способность имеет тир с isOmicronTier=True (comlink SkillDefinitions). units_list
    переиспользуется из уже сделанного в sync_units() вызова UnitDefinitions, чтобы не
    тянуть тот же справочник второй раз."""
    skill_data = await asyncio.to_thread(comlink.get_game_data, items=str(SKILL_DEFINITIONS_FLAG))
    skills_list = skill_data.get("skill") or []
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
        bid = unit.get('baseId')
        if not bid:
            continue
        name_key = unit.get('nameKey', bid)
        name = loc_ru_kv.get(name_key, name_key)
        name_en = loc_en_kv.get(name_key, name_key)
        unit_type = "ship" if unit.get("combatType") == 2 else "character"
        units_to_db[bid] = (name, unit_type, name_en)

    database.upsert_game_units(units_to_db)

    # Отдельно от основного справочника (не валим весь sync_units из-за этого) — тот же
    # comlink /data периодически отвечает HTTP 400 на SkillDefinitions, а обновление
    # имён важнее и не должно срываться из-за флага "есть ли омикрон".
    try:
        omicron_ids = await _fetch_omicron_capable_base_ids(comlink, units_list)
        database.set_omicron_capable_base_ids(omicron_ids)
    except Exception as e:
        print(f"⚠️ [Справочник] Не удалось обновить флаг has_omicron: {e}")

    return len(units_to_db)
