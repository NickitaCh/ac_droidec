"""Named protobuf fields recovered from the SWGOH Android client.

Only fields selected for the test lab are decoded. Unknown fields are counted,
and their bytes are never rendered. '[]' marks a repeated embedded message.
"""
from collections import Counter

from services import game_account as ga

SCHEMAS = {
    "initial": {
        2: ("Игрок", "player"), 5: ("Инвентарь", "inventory"),
        13: ("События", "event[]"), 16: ("Почта", "inbox"),
        18: ("Магазин", "store"), 20: ("Ежедневные награды", "daily[]"),
        21: ("Задания", "challenge[]"), 31: ("Таймеры", "cooldown[]"),
        33: ("Дневные ограничения", "cap[]"), 50: ("ТБ", "tb[]"),
        52: ("ВГ", "tw[]"), 56: ("Великая арена", "arena[]"),
        71: ("Завоевание", "conquest"),
    },
    "inventory": {
        1: ("Персонажи и корабли", "unit[]"), 2: ("Отряды", "squad[]"),
        3: ("Флоты", "squad[]"), 5: ("Материалы и осколки", "item[]"),
        9: ("Валюты", "currency[]"), 10: ("Снаряжение", "item[]"),
        13: ("Свободные модули", "mod[]"), 14: ("Жетоны ускорения", "token[]"),
    },
    "unit": {2: ("Персонаж или корабль", "text"), 8: ("Звёзды", "int"),
             9: ("Уровень", "int"), 19: ("Уровень снаряжения", "int"),
             21: ("Надетые модули", "mod[]"), 22: ("Реликвия", "relic")},
    "relic": {1: ("Уровень игры", "int")},
    "squad": {1: ("Ячейки отряда", "count"), 4: ("Тип", "int")},
    "item": {1: ("Предмет", "text"), 2: ("Количество", "int"), 3: ("Бонус", "int")},
    "currency": {1: ("Валюта", "currency_name"), 2: ("Количество", "int"), 3: ("Бонус", "int")},
    "token": {2: ("Истекает", "int"), 3: ("Жетон", "text")},
    "mod": {2: ("Модуль", "mod_definition"), 4: ("Уровень", "int"), 5: ("Качество", "int"),
            11: ("Заблокирован", "bool"), 12: ("Основная характеристика", "mod_stat"),
            13: ("Дополнительные характеристики", "mod_stat[]"), 18: ("Перекалибровки", "int")},
    "mod_stat": {2: ("Характеристика", "stat"), 4: ("Улучшений", "int")},
    "stat": {3: ("Характеристика", "stat_name"), 5: ("Масштабированное значение", "int"),
             6: ("Точное значение", "int")},
    "player": {3: ("Уровень", "int"), 4: ("Энергия", "energy[]"), 7: ("Имя", "text")},
    "energy": {1: ("Тип энергии", "int"), 2: ("Метка восстановления", "int"),
               3: ("Максимум", "int"), 4: ("Интервал восстановления", "int"), 6: ("Сверх лимита", "int")},
    "event": {1: ("Событие", "text"), 3: ("Ключ названия", "text"), 7: ("Тип", "int"),
              10: ("Периоды", "event_period[]"), 12: ("Статус", "int")},
    "event_period": {2: ("Начало", "int"), 3: ("Конец", "int"), 14: ("Участвует", "bool")},
    "inbox": {1: ("Письма", "mail[]")},
    "mail": {2: ("Ключ темы", "text"), 3: ("Тема", "text"), 6: ("Истекает", "int"),
             7: ("Вложения", "count"), 8: ("Тип", "int")},
    "store": {10: ("Поставки", "shipment[]")},
    "shipment": {1: ("Тип", "int"), 2: ("Предложения", "shipment_item[]"), 3: ("Обновление", "int")},
    "shipment_item": {12: ("Товар", "text"), 14: ("Стоимость", "count"), 17: ("Куплено", "bool")},
    "daily": {5: ("Номер награды", "int"), 7: ("Награды", "count"), 8: ("Получена", "bool"),
              9: ("Цикл", "text")},
    "challenge": {1: ("Задание", "text"), 2: ("Прогресс", "int"), 3: ("Цель", "int"),
                  4: ("Награда получена", "bool"), 5: ("Истекает", "int")},
    "cooldown": {1: ("Таймер", "text"), 2: ("До", "int")},
    "cap": {1: ("Ограничение", "text"), 2: ("Действий", "int"), 3: ("Покупок", "int"),
            4: ("Сброс", "int")},
    "loadouts": {1: ("Вкладки", "tab[]")},
    "tab": {2: ("Вкладка", "text"), 3: ("Наборы", "loadout[]")},
    "loadout": {2: ("Набор", "text"), 3: ("Модули в наборе", "count")},
    "filters": {1: ("Фильтры", "filter[]")},
    "filter": {2: ("Фильтр", "text"), 3: ("Только свободные", "bool"),
               4: ("Только вне наборов", "bool"), 5: ("Ячейка", "int"),
               6: ("Комплект", "int"), 7: ("Основная характеристика", "stat_name"),
               12: ("Редкость", "int"), 13: ("Качество", "int")},
    "conquest_response": {1: ("Прогресс", "conquest"), 2: ("Энергия", "energy")},
    "conquest": {6: ("Событие", "text"), 7: ("Сложность", "int"),
                 9: ("Установленные диски", "count"), 10: ("Свободные диски", "count"),
                 11: ("Узлы", "node[]"), 12: ("Расходники", "count"),
                 13: ("Активные расходники", "count"), 14: ("Выносливость", "stamina[]"),
                 19: ("Пропуск активен", "bool")},
    "node": {1: ("Прогресс", "int"), 2: ("Узел", "text"), 3: ("Сектор", "text")},
    "stamina": {1: ("Персонаж", "text"), 2: ("Остаток выносливости", "int")},
    "territory": {1: ("Текущая ТБ", "tb[]"), 2: ("Итоги ТБ", "tb_result[]"),
                  3: ("Текущая ВГ", "tw[]"), 4: ("Великая арена", "arena[]")},
    "tb": {2: ("Карта", "text"), 5: ("Планеты", "conflict[]"), 6: ("Боевые миссии", "strike[]"),
           7: ("Взводы", "recon[]"), 9: ("Этап", "int"), 11: ("Конец этапа", "int"),
           13: ("Особые миссии", "covert[]"), 14: ("Статистика", "map_stat[]")},
    "conflict": {5: ("Зона", "zone")},
    "strike": {3: ("Участников", "int"), 5: ("Зона", "zone")},
    "recon": {6: ("Зона", "zone"), 7: ("Взводы", "count")},
    "covert": {1: ("Зона", "zone"), 2: ("Участников", "int"), 3: ("Успешных попыток", "int")},
    "zone": {1: ("Зона", "text"), 2: ("Статус", "int"), 3: ("Очки", "int")},
    "map_stat": {1: ("Показатель", "text"), 2: ("Игроки", "player_stat[]")},
    "player_stat": {1: ("Игрок", "text"), 2: ("Очки", "int")},
    "tb_result": {2: ("Карта", "text"), 3: ("Завершение", "int"), 4: ("Звёзды", "int"),
                  5: ("Статистика", "map_stat[]")},
    "tw": {2: ("Карта", "text"), 3: ("Раунд", "int"), 4: ("Конец раунда", "int"),
           6: ("Своя гильдия", "tw_guild"), 7: ("Соперник", "tw_guild"),
           9: ("Статистика", "map_stat[]"), 11: ("Участники", "count")},
    "tw_guild": {2: ("Территории", "tw_conflict[]"), 3: ("Разведка", "count")},
    "tw_conflict": {1: ("Зона", "zone"), 2: ("Вместимость", "int"),
                    3: ("Отряды", "tw_squad[]"), 4: ("Выставлено", "int"), 5: ("Побеждено", "int")},
    "tw_squad": {2: ("Игрок", "text"), 3: ("Мощь", "int"),
                 6: ("Статус", "int"), 8: ("Успешных защит", "int")},
    "arena": {2: ("Матчи", "arena_match[]"), 4: ("Конец матча", "int"),
              5: ("Участвует", "bool"), 10: ("Рейтинг", "int"),
              11: ("Лига", "int"), 12: ("Дивизион", "int")},
    "arena_match": {1: ("Карта", "text"), 5: ("Раунд", "int"), 6: ("Конец раунда", "int"),
                    11: ("Статус матча", "int"), 15: ("Начало матча", "int")},
    "raid_response": {1: ("Текущий рейд", "raid[]"), 2: ("Результаты", "raid_result[]"),
                     3: ("Прогресс", "raid_progress[]"), 4: ("Награды", "count")},
    "raid": {1: ("Рейд", "text"), 2: ("Завершение", "int"), 15: ("Участвовал", "bool"),
             17: ("Статус", "int"), 19: ("Положены награды", "bool"),
             23: ("Игроки", "raid_member[]"), 27: ("Личные очки", "int"), 28: ("Очки гильдии", "int")},
    "raid_result": {1: ("Рейд", "text"), 7: ("Игроки", "raid_member[]"), 8: ("Завершение", "int"),
                    11: ("Очки гильдии", "int")},
    "raid_progress": {1: ("Рейд", "text"), 2: ("Прогресс", "int"), 3: ("Побед", "int")},
    "raid_member": {1: ("Игрок", "text"), 2: ("Очки", "int"), 3: ("Место", "int"), 4: ("Попыток", "int")},
}

