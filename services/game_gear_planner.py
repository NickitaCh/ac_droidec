"""Calculate upgrade deficits against a single private inventory read."""
from collections import Counter
import json

import database
from services import game_account as ga

CURRENCY_NAMES = {
    "GRIND": "Кредиты", "PREMIUM": "Кристаллы", "SOCIAL": "Очки союзников",
    "SHIPGRIND": "Кредиты флота", "PVPCURRENCY": "Валюта арены отрядов",
    "PVPSHIPCURRENCY": "Валюта арены флотов", "WARSHIPCURRENCY": "Валюта галактической войны",
    "SHARDCURRENCY": "Валюта осколков", "GUILDCURRENCY": "Валюта гильдии",
    "TERRITORYBATTLECURRENCY": "Жетоны события гильдии I",
    "TERRITORYBATTLECURRENCY02": "Жетоны события гильдии II",
    "TERRITORYBATTLECURRENCY03": "Жетоны события гильдии III",
    "SEASONSCURRENCY": "Валюта Великой арены", "CONQUESTCURRENCY": "Валюта Завоевания",
    "RAIDREWARDCURRENCY01": "Рейдовые жетоны I", "RAIDREWARDCURRENCY02": "Рейдовые жетоны II",
    "RAIDREWARDCURRENCY03": "Рейдовые жетоны III",
}
ITEM_NAMES = {
    f"xp-mat-{word}-star": f"Тренировочный дроид · {stars}★"
    for stars, word in enumerate(("one", "two", "three", "four", "five", "six", "seven"), 1)
}


def unit_progress(raw):
    rows = ga.fields(raw)
    base_id = ga._one(rows, 2, 2).decode().split(":")[0]
    gear = next((v for n, w, v in rows if (n, w) == (19, 0)), 0)
    relic = 0
    equipped = Counter()
    for number, wire, raw_part in rows:
        if wire == 2 and number == 18:
            equipped[ga._one(ga.fields(raw_part), 1, 2).decode()] += 1
        elif wire == 2 and number == 22:
            tier = next((v for n, w, v in ga.fields(raw_part) if (n, w) == (1, 0)), 0)
            relic = max(0, tier - 2)
    return base_id, gear, relic, equipped


def _requirements(base_id, gear, relic, equipped, kind, target):
    if kind == "gear" and 1 <= target <= 13:
        if target <= gear:
            return Counter()
        tiers = [(base_id, tier) for tier in range(gear, target)]
        recipes = database.get_gear_recipe_items(tiers)
        if any(not recipes.get(tier) for tier in tiers):
            raise ga.AccountError("В справочнике нет полного рецепта для выбранной цели.")
        needed = Counter(recipes[tiers[0]]) - equipped
        for tier in tiers[1:]:
            needed.update(recipes[tier])
        return needed
    if kind == "relic" and 1 <= target <= 10:
        if gear < 13:
            raise ga.AccountError("Для расчёта реликвии нужен персонаж со снаряжением G13.")
        needed = Counter()
        for level in range(relic + 1, target + 1):
            recipe = database.get_relic_promotion_recipe_range(level - 1, level)
            if not recipe:
                raise ga.AccountError(f"В справочнике нет рецепта R{level}.")
            needed.update(recipe)
        return needed
    raise ga.AccountError("Выберите цель от G1 до G13 или от R1 до R10.")


def _allocate(requirements, stock, recipes):
    ordered = []
    visiting, visited = set(), set()

    def visit(item):
        if item in visiting:
            raise ga.AccountError("В справочнике найден циклический рецепт снаряжения.")
        if item in visited:
            return
        visiting.add(item)
        for part, _ in recipes.get(item, []):
            visit(part)
        visiting.remove(item)
        visited.add(item)
        ordered.append(item)

    for item in requirements:
        visit(item)
    demand = Counter(requirements)
    rows = []
    for item in reversed(ordered):
        needed = demand[item]
        if not needed:
            continue
        available = stock.get(item, 0)
        deficit = max(0, needed - available)
        parts = recipes.get(item, [])
        rows.append((item, needed, available, 0 if parts else deficit, deficit if parts else 0))
        for part, quantity in parts:
            demand[part] += deficit * quantity
    return rows


def calculate(unit, kind, target, stock):
    needed = _requirements(*unit, kind, target)
    raw_recipes = database.get_bot_state("inventory_gear_crafting", guild_id=0) if needed and kind == "gear" else None
    rows = _allocate(needed, stock, json.loads(raw_recipes) if raw_recipes else {})
    names = database.get_game_equipment_names([row[0] for row in rows])
    return [(names.get(item) or CURRENCY_NAMES.get(item) or ITEM_NAMES.get(item, item),
             count, available, missing, craft) for item, count, available, missing, craft in rows], bool(raw_recipes)
