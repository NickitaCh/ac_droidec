"""Обновление глобального справочника снаряжения/релик-материалов (game_equipment +
game_equipment_locations) из Comlink — тот же паттерн, что services/units_sync.py::sync_units
(comlink.get_game_data по битовому флагу + comlink.get_localization + "|"-разделённый парсинг),
но для деталей и мест их фарма. Источник для /фарм (cogs/gear_farm.py, см. CLAUDE.md-style
контекст в этом файле): распознав список недостающих деталей с картинки C3PO через vision,
/фарм сопоставляет английские названия с game_equipment.name_en и подсказывает локации из
game_equipment_locations — без обращения к ИИ на этом шаге, чистые игровые данные.

Флаги подтверждены живыми пробами на боевом Comlink (docker exec на VPS, тот же приём, что и
UNIT_DEFINITIONS_FLAG в units_sync.py): 'EquipmentDefinitions'=8, 'Campaign'=274877906944,
'Material'=16384, 'ScavengerConversionSet'=268435456 — значения из
swgoh_comlink.helpers._data_items.DataItems, но сюда вписаны как обычные int-константы, а не
импорт всего DataItems enum — тот же стиль, что и остальные *_DEFINITIONS_FLAG в этом проекте
(units_sync.py, datacron_requirements.py).

Релик-материалы (Comlink-коллекция material, НЕ equipment) — отдельный источник со своим
lookupMission (та же схема), кладутся в тот же справочник game_equipment/game_equipment_locations
(ID-неймспейсы не пересекаются: у material — "RM_xxx"/"SCV_xxx", у equipment — короткие
числовые вроде "158Prototype", проверено вживую). Часть материалов (SCV_xxx-семейство —
Aeromagnifier, Bronzium Wiring и т.п.) в принципе не фармится битвами: их lookupMission
указывает только на ротационные ивенты (см. _process_item_list ниже, та же приоритизация
"постоянное > ивентовое" уже отфильтровывает такие в пользу пустого списка локаций) — они
собираются переработкой деталей снаряжения у "Мусорщика" (Comlink-коллекция
scavengerConversionSet, см. sync_equipment) в game_scavenger_recipes.
"""

import asyncio
import re

import database

EQUIPMENT_DEFINITIONS_FLAG = 8
CAMPAIGN_FLAG = 274877906944
MATERIAL_FLAG = 16384
SCAVENGER_CONVERSION_SET_FLAG = 268435456

# Человекочитаемые названия кампаний — их всего ~16 (id из живого пробника: C00, C01D, C01L,
# C01H, EVENTS, GUILD, C01MB, C01SP, SHIP_EVENTS, t01D..t05D, TW_EVENTS, ERA), campaign.nameKey
# резолвится через локализацию в КАПС С ПЕРЕНОСОМ СТРОКИ ("DARK SIDE\nBATTLES" — некрасиво для
# вывода в Discord), поэтому — по образцу PLANET_RU в cogs/tb_order_image.py — короткий
# ручной справочник вместо автоматической резолвнутой строки. Кампании, которых тут нет
# (незнакомый id, встретится при добавлении новых событий в игру) — просто используем campaignId
# как есть, не падаем.
CAMPAIGN_LABELS = {
    "C00": "Обучение",
    "C01D": "Тёмная сторона",
    "C01L": "Светлая сторона",
    "C01H": "Голокрон",
    "EVENTS": "События",
    "GUILD": "Гильдия",
    "C01MB": "Битвы модов",
    "C01SP": "Флот",
    "SHIP_EVENTS": "Флот: события",
    "TW_EVENTS": "Война территорий",
    "ERA": "Эра",
}

# Ключевые слова сложности, встречающиеся в mission.descKey/unlockRequirementLocalizationKey
# (например "MISSION_TU40_DARKSIDEBATTLE_HARD_9A_DESC", "..._HARD_DIFF_KEY") — единого
# отдельного поля-перечисления с готовым текстом сложности в данных нет (campaignNodeDifficulty
# — просто число, значение которого зависит от конкретной главы/кампании, не глобальный enum),
# это находка эмпирическим сопоставлением на живых данных (см. чат-обсуждение при реализации
# /фарм). Порядок проверки важен — "VERYHARD" должен проверяться раньше "HARD".
_DIFFICULTY_WORDS = [
    ("VERYHARD", "очень сложный"),
    ("HARD", "сложный"),
    ("MEDIUM", "средний"),
    ("NORMAL", "обычный"),
    ("EASY", "лёгкий"),
]


def _parse_loc(raw: dict, filename: str) -> dict:
    loc_kv = {}
    for line in raw.get(filename, "").split("\n"):
        if "|" not in line:
            continue
        k, _, v = line.partition("|")
        loc_kv[k.strip()] = v.strip()
    return loc_kv


def _difficulty_word(*texts: str) -> str:
    combined = " ".join(t or "" for t in texts).upper()
    for marker, label in _DIFFICULTY_WORDS:
        if marker in combined:
            return label
    return ""