# The names are keyed by the actual enum values, which are sparse.
CURRENCIES = {
    1: "Кредиты", 2: "Кристаллы", 4: "Очки союзников", 10: "Валюта арены отрядов",
    12: "Валюта арены флотов", 15: "Симуляционные билеты", 16: "Валюта осколков",
    17: "Валюта гильдии", 18: "Билеты гильдии", 19: "Кредиты флота",
    32: "Жетоны события гильдии I", 33: "Валюта Великой арены",
    34: "Жетоны события гильдии II", 39: "Валюта Завоевания",
    41: "Материалы перекалибровки модулей", 42: "Жетоны события гильдии III",
    43: "Рейдовые жетоны I", 44: "Рейдовые жетоны II", 45: "Рейдовые жетоны III",
}
MAX_SHOWN = 100
MAX_NODES = 3000


def decode(raw, schema):
    budget = [MAX_NODES]
    rows = ga.fields(raw)
    if schema == "initial":
        rows.sort(key=lambda row: row[0] != 5)
    return _decode(rows, schema, budget)


def _decode(fields, schema, budget):
    spec = SCHEMAS[schema]
    if schema == "inventory":
        # Large rosters must not crowd out private balances and free mods.
        priority = {9: 0, 5: 1, 10: 2, 13: 3, 14: 4}
        fields = sorted(fields, key=lambda row: priority.get(row[0], 5))
    result = []
    skipped = Counter()
    counts = Counter()
    occurrences = Counter()
    groups = {}
    for number, wire, value in fields:
        if number not in spec:
            skipped[number] += 1
            continue
        label, kind = spec[number]
        if kind == "count":
            counts[label] += 1
            continue
        occurrences[number] += 1
        if occurrences[number] > MAX_SHOWN or budget[0] == 0:
            continue
        budget[0] -= 1
        try:
            decoded = _value(wire, value, kind, budget)
        except (UnicodeError, ga.AccountError, ValueError):
            skipped[number] += 1
            continue
        if kind.endswith("[]"):
            entry = next((value for _, value in decoded if isinstance(value, str)), str(occurrences[number]))
            groups.setdefault(label, []).append((entry, decoded))
        else:
            result.append((label, decoded))
    for label, rows in groups.items():
        result.append((f"{label} · {len(rows)}", rows))
    result.extend(counts.items())
    for number, count in occurrences.items():
        if count > MAX_SHOWN:
            result.append((spec[number][0] + " · всего записей", count))
    if schema == "stat":
        from services.mod_search import STAT_ID_TO_INFO
        from stat_engine import UNIT_STAT_RU
        values = {n: value for n, wire, value in fields if wire == 0}
        info = STAT_ID_TO_INFO.get(values.get(3))
        if info and (5 in values or 6 in values):
            name, percent = info
            raw = values[6] if 6 in values else values[5] * 10_000
            if raw >= 1 << 63 and 6 in values:
                raw -= 1 << 64
            display = f"{raw / (1_000_000 if percent else 100_000_000):g}"
            result = [(UNIT_STAT_RU.get(values[3], name.removesuffix(" %")), display + ("%" if percent else ""))]
    if skipped:
        result.append(("Поля без расшифровки", ", ".join(f"№{n} × {count}" for n, count in sorted(skipped.items()))))
    if budget[0] == 0:
        result.append(("Вывод ограничен", "Достигнут предел записей на странице"))
    return result


