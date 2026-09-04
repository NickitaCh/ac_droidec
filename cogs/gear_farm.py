"""/фарм — по прямой ссылке на сообщение C3PO (`inventory unit`) распознаёт картинку со
списком недостающих деталей снаряжения/релик-материалов через Mistral vision (тот же
MISTRAL_API_KEY/mistral-medium-latest, что и cogs/tb_order_image.py) и подсказывает, на каких
битвах их фармить.

Почему через ИИ, хотя сам список "чего не хватает" мы честно посчитать не можем: C3PO вычитает
из общего количества то, что уже лежит у игрока на складе (доступ к аккаунту через что-то вроде
HotUtils) — Comlink складом не располагает и никогда не будет, это подтверждённый потолок API
(см. обсуждение при планировании этой фичи). Картинка C3PO — единственный источник этих
"итоговых" чисел, поэтому распознавание неизбежно. А вот "где фармить" — чистые игровые данные
(Comlink `equipment[].lookupMission`, справочник — services/equipment_sync.py), тут ИИ не нужен:
сопоставление названий и подбор локаций (жадный set-cover) — обычный код.
"""

import asyncio

import disnake
from disnake.ext import commands

import database
import guild_resolver
from services.message_image import extract_channel_id, extract_message_id, guess_mime_type, is_image_attachment
from services.mistral_vision import budget_used_ratio, call_vision_json

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
        if not self.bot.mistral_api_key:
            await inter.response.send_message(
                "❌ MISTRAL_API_KEY не настроен на сервере — обратитесь к администратору бота.",
                ephemeral=True,
            )
            return

        # Общий с /тб_ордер_из_картинки месячный бюджет Mistral (см. services/mistral_vision.py) —
        # проверяем ДО вызова API, чтобы не расходовать последние токены на запрос, который
        # всё равно не пригодится, если лимит и так почти исчерпан.
        if budget_used_ratio(self.bot.mistral_monthly_budget_usd) >= self.bot.mistral_budget_warning_ratio:
            await inter.response.send_message(
                "⏳ Команда временно недоступна — почти исчерпан месячный лимит на распознавание "
                "картинок (Mistral, ~90%). Лимит сбрасывается в начале следующего месяца.",
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
                call_vision_json, image_bytes, mime_type, self.bot.mistral_api_key, PROMPT
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
        base_id_locations = {}
        no_location = []
        for base_id, name in matched:
            locs = database.get_equipment_locations(base_id)
            if locs:
                base_id_locations[base_id] = locs
            else:
                no_location.append(name)

        plan = _greedy_location_plan(base_id_locations)

        lines = []
        header_bits = [b for b in (data.get("gear_tier"), data.get("relic_tier")) if b]
        if header_bits:
            lines.append(f"**Цель:** {' / '.join(header_bits)}")
            lines.append("")

        if plan:
            lines.append("**Где фармить:**")
            for label, covered_ids in plan:
                covered_names = ", ".join(names_by_base_id[bid] for bid in covered_ids)
                lines.append(f"📍 **{label}** — закрывает: {covered_names}")
            lines.append("")

        if no_location:
            lines.append("**Нет данных о месте фарма (гильд-магазин/шаттлы/ивентовое):**")
            lines.append(", ".join(no_location))
            lines.append("")

        if unmatched:
            lines.append("**Не нашёл в справочнике (сверьте название вручную):**")
            lines.append(", ".join(f"{n} x{q}" for n, q in unmatched))

        text = "\n".join(lines).strip() or "Не удалось построить план фарма."
        await inter.edit_original_response(text[:2000])


def setup(bot: commands.Bot):
    bot.add_cog(GearFarm(bot))