def _build_mission_index(campaign_list: list) -> dict:
    """{(campaignId, campaignMapId, campaignNodeId, campaignNodeDifficulty, campaignMissionId):
    (mission_dict, node_dict, campaign_dict)} — плоский индекс по всему дереву кампаний, чтобы
    по идентификатору из equipment[].lookupMission находить реальный узел/миссию без повторного
    обхода дерева на каждую деталь (~709 деталей, у части по несколько lookupMission)."""
    index = {}
    for camp in campaign_list:
        camp_id = camp.get("id")
        for cmap in camp.get("campaignMap") or []:
            map_id = cmap.get("id")
            for group in cmap.get("campaignNodeDifficultyGroup") or []:
                difficulty = group.get("campaignNodeDifficulty")
                for node in group.get("campaignNode") or []:
                    node_id = node.get("id")
                    for mission in node.get("campaignNodeMission") or []:
                        mission_id = mission.get("id")
                        key = (camp_id, map_id, node_id, difficulty, mission_id)
                        index[key] = (mission, node, camp, group)
    return index


_NUMBERED_NODE_RE = re.compile(r"^N\d+$")


def _readable_node_id(node_id: str | None, map_id: str | None) -> str:
    raw = (node_id or map_id or "?").replace("_MYTHIC", "")
    return raw.replace("_", " ").title()


def _location_label(mission_identifier: dict, mission_index: dict, loc_kv: dict) -> str | None:
    camp_id = mission_identifier.get("campaignId")
    map_id = mission_identifier.get("campaignMapId")
    node_id = mission_identifier.get("campaignNodeId")
    difficulty = mission_identifier.get("campaignNodeDifficulty")
    mission_id = mission_identifier.get("campaignMissionId")

    campaign_label = CAMPAIGN_LABELS.get(camp_id, camp_id or "?")
    mythic_suffix = " (мифический)" if node_id and "MYTHIC" in node_id else ""
    # Обычные кампании (Тёмная/Светлая сторона и т.п.) нумеруют узлы "N01", "N02"... —
    # для них короткая метка вида "9-А" (mission.shortNameKey) достаточно однозначна.
    # А вот ивентовые/мифические узлы (campaignId="EVENTS") используют описательный
    # node_id ("GRANDMASTERS_TRAINING_MYTHIC") — там сам shortNameKey резолвится в общий
    # "Tier I"/"Tier II" БЕЗ имени события (проверено на живых данных: несколько разных
    # мифических квестов резолвятся в один и тот же текст "Tier II"), поэтому для таких
    # узлов имя события важнее короткой метки — она уходит в скобки как уточнение тира,
    # а не заменяет собой название.
    is_numbered_node = bool(node_id) and _NUMBERED_NODE_RE.match(node_id) is not None

    key = (camp_id, map_id, node_id, difficulty, mission_id)
    found = mission_index.get(key)

    if found is None:
        # Не нашли точный узел (сложность может не совпасть буквально в редких случаях,
        # либо структура кампании отличается от ожидаемой) — деградируем до менее точной,
        # но всё ещё полезной метки, а не тихо теряем локацию.
        return f"{campaign_label}: {_readable_node_id(node_id, map_id)}{mythic_suffix}"

    mission, node, camp, group = found
    short_key = mission.get("shortNameKey")
    short_label = loc_kv.get(short_key) if short_key else None
    if not short_label or short_label == short_key:
        short_label = None  # SHORT_*_NAME не резолвится — просто не показываем тир/номер

    diff_word = _difficulty_word(
        mission.get("descKey", ""),
        group.get("unlockRequirementLocalizationKey", ""),
    )

    if is_numbered_node:
        base = short_label or _readable_node_id(node_id, map_id)
        suffix = f" ({diff_word})" if diff_word else ""
        return f"{campaign_label} {base}{suffix}{mythic_suffix}"

    event_name = _readable_node_id(node_id, map_id)
    tier_suffix = f" ({short_label})" if short_label else (f" ({diff_word})" if diff_word else "")
    return f"{campaign_label}: {event_name}{tier_suffix}{mythic_suffix}"


