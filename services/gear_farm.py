"""Где фармить недостающие детали по картинке C3PO (`inventory unit`) — логика бывшей
Discord-команды /фарм, с 2026-10-14 живёт только на сайте (/farm). Картинку распознаёт
OpenRouter vision (services/openrouter_vision.py, общий дневной лимит с ордером ТБ),
«где фармить» — чистые игровые данные (Comlink `equipment[].lookupMission`,
services/equipment_sync.py): сопоставление названий и жадный подбор локаций.

Почему через ИИ: C3PO вычитает из нужного то, что уже лежит у игрока на складе, а склада
Comlink не отдаёт — картинка C3PO единственный источник итоговых чисел."""

import math

import database

PROMPT = """На картинке ДОЛЖЕН быть снимок экрана от Discord-бота C3PO (команда inventory unit,
Star Wars: Galaxy of Heroes) — список недостающих деталей снаряжения/релик-материалов
персонажа. Характерный вид: заголовки "Additional Gear needed for G<N>" (детали снаряжения)
и/или "Additional Material needed for R<N>" (релик-материалы, числа N любые — это целевой
гир/уровень реликвии), под каждым — строки с иконкой детали слева, названием и количеством
через "x" справа.

СНАЧАЛА проверь: это действительно такой снимок экрана, или что-то другое (случайная картинка,
другой снимок экрана игры/чата, мем, пустое/нечитаемое изображение и т.п.)?

Если это НЕ такой снимок экрана — верни ровно:
{"is_c3po_inventory": false, "gear_tier": null, "relic_tier": null, "items": []}
и дальше ничего не выдумывай.

Если это такой снимок экрана — распознай ВСЕ строки из ОБОИХ разделов (какие есть; один из
разделов может отсутствовать, если по нему всё уже готово) — название детали на английском,
как написано на картинке, и количество после "x".

Верни СТРОГО JSON без markdown-обрамления (без ```), ровно такой структуры:
{
  "is_c3po_inventory": true,
  "gear_tier": "G13" или null (если раздела гира нет на картинке),
  "relic_tier": "R9" или null (если раздела релика нет на картинке),
  "items": [
    {"name": "точное название детали как на картинке", "quantity": 16}
  ]
}
"items" — общий список для обоих разделов вперемешку, без деления на секции в самом JSON."""


def validate_response(data: dict):
    """Отбраковка ответа модели для роутера (services/openrouter_router.py): без поля
    is_c3po_inventory или со списком items не того вида — пробуем другую модель."""
    if "is_c3po_inventory" not in data:
        return "нет поля is_c3po_inventory"
    if data.get("is_c3po_inventory") and not isinstance(data.get("items"), list):
        return "items не список"
    return True


def _choose_recipe_ingredient(ingredients: list, already_needed_ids: set):
    """ingredients: [(ingredient_base_id, point_cost), ...] из database.get_scavenger_recipe.
    Выбирает один "лучший" вариант эвристикой (см. план фичи, чат-обсуждение при реализации):
    1) деталь, которая и так уже нужна напрямую в этом же списке (расшаривает локацию);
    2) иначе — деталь с наименьшим числом известных локаций (проще объяснить "иди туда");
    3) при равенстве — с большей ценой за штуку (нужно меньше ходок).
    Варианты без известных локаций вообще пропускаются. None, если ни у одного нет локаций.
    Совместная оптимизация между несколькими материалами одновременно — осознанно за рамками,
    см. план фичи."""
    best = None
    best_key = None
    for ingredient_id, cost in ingredients:
        locs = database.get_equipment_locations(ingredient_id)
        if not locs:
            continue
        key = (0 if ingredient_id in already_needed_ids else 1, len(locs), -cost)
        if best_key is None or key < best_key:
            best_key = key
            best = (ingredient_id, cost, locs)
    return best


def _greedy_location_plan(base_id_locations: dict) -> list:
    """base_id_locations: {base_id: [метка, ...]}. Возвращает [(метка, [base_id, ...]), ...] —
    жадное (не точное, задача NP-hard) приближение set-cover: на каждом шаге выбираем локацию,
    закрывающую больше всего из ещё непокрытых деталей, вычёркиваем их, повторяем. Для
    типичных 5-20 деталей на список этого достаточно, точный солвер не нужен."""
    remaining = dict(base_id_locations)
    plan = []
    while remaining:
        coverage = {}
        for base_id, labels in remaining.items():
            for label in labels:
                coverage.setdefault(label, []).append(base_id)
        best_label = max(coverage.items(), key=lambda kv: len(kv[1]))[0]
        covered_ids = coverage[best_label]
        plan.append((best_label, covered_ids))
        for base_id in covered_ids:
            remaining.pop(base_id, None)
    return plan


