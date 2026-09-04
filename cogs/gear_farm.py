"""/фарм — по прямой ссылке на сообщение C3PO (`inventory unit`) распознаёт картинку со
списком недостающих деталей снаряжения/релик-материалов через OpenRouter vision (тот же
OPENROUTER_API_KEY, что и cogs/tb_order_image.py — см. services/openrouter_vision.py за тем,
почему не Mistral) и подсказывает, на каких битвах их фармить.

Почему через ИИ, хотя сам список "чего не хватает" мы честно посчитать не можем: C3PO вычитает
из общего количества то, что уже лежит у игрока на складе (доступ к аккаунту через что-то вроде
HotUtils) — Comlink складом не располагает и никогда не будет, это подтверждённый потолок API
(см. обсуждение при планировании этой фичи). Картинка C3PO — единственный источник этих
"итоговых" чисел, поэтому распознавание неизбежно. А вот "где фармить" — чистые игровые данные
(Comlink `equipment[].lookupMission`, справочник — services/equipment_sync.py), тут ИИ не нужен:
сопоставление названий и подбор локаций (жадный set-cover) — обычный код.
"""

import asyncio
import math

import disnake
from disnake.ext import commands

import database
import guild_resolver
from services.message_image import extract_channel_id, extract_message_id, guess_mime_type, is_image_attachment
from services.openrouter_vision import call_vision_json, daily_used_ratio, OPENROUTER_DAILY_REQUEST_LIMIT

PROMPT = """На картинке — список недостающих деталей снаряжения/релик-материалов персонажа
(бот C3PO, команда inventory unit, Star Wars: Galaxy of Heroes). Обычно два раздела:
"Additional Gear needed for G<N>" (детали снаряжения) и "Additional Material needed for R<N>"
(релик-материалы) — числа N после G/R могут быть любыми, это целевой гир/уровень реликвии.
Один из разделов может отсутствовать, если по нему всё уже готово.

В каждой строке — иконка, название детали (на английском, как написано на картинке) и
количество после "x". Нужно распознать ВСЕ строки из ОБОИХ разделов.

Верни СТРОГО JSON без markdown-обрамления (без ```), ровно такой структуры:
{
  "gear_tier": "G13" или null (если раздела гира нет на картинке),
  "relic_tier": "R9" или null (если раздела релика нет на картинке),
  "items": [
    {"name": "точное название детали как на картинке", "quantity": 16}
  ]
}
"items" — общий список для обоих разделов вперемешку, без деления на секции в самом JSON."""


# Кнопка «Показать всем» на скрытом (ephemeral) ответе — по образцу
# cogs/datacron_requirements.py::DatacronCheckRevealView (там для embed'ов, здесь для
# простого текста, т.к. /фарм отвечает обычным сообщением, не embed'ом).
class GearFarmRevealView(disnake.ui.View):
    def __init__(self, content: str):
        super().__init__(timeout=1800)
        self.content = content
        self.revealed = False

    @disnake.ui.button(label="Показать всем", emoji="🔓", style=disnake.ButtonStyle.secondary)
    async def reveal(self, button: disnake.ui.Button, interaction: disnake.MessageInteraction):
        if self.revealed:
            await interaction.response.defer()
            return
        self.revealed = True
        button.disabled = True
        button.label = "Показано всем"
        await interaction.response.edit_message(view=self)
        await interaction.channel.send(self.content)


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