def _process_item_list(items: list, loc_ru_kv: dict, loc_en_kv: dict, mission_index: dict) -> tuple[dict, dict]:
    """Общая обработка и для equipment[], и для material[] — обе коллекции используют
    одинаковую схему (id/nameKey/lookupMission), см. докстринг модуля. Возвращает
    (item_to_db, locations_by_base_id) для последующего объединения с другой коллекцией."""
    item_to_db = {}
    locations_by_base_id = {}
    for item in items:
        base_id = item.get("id")
        if not base_id:
            continue
        name_key = item.get("nameKey", base_id)
        name_ru = loc_ru_kv.get(name_key, name_key)
        name_en = loc_en_kv.get(name_key, name_key)
        item_to_db[base_id] = (name_ru, name_en)

        repeatable_labels = []  # обычные кампании — фармятся каждый день, всегда доступны
        event_labels = []  # EVENTS/ивенты/мифические квесты — не всегда активны
        for lookup in item.get("lookupMission") or []:
            identifier = lookup.get("missionIdentifier") or {}
            if not identifier:
                continue
            label = _location_label(identifier, mission_index, loc_en_kv)
            if not label:
                continue
            target = event_labels if identifier.get("campaignId") == "EVENTS" else repeatable_labels
            if label not in target:
                target.append(label)

        # Постоянные (всегда доступные) локации приоритетнее — если они есть, ивентовые/
        # временные (лимитированные "Flash"-события и т.п., подтверждено на живых данных: у
        # некоторых деталей их набирается до 60+ штук на разные уровни одного и того же
        # ивента) не показываем вообще, чтобы не рекомендовать фармить то, что может быть
        # недоступно прямо сейчас. Если постоянных нет — деталь/материал падает ТОЛЬКО с
        # ивентов/мифических квестов (характерно для части релик-материалов — SCV_xxx-семейство,
        # см. докстринг модуля), тогда берём их (это по-прежнему полезная информация), но
        # обрезаем до первых 5, чтобы не заваливать вывод командой /фарм десятками почти
        # одинаковых вариаций одного и того же события.
        labels = repeatable_labels if repeatable_labels else event_labels[:5]
        if labels:
            locations_by_base_id[base_id] = labels

    return item_to_db, locations_by_base_id


async def sync_equipment(comlink) -> int:
    """Возвращает число деталей+материалов в обновлённом справочнике. Раздельные запросы
    (не побитовое ИЛИ) — та же причина, что и раздельные запросы в units_sync.py: CAMPAIGN —
    тяжёлые данные (полное дерево узлов всех кампаний), рисковать таймаутом/HTTP 400 на
    объединённом запросе не стоит."""
    equip_data = await asyncio.to_thread(comlink.get_game_data, items=str(EQUIPMENT_DEFINITIONS_FLAG))
    equipment_list = equip_data.get("equipment") or []
    if not equipment_list:
        raise Exception("В полученных данных отсутствует массив снаряжения ('equipment').")

    material_data = await asyncio.to_thread(comlink.get_game_data, items=str(MATERIAL_FLAG))
    material_list = material_data.get("material") or []

    camp_data = await asyncio.to_thread(comlink.get_game_data, items=str(CAMPAIGN_FLAG))
    campaign_list = camp_data.get("campaign") or []

    loc_ru = await asyncio.to_thread(comlink.get_localization, locale="RUS_RU", unzip=True)
    loc_ru_kv = _parse_loc(loc_ru, "Loc_RUS_RU.txt")
    loc_en = await asyncio.to_thread(comlink.get_localization, locale="ENG_US", unzip=True)
    loc_en_kv = _parse_loc(loc_en, "Loc_ENG_US.txt")

    mission_index = _build_mission_index(campaign_list)

    equipment_to_db, equipment_locations = _process_item_list(equipment_list, loc_ru_kv, loc_en_kv, mission_index)
    material_to_db, material_locations = _process_item_list(material_list, loc_ru_kv, loc_en_kv, mission_index)

    # Общий справочник для equipment и material — ID-неймспейсы не пересекаются (проверено
    # вживую: у material — "RM_xxx"/"SCV_xxx", у equipment — короткие числовые), поэтому
    # /фарм (cogs/gear_farm.py::find_game_equipment_by_name_en) сопоставляет оба вида деталей
    # одним и тем же поиском, без отдельной ветки под материалы.
    equipment_to_db.update(material_to_db)
    locations_by_base_id = {**equipment_locations, **material_locations}

    database.upsert_game_equipment(equipment_to_db)
    database.prune_game_equipment(equipment_to_db.keys())
    database.set_all_equipment_locations(locations_by_base_id)

    # Рецепты переработки у "Мусорщика" — часть материалов (SCV_xxx-семейство) не имеет
    # надёжных локаций фарма выше (только ивентовые), а собирается сдачей деталей снаряжения.
    # Имена ингредиентов резолвятся из equipment_to_db, который уже содержит все нужные детали
    # (consumable в scavengerConversionSet — это equipment id) — доп. запроса не нужно.
    scav_data = await asyncio.to_thread(comlink.get_game_data, items=str(SCAVENGER_CONVERSION_SET_FLAG))
    for conv_set in scav_data.get("scavengerConversionSet") or []:
        output_item = (conv_set.get("output") or {}).get("item") or {}
        material_id = output_item.get("id")
        points_needed = output_item.get("pointValue")
        if not material_id or not points_needed:
            continue
        ingredients = [
            (c.get("id"), c.get("pointValue"))
            for c in (conv_set.get("consumable") or [])
            if c.get("id") and c.get("pointValue")
        ]
        if ingredients:
            database.set_scavenger_recipe(material_id, points_needed, ingredients)

    return len(equipment_to_db)
