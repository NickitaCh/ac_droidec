"""/тб_ордер_из_картинки — по ссылке на тред достаёт первое сообщение с картинкой
"Strategy Summary" (планировщик ТБ, вкладка со сводной таблицей Round 1..6 x
Dark/Mixed/Light/Bonus), распознаёт её через Gemini vision (GOOGLE_API_KEY,
бесплатный тариф — см. main.py) и публикует в тот же тред 6 сообщений, по одному
на этап, в формате, которым офицеры уже пишут ордер вручную (см. тред "43*",
TB_ORDER_SOURCE_CHANNEL_ID).

Правило перевода звёзд с картинки в текст ордера (подтверждено пользователем
2026-08-21, на примере треда "44тест"):
- Цель = количество ★ в правой нижней части ячейки (0-3).
- Взводы = "все", если Цель > 0, иначе "по боту" (для этапов 1-5 — даже если эта
  планета получит звёзды позже, взвод всё равно нужен для очков территории).
- БЗ = "обязательно" всегда, кроме случая ниже.
- Особый случай — 6 (последний) этап: если Цель = 0, блок планеты не выводится
  вообще (дальше не будет этапа, куда переносить взвод/БЗ). На этапах 1-5 блок
  выводится всегда, даже при Цель = 0.
- Бонусная зона (🟢) в общий зачёт "Total Stars" даёт всего 1 звезду при Цель = 3
  и 0 звёзд при Цель < 3 (за 1-2★ на бонусной планете — другие награды, не входит
  в общий зачёт) — это объясняет, почему сумма видимых ★ на картинке обычно НЕ
  равна числу в шапке "Total Stars: N". Используется только для сверки суммы,
  параметр "звёзды" передаёт офицер, значение не меняет текст самого ордера.
"""

import asyncio
import json
import mimetypes
import re

import disnake
from disnake.ext import commands

import guild_resolver

LINK_RE = re.compile(r"discord(?:app)?\.com/channels/(\d+)/(\d+)(?:/(\d+))?")

ZONE_EMOJI = {"dark": "🔴", "mixed": "🟠", "light": "🔵", "bonus": "🟢"}

# Известные планеты "Восхода Империи" — русские названия сверены с TB_PLANET_CONFLICT
# в cogs/guild_events.py (для планет, которые там уже есть) плюс несколько новых.
PLANET_RU = {
    "mustafar": "Мустафар",
    "geonosis": "Джеонозис",
    "dathomir": "Датомир",
    "haven-class medical station": "Медстанция",
    "medical station": "Медстанция",
    "corellia": "Кореллия",
    "felucia": "Фелуция",
    "tatooine": "Татуин",
    "kessel": "Кессель",
    "vandor": "Вандор",
    "coruscant": "Корусант",
    "bracca": "Бракка",
    "kashyyyk": "Кашик",
    "lothal": "Лотал",
    "ring of kafrene": "Кольцо Кафрены",
    "scarif": "Скариф",
    "zeffo": "Зеффо",
    "mandalore": "Мандалор",
    "malachor": "Малахор",
}

PROMPT = """На картинке — скриншот вкладки "Strategy Summary" планировщика ТБ (Star Wars:
Galaxy of Heroes, "Восход Империи"). Таблица: строки Dark/Mixed/Light/Bonus,
колонки Round 1..Round 6.

В каждой заполненной ячейке — название планеты сверху (например "R5: Mustafar" —
верни только "Mustafar", без префикса "R#:"), и под ним три строки чисел. Нужно
только:
1. Название планеты (английское, как написано, без префикса "R#:").
2. Итоговый рейтинг ★ в правой нижней части ячейки (0-3 звезды). Если там пусто,
   стоит только число без иконок звёзд, или ячейка пустая — это 0 звёзд.
Всё остальное (проценты, "M"-значения, "Sandbagged"/"Mission Req" внизу) не нужно
и должно игнорироваться.

Для строки Bonus: если в колонке стоит "???" — зона ещё не открыта, верни для неё
пустой список. Если в колонке одна или несколько планет (может быть до 2, одна
под другой) — верни все, сверху вниз.

Верни СТРОГО JSON без markdown-обрамления (без ```), ровно такой структуры:
{
  "total_stars_header": <число из заголовка "Total Stars: N">,
  "rounds": [
    {
      "round": 1,
      "dark": {"planet": "...", "stars": 0} или null,
      "mixed": {"planet": "...", "stars": 0} или null,
      "light": {"planet": "...", "stars": 0} или null,
      "bonus": [{"planet": "...", "stars": 0}]
    }
  ]
}
Массив "rounds" должен содержать ровно 6 элементов (round: 1..6)."""


def _extract_channel_id(link: str):
    link = link.strip()
    m = LINK_RE.search(link)
    if m:
        return int(m.group(2))
    if link.isdigit():
        return int(link)
    return None


def _is_image_attachment(a: disnake.Attachment) -> bool:
    if a.content_type and a.content_type.startswith("image/"):
        return True
    return a.filename.lower().endswith((".png", ".jpg", ".jpeg", ".webp", ".gif"))


def _guess_mime_type(a: disnake.Attachment) -> str:
    if a.content_type:
        return a.content_type.split(";")[0].strip()
    guessed, _ = mimetypes.guess_type(a.filename)
    return guessed or "image/png"


def _translate_planet(name: str) -> str:
    key = (name or "").strip().lower()
    key = re.sub(r"^r\d+:\s*", "", key)
    return PLANET_RU.get(key, (name or "?").strip())