def build_plan_text(data: dict) -> tuple[bool, str]:
    """(ok, текст) по ответу модели. Текст — строки с **жирным** (как было в Discord)."""
    if not data.get("is_c3po_inventory", False):
        return False, ("На картинке не похоже на список деталей от C3PO (inventory unit) — "
                       "проверьте картинку или ссылку на сообщение.")
    items = data.get("items") or []
    if not items:
        return False, ("Похоже на снимок экрана C3PO, но не удалось распознать ни одной детали — "
                       "попробуйте другую картинку.")

    matched = []  # [(base_id, name), ...]
    unmatched = []  # [(name, quantity), ...]
    for item in items:
        name = (item.get("name") or "").strip()
        if not name:
            continue
        quantity = int(item.get("quantity") or 0)
        base_id = database.find_game_equipment_by_name_en(name)
        if base_id:
            matched.append((base_id, name))
        else:
            unmatched.append((name, quantity))

    names_by_base_id = dict(matched)
    matched_ids = set(names_by_base_id.keys())
    base_id_locations = {}
    no_location = []
    # [(материал, ingredient_id, кол-во, нужно_очков), ...] — материалы, которые не
    # фармятся, а собираются переработкой деталей у Мусорщика (см. план фичи/чат: часть
    # релик-материалов, SCV_xxx-семейство, лежит только за ротационными ивентами).
    recycled = []
    for base_id, name in matched:
        recipe = database.get_scavenger_recipe(base_id)
        if recipe:
            points_needed, ingredients = recipe
            choice = _choose_recipe_ingredient(ingredients, matched_ids)
            if choice:
                ingredient_id, cost, locs = choice
                qty = math.ceil(points_needed / cost)
                recycled.append((name, ingredient_id, qty, points_needed, base_id))
                base_id_locations[ingredient_id] = locs
                continue
            no_location.append(f"{name} (есть рецепт переработки, но нет данных о фарме деталей под него)")
            continue

        locs = database.get_equipment_locations(base_id)
        if locs:
            base_id_locations[base_id] = locs
        else:
            no_location.append(name)

    # Имена деталей-ингредиентов под переработку, которых не было в исходном
    # распознанном списке (значит их нет в names_by_base_id) — иначе плану будет
    # нечем подписать локацию, которая их закрывает.
    missing_names = set(base_id_locations.keys()) - names_by_base_id.keys()
    if missing_names:
        names_by_base_id.update(database.get_game_equipment_names(list(missing_names)))

    plan = _greedy_location_plan(base_id_locations)

    # Обратный индекс "деталь → куда идти" (не "локация → что она закрывает") — так вывод
    # читается построчно по списку недостающего, а не отдельным списком локаций, который
    # нужно самому сверять с деталями. Одна и та же локация вполне может повториться в
    # нескольких строках подряд — это и есть "тут фармится сразу несколько деталей",
    # просто показано с точки зрения детали, а не битвы.
    location_by_id = {}
    for label, covered_ids in plan:
        for bid in covered_ids:
            location_by_id[bid] = label

    lines = []
    header_bits = [b for b in (data.get("gear_tier"), data.get("relic_tier")) if b]
    if header_bits:
        lines.append(f"**Цель:** {' / '.join(header_bits)}")
        lines.append("")

    direct_farm_lines = [
        f"🔧 [[eq:{base_id}]]**{name}** — {location_by_id[base_id]}"
        for base_id, name in matched
        if base_id in location_by_id
    ]
    if direct_farm_lines:
        lines.append("**Где фармить:**")
        lines.extend(direct_farm_lines)
        lines.append("")

    if recycled:
        lines.append("**Через переработку у Мусорщика:**")
        for material_name, ingredient_id, qty, points_needed, material_id in recycled:
            ingredient_name = names_by_base_id.get(ingredient_id, ingredient_id)
            ingredient_location = location_by_id.get(ingredient_id, "нет данных о фарме")
            lines.append(
                f"♻️ [[eq:{material_id}]]**{material_name}** — нужно {points_needed} очк.: "
                f"{qty}x [[eq:{ingredient_id}]]{ingredient_name} — {ingredient_location}"
            )
        lines.append("")

    if no_location:
        lines.append("**Нет данных о месте фарма (гильд-магазин/шаттлы/ивентовое):**")
        lines.append(", ".join(no_location))
        lines.append("")

    if unmatched:
        lines.append("**Не нашёл в справочнике (сверьте название вручную):**")
        lines.append(", ".join(f"{n} x{q}" for n, q in unmatched))

    return True, "\n".join(lines).strip() or "Не удалось построить план фарма."