def _value(wire, value, kind, budget):
    if kind in {"int", "bool", "currency_name", "stat_name"}:
        if wire != 0:
            raise ValueError("Expected varint")
        if kind == "bool":
            return "Да" if value else "Нет"
        if kind == "currency_name":
            return CURRENCIES.get(value, f"Валюта №{value}")
        if kind == "stat_name":
            from stat_engine import UNIT_STAT_RU
            return UNIT_STAT_RU.get(value, f"Характеристика №{value}")
        return value
    if wire != 2:
        raise ValueError("Expected length-delimited field")
    if kind == "mod_definition":
        from services.mod_search import SLOT_ID_TO_KEY, SLOT_KEY_TO_LABEL
        from stat_engine import MOD_SET_NAMES_RU
        definition = value[:256].decode("utf-8")
        if len(definition) == 3 and definition.isascii() and definition.isdigit():
            set_id, rarity, slot_id = map(int, definition)
            slot = SLOT_KEY_TO_LABEL.get(SLOT_ID_TO_KEY.get(slot_id), f"Ячейка №{slot_id}")
            return f"{slot} · {MOD_SET_NAMES_RU.get(set_id, f'Комплект №{set_id}')} · {rarity}★"
        return definition
    if kind == "text":
        return value[:256].decode("utf-8")
    return _decode(ga.fields(value), kind.removesuffix("[]"), budget)
