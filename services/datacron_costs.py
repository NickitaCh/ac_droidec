"""Cumulative datacron upgrade costs from Comlink recipes."""
from collections import Counter
import time

ITEMS = str(70368744177664 | 67108864 | 16384)
LABELS = {'mk1': 'Детали Mk I', 'mk2': 'Детали Mk II', 'mk3': 'Детали Mk III',
          'currency': 'Валюта датакронов'}
REGULAR_LEVELS = (3, 6, 9)
CURRENCY_ICON = 'tex.icon_datacrons_currency'
_cached = None


def _tiers(tiers, recipes, season):
    materials = {f'datacron_set_{season}_upgrade_{i}': f'mk{i}' for i in range(1, 4)}
    materials['DATACRON_CURRENCY'] = 'currency'
    total, result = Counter(), {}
    for tier in sorted(tiers, key=lambda row: int(row['id'])):
        level = int(tier['id'])
        if level == 0:
            continue
        ingredients = recipes.get(tier['upgradeCostRecipeId'])
        if level != len(result) + 1 or not ingredients:
            break
        for item in ingredients:
            name = materials.get(item['id'])
            if not name or item['minQuantity'] != item['maxQuantity']:
                return result
            total[name] += int(item['minQuantity'])
        result[level] = dict(total)
    return result


def catalog(data, now_ms):
    recipes = {row['id']: row['ingredients'] for row in data.get('recipe', [])}
    material_icons = {row['id']: row.get('iconKey') for row in data.get('material', [])}
    result = {}
    for row in sorted(data.get('datacronSet', []), key=lambda item: int(item['id']), reverse=True):
        if int(row['expirationTimeMs']) <= now_ms:
            continue
        season = str(row['id'])
        templates = [t for t in data.get('datacronTemplate', [])
                     if str(t['setId']) == season and not t.get('initialTiers')]
        costs = {}
        for focused, field in ((False, 'tier'), (True, 'focusedTier')):
            variants = [t for t in templates if bool(t.get('focused')) == focused]
            has_override = any(r.get('overrideUpgradeCostRecipeId') for t in variants for r in t['tier'])
            costs[field] = {} if has_override else _tiers(row.get(field, []), recipes, season)
        if all(level in costs['tier'] for level in REGULAR_LEVELS):
            costs['icons'] = {
                LABELS[f'mk{i}']: material_icons.get(f'datacron_set_{season}_upgrade_{i}')
                for i in range(1, 4)
            }
            costs['icons'][LABELS['currency']] = CURRENCY_ICON
            result[season] = costs
    return result


def get_catalog(comlink):
    global _cached
    now = time.time()
    if _cached and now - _cached[0] < 3600:
        return _cached[1]
    result = catalog(comlink.get_game_data(items=ITEMS), now * 1000)
    if not result:
        raise ValueError('Не удалось получить рецепты прокачки ДК.')
    _cached = now, result
    return result


def calculate(costs, counts, focused):
    if any(not 0 <= count <= 999 for count in counts.values()):
        raise ValueError('Укажите от 0 до 999 обычных ДК каждого уровня.')
    parts = focused.split(',') if focused.strip() else []
    if len(parts) > 100:
        raise ValueError('Укажите до 100 спец-ДК.')
    levels = {str(level): level for level in costs['focusedTier']}
    if any(part.strip() not in levels for part in parts):
        maximum = max(levels.values(), default=0)
        raise ValueError(f'Введите уровни спец-ДК от 1 до {maximum} через запятую.'
                         if maximum else 'В этом сезоне нет доступных рецептов спец-ДК.')
    if not any(counts.values()) and not parts:
        raise ValueError('Укажите хотя бы один ДК.')
    total = Counter()
    for level, count in counts.items():
        if count:
            total.update({key: value * count for key, value in costs['tier'][level].items()})
    for part in parts:
        total.update(costs['focusedTier'][levels[part.strip()]])
    return [(label, total[key]) for key, label in LABELS.items()]