def _stars_word(n: int) -> str:
    if n <= 0:
        return "0 звёзд"
    if n == 1:
        return "1 звезда"
    return f"{n} звезды"


def _overall_contribution(zone: str, stars: int) -> int:
    # Подтверждено пользователем: бонусная планета даёт 1 звезду в общий зачёт
    # только при полных 3★ на ней, иначе 0 (1-2★ там — другие награды).
    if zone == "bonus":
        return 1 if stars >= 3 else 0
    return max(0, stars)


def _parse_strategy_summary_sync(image_bytes: bytes, mime_type: str, api_key: str) -> dict:
    # google-generativeai официально прекратил поддержку (deprecation warning
    # при импорте) — используем его замену, google-genai (проверено живым
    # запросом 2026-08-21: тот же бесплатный ключ, та же модель работают).
    from google import genai
    from google.genai import types

    client = genai.Client(api_key=api_key)
    response = client.models.generate_content(
        model="gemini-flash-latest",
        contents=[PROMPT, types.Part.from_bytes(data=image_bytes, mime_type=mime_type)],
        config=types.GenerateContentConfig(response_mime_type="application/json"),
    )
    text = (response.text or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip())
    return json.loads(text)


def _build_order_blocks(data: dict):
    """Возвращает (список из 6 текстов этапов, сумма звёзд в общий зачёт)."""
    rounds = data.get("rounds") or []
    blocks = []
    total = 0
    for round_data in rounds:
        phase = round_data.get("round")
        entries = []
        for zone in ("dark", "mixed", "light"):
            cell = round_data.get(zone)
            if cell and cell.get("planet"):
                entries.append((zone, cell.get("planet"), int(cell.get("stars") or 0)))
        for cell in round_data.get("bonus") or []:
            if cell and cell.get("planet"):
                entries.append(("bonus", cell.get("planet"), int(cell.get("stars") or 0)))

        lines = [f"Восход Империи — {phase} этап", ""]
        any_entry = False
        for zone, planet, stars in entries:
            total += _overall_contribution(zone, stars)
            if phase == 6 and stars <= 0:
                continue
            any_entry = True
            lines.append(f"{ZONE_EMOJI[zone]} {_translate_planet(planet)}")
            lines.append(f"Цель: {_stars_word(stars)}")
            lines.append(f"Взводы: {'все' if stars > 0 else 'по боту'}")
            lines.append("БЗ: обязательно")
            lines.append("")
        if not any_entry:
            lines.append("(нет активных планет на этом этапе)")
        blocks.append("\n".join(lines).rstrip())
    return blocks, total


class TBOrderImage(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @commands.slash_command(
        name="тб_ордер_из_картинки",
        description="Собрать ордер на 6 этапов ТБ из картинки Strategy Summary в треде (по ссылке)",
    )
    @commands.check(lambda inter: guild_resolver.is_officer_for_resolved_guild(inter.author))
    async def tb_order_from_image(
        self,
        inter: disnake.ApplicationCommandInteraction,
        ссылка: str = commands.Param(description="Ссылка на тред с картинкой Strategy Summary в первом сообщении"),
        звёзды: int = commands.Param(description="Total Stars с картинки — для проверки, что распознано верно"),
    ):
        if not self.bot.google_api_key:
            await inter.response.send_message(
                "❌ GOOGLE_API_KEY не настроен на сервере — обратитесь к администратору бота.",
                ephemeral=True,
            )
            return

        await inter.response.defer(ephemeral=True)

        channel_id = _extract_channel_id(ссылка)
        if channel_id is None:
            await inter.edit_original_response("❌ Не удалось распознать ссылку на тред.")
            return

        try:
            thread = self.bot.get_channel(channel_id) or await self.bot.fetch_channel(channel_id)
        except disnake.NotFound:
            await inter.edit_original_response("❌ Тред не найден — проверьте ссылку.")
            return
        except disnake.Forbidden:
            await inter.edit_original_response("❌ Нет доступа к этому треду.")
            return

        first_message = None
        async for msg in thread.history(limit=1, oldest_first=True):
            first_message = msg
        if first_message is None:
            await inter.edit_original_response("❌ В треде нет сообщений.")
            return

        image_attachment = next(
            (a for a in first_message.attachments if _is_image_attachment(a)), None
        )
        if image_attachment is None:
            await inter.edit_original_response("❌ В первом сообщении треда нет картинки.")
            return

        image_bytes = await image_attachment.read()
        mime_type = _guess_mime_type(image_attachment)

        try:
            data = await asyncio.to_thread(
                _parse_strategy_summary_sync, image_bytes, mime_type, self.bot.google_api_key
            )
        except Exception as e:
            await inter.edit_original_response(f"❌ Ошибка распознавания картинки: {e}")
            return

        try:
            blocks, total = _build_order_blocks(data)
        except Exception as e:
            await inter.edit_original_response(f"❌ Не удалось разобрать ответ модели: {e}")
            return

        if len(blocks) != 6:
            await inter.edit_original_response(
                f"⚠️ Распознано {len(blocks)} этапов вместо 6 — проверьте картинку вручную, не публикую."
            )
            return

        warning = ""
        if total != звёзды:
            warning = (
                f"⚠️ Внимание: по распознанным данным сумма звёзд в общий зачёт = {total}, "
                f"а вы указали {звёзды}. Проверьте текст в треде перед тем, как тегать гильдию.\n\n"
            )

        for block in blocks:
            await thread.send(block)

        await inter.edit_original_response(f"{warning}✅ Опубликовано 6 сообщений в тред «{thread.name}».")


def setup(bot: commands.Bot):
    bot.add_cog(TBOrderImage(bot))