class GearFarm(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @commands.slash_command(
        name="фарм",
        description="Распознать картинку C3PO с недостающими деталями и подсказать, где их фармить",
    )
    @commands.check(lambda inter: guild_resolver.is_officer_for_resolved_guild(inter.author))
    async def gear_farm(
        self,
        inter: disnake.ApplicationCommandInteraction,
        ссылка: str = commands.Param(
            description="Прямая ссылка на сообщение C3PO (inventory unit) с картинкой недостающих деталей"
        ),
    ):
        if not self.bot.openrouter_api_key:
            await inter.response.send_message(
                "❌ OPENROUTER_API_KEY не настроен на сервере — обратитесь к администратору бота.",
                ephemeral=True,
            )
            return

        # Общий с /тб_ордер_из_картинки дневной лимит OpenRouter (см.
        # services/openrouter_vision.py) — проверяем ДО вызова API, чтобы не тратить
        # последний запрос из дневной квоты впустую, если лимит и так почти исчерпан.
        if daily_used_ratio(OPENROUTER_DAILY_REQUEST_LIMIT) >= self.bot.openrouter_daily_warning_ratio:
            await inter.response.send_message(
                "⏳ Команда временно недоступна — почти исчерпан дневной лимит на распознавание "
                "картинок (~90%). Лимит сбрасывается в начале следующих суток (UTC).",
                ephemeral=True,
            )
            return

        await inter.response.defer(ephemeral=True)

        channel_id = extract_channel_id(ссылка)
        message_id = extract_message_id(ссылка)
        if channel_id is None or message_id is None:
            await inter.edit_original_response(
                "❌ Не удалось распознать ссылку — нужна прямая ссылка на конкретное сообщение "
                "(правой кнопкой по сообщению C3PO → «Копировать ссылку»)."
            )
            return

        try:
            channel = self.bot.get_channel(channel_id) or await self.bot.fetch_channel(channel_id)
            message = await channel.fetch_message(message_id)
        except disnake.NotFound:
            await inter.edit_original_response("❌ Сообщение не найдено — проверьте ссылку.")
            return
        except disnake.Forbidden:
            await inter.edit_original_response("❌ Нет доступа к этому каналу/сообщению.")
            return

        image_attachment = next((a for a in message.attachments if is_image_attachment(a)), None)
        if image_attachment is None:
            await inter.edit_original_response("❌ В этом сообщении нет картинки.")
            return

        image_bytes = await image_attachment.read()
        mime_type = guess_mime_type(image_attachment)

        try:
            data = await asyncio.to_thread(
                call_vision_json, image_bytes, mime_type, self.bot.openrouter_api_key, PROMPT
            )
        except Exception as e:
            await inter.edit_original_response(f"❌ Ошибка распознавания картинки: {e}")
            return

        items = data.get("items") or []
        if not items:
            await inter.edit_original_response(
                "⚠️ Не удалось распознать ни одной детали на картинке — проверьте, что это "
                "скриншот от C3PO (`inventory unit`)."
            )
            return

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
                    recycled.append((name, ingredient_id, qty, points_needed))
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
            f"🔧 **{name}** — {location_by_id[base_id]}"
            for base_id, name in matched
            if base_id in location_by_id
        ]
        if direct_farm_lines:
            lines.append("**Где фармить:**")
            lines.extend(direct_farm_lines)
            lines.append("")

        if recycled:
            lines.append("**Через переработку у Мусорщика:**")
            for material_name, ingredient_id, qty, points_needed in recycled:
                ingredient_name = names_by_base_id.get(ingredient_id, ingredient_id)
                ingredient_location = location_by_id.get(ingredient_id, "нет данных о фарме")
                lines.append(
                    f"♻️ **{material_name}** — нужно {points_needed} очк.: "
                    f"{qty}x {ingredient_name} — {ingredient_location}"
                )
            lines.append("")

        if no_location:
            lines.append("**Нет данных о месте фарма (гильд-магазин/шаттлы/ивентовое):**")
            lines.append(", ".join(no_location))
            lines.append("")

        if unmatched:
            lines.append("**Не нашёл в справочнике (сверьте название вручную):**")
            lines.append(", ".join(f"{n} x{q}" for n, q in unmatched))

        text = "\n".join(lines).strip() or "Не удалось построить план фарма."
        text = text[:2000]
        await inter.edit_original_response(content=text, view=GearFarmRevealView(text))


def setup(bot: commands.Bot):
    bot.add_cog(GearFarm(bot))
