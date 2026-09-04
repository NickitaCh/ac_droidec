"""/тб_ордер_из_картинки — по ссылке на тред достаёт первое сообщение с картинкой
"Strategy Summary" (планировщик ТБ, вкладка со сводной таблицей Round 1..6 x
Dark/Mixed/Light/Bonus), распознаёт её через Mistral vision (MISTRAL_API_KEY,
бесплатный тариф, модель mistral-medium-latest — см. main.py) и публикует в тот
же тред 6 сообщений, по одному на этап, в формате, которым офицеры уже пишут
ордер вручную (см. тред "43*", TB_ORDER_SOURCE_CHANNEL_ID).

После публикации, если в треде нашлись все 6 заголовков этапов (см.
_find_existing_order_phases — это и есть "проверка, что ордер появился"), команда
сохраняет план (обязательный параметр "название" + ветка + сумма звёзд) в
database.tb_saved_plans. /тб_план — отдельная группа команд поверх этой таблицы
(список / выбрать / удалить): "выбрать" пишет id плана в guilds.tb_active_plan_id,
и tb_order_loop (cogs/guild_events.py) с этого момента берёт ежедневный блок
этапа из ветки выбранного плана, а не из статического guilds.tb_order_source_
channel_id. Так на одну ТБ можно держать несколько разобранных планов (например,
если план поменяли) и переключаться между ними без обращения к /настройки.

"/тб_план сохранить" — то же самое сохранение в tb_saved_plans, но без картинки/
Mistral: для ветки, где ордер на все 6 этапов уже есть (написан вручную офицером
или собран этой же командой раньше) — просто регистрирует её под названием и
звёздами. Проверка на "появился ли ордер" здесь ищет заголовки этапов по тому же
паттерну, что и tb_order_loop (guild_events.TB_PLAN_HEADER_RE), а не только среди
сообщений бота — офицерский текст тоже считается.

Изначально был реализован на Gemini (google-genai, GOOGLE_API_KEY), но бесплатный
тариф Google блокирует запросы с датацентр-IP VPS (проверено 2026-08-21 — тот же
ключ работает локально и падает изнутри контейнера на VPS с "location not
supported"). Mistral с того же VPS работает (подтверждено live-пробником). Точность
чуть ниже Gemini (у Mistral изредка промах на 1 звезду в одной ячейке из ~30 — см.
sanity-check по сумме звёзд ниже), но приемлема.

Правило перевода звёзд с картинки в текст ордера (подтверждено пользователем
2026-08-21, на примере треда "44тест"):
- Цель = количество ★ в правой нижней части ячейки (0-3).
- Взводы = "все", если Цель > 0. Если Цель = 0: "по боту" для Dark/Mixed/Light,
  но "только по боту" для бонусной зоны — это разные формулировки, не опечатка
  (сверено с реальным ордером бота в #ac-тб-оповещения, сообщение про 4 этап,
  2026-08-21: Кессель 0★ → "по боту", Мандалор 0★ → "только по боту"). Для
  этапов 1-5 это верно, даже если планета получит звёзды позже — взвод всё
  равно нужен для очков территории.
- БЗ = "обязательно" всегда, кроме случая ниже.
- Особый случай — 6 (последний) этап: если Цель = 0, блок планеты не выводится
  вообще (дальше не будет этапа, куда переносить взвод/БЗ). На этапах 1-5 блок
  выводится всегда, даже при Цель = 0.
- Бонусная зона (⚪) в общий зачёт "Total Stars" даёт всего 1 звезду при Цель = 3
  и 0 звёзд при Цель < 3 (за 1-2★ на бонусной планете — другие награды, не входит
  в общий зачёт) — это объясняет, почему сумма видимых ★ на картинке обычно НЕ
  равна числу в шапке "Total Stars: N". Используется только для сверки суммы,
  параметр "звёзды" передаёт офицер, значение не меняет текст самого ордера.

Markdown в точности как у офицеров (сверено 2026-08-22 с сообщением о 4 этапе
в #ac-тб-оповещения и с файлом order.txt, который прислал пользователь):
"## Восход Империи — N этап" (заголовок H2, без "> "), затем каждая планета —
блок из 4 строк с префиксом "> " (цитата), название планеты жирным с эмодзи
зоны перед ним, между планетами — две пустые строки "> ". Эмодзи зон:
Dark=🔴, Mixed=🟡 (жёлтый, не оранжевый), Light=🔵, Bonus=⚪ (белый, не зелёный).
"""

import asyncio
import json
import re

import disnake
from disnake.ext import commands

import database
import guild_resolver
import tb_platoon_autofill
from cogs.guild_events import TB_PLAN_HEADER_RE
from services.message_image import extract_channel_id, guess_mime_type, is_image_attachment

ORDER_HEADER_RE = re.compile(r"^##\s.+—\s*(\d+)\s*этап", re.MULTILINE)

ZONE_EMOJI = {"dark": "🔴", "mixed": "🟡", "light": "🔵", "bonus": "⚪"}

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


async def _find_existing_order_phases(thread: disnake.Thread, bot_user_id: int) -> list:
    """Сканирует историю треда на уже опубликованные блоки ордера (заголовки
    "## ... — N этап" от самого бота) — используется для защиты от повторной
    публикации дублей при повторном запуске команды в том же треде."""
    phases = set()
    async for msg in thread.history(limit=200):
        if msg.author.id != bot_user_id:
            continue
        m = ORDER_HEADER_RE.match(msg.content or "")
        if m:
            phases.add(int(m.group(1)))
    return sorted(phases)


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
    import base64

    import requests

    b64 = base64.b64encode(image_bytes).decode()
    response = requests.post(
        "https://api.mistral.ai/v1/chat/completions",
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json={
            "model": "mistral-medium-latest",
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": PROMPT},
                        {"type": "image_url", "image_url": f"data:{mime_type};base64,{b64}"},
                    ],
                }
            ],
            "response_format": {"type": "json_object"},
            "temperature": 0,
        },
        timeout=60,
    )
    response.raise_for_status()
    text = response.json()["choices"][0]["message"]["content"].strip()
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

        active = []
        for zone, planet, stars in entries:
            total += _overall_contribution(zone, stars)
            if phase == 6 and stars <= 0:
                continue
            active.append((zone, planet, stars))

        lines = [f"## Восход Империи — {phase} этап"]
        if not active:
            lines.append("> (нет активных планет на этом этапе)")
        for i, (zone, planet, stars) in enumerate(active):
            if stars > 0:
                vzvod = "все"
            elif zone == "bonus":
                vzvod = "только по боту"
            else:
                vzvod = "по боту"
            lines.append(f"> {ZONE_EMOJI[zone]} **{_translate_planet(planet)}**")
            lines.append(f"> **Цель:** {_stars_word(stars)}")
            lines.append(f"> **Взводы:** {vzvod}")
            lines.append("> **БЗ:** обязательно")
            if i != len(active) - 1:
                lines.append("> ")
                lines.append("> ")
        blocks.append("\n".join(lines))
    return blocks, total


async def _find_posted_phases_any_author(thread: disnake.Thread) -> list:
    """Как _find_existing_order_phases, но не привязано к автору-боту — использует тот
    же паттерн заголовка, что и tb_order_loop (guild_events.TB_PLAN_HEADER_RE), чтобы
    можно было вручную сохранить план, где текст ордера писали офицеры, а не бот."""
    phases = set()
    async for msg in thread.history(limit=None):
        match = TB_PLAN_HEADER_RE.search(msg.content or "")
        if match:
            phases.add(int(match.group(1)))
    return sorted(phases)


async def autocomplete_tb_plans(inter: disnake.ApplicationCommandInteraction, user_input: str):
    guild_id = guild_resolver.resolve_guild_id(inter.author)
    if guild_id is None:
        return []
    user_input = (user_input or "").lower()
    return [p["name"] for p in database.get_tb_saved_plans(guild_id) if user_input in p["name"].lower()][:25]


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
        название: str = commands.Param(description="Название плана — под ним он сохранится для /тб_план выбрать"),
        звёзды: int = commands.Param(description="Total Stars с картинки — для проверки, что распознано верно"),
        принудительно: bool = commands.Param(
            default=False,
            description="Опубликовать заново, даже если ордер для этих этапов уже есть в треде (будут дубли)",
        ),
    ):
        if not self.bot.mistral_api_key:
            await inter.response.send_message(
                "❌ MISTRAL_API_KEY не настроен на сервере — обратитесь к администратору бота.",
                ephemeral=True,
            )
            return

        await inter.response.defer(ephemeral=True)

        channel_id = extract_channel_id(ссылка)
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
            (a for a in first_message.attachments if is_image_attachment(a)), None
        )
        if image_attachment is None:
            await inter.edit_original_response("❌ В первом сообщении треда нет картинки.")
            return

        if not принудительно:
            existing_phases = await _find_existing_order_phases(thread, self.bot.user.id)
            if existing_phases:
                phases_str = ", ".join(str(p) for p in existing_phases)
                await inter.edit_original_response(
                    f"⚠️ В треде уже есть ордер для этапов: {phases_str}. "
                    "Повторный запуск создаст дубли. Если это осознанно (например, план поменялся) — "
                    "повторите команду с параметром `принудительно: True`."
                )
                return

        image_bytes = await image_attachment.read()
        mime_type = guess_mime_type(image_attachment)

        try:
            data = await asyncio.to_thread(
                _parse_strategy_summary_sync, image_bytes, mime_type, self.bot.mistral_api_key
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

        try:
            for block in blocks:
                await thread.send(block)
        except (disnake.Forbidden, disnake.HTTPException) as e:
            await inter.edit_original_response(
                f"{warning}❌ Публикация прервалась на середине (не всё могло уйти в тред): {e}\n"
                "План не сохранён — проверьте тред вручную и, если нужно, повторите с `принудительно: True`."
            )
            return

        # Проверяем, что все 6 этапов реально появились в треде (не просто что
        # thread.send не бросил исключение), прежде чем сохранять план в память.
        posted_phases = await _find_existing_order_phases(thread, self.bot.user.id)
        if posted_phases != [1, 2, 3, 4, 5, 6]:
            missing = sorted(set(range(1, 7)) - set(posted_phases))
            await inter.edit_original_response(
                f"{warning}⚠️ Сообщения отправлены, но в треде не нашлись все 6 этапов "
                f"(не хватает: {', '.join(map(str, missing))}). План не сохранён — проверьте тред вручную."
            )
            return

        guild_id = guild_resolver.resolve_guild_id(inter.author)
        plan_id = database.save_tb_plan(guild_id, название, thread.id, total, created_by=str(inter.author.id))

        # Автозаполнение взводов сразу после сохранения плана — по прямому запросу
        # пользователя (см. план "Автозаполнение взводов ТБ + фильтры" от 2026-08-29,
        # пересказ разговора с Ильёй: "прописал команду и бот... подготовил взводы на 6
        # этапов и сообщил, всего ли хватает"). Ошибка автозаполнения НЕ откатывает уже
        # сохранённый план — он в любом случае корректно сохранён, просто взводы придётся
        # расставить вручную на /tb/platoons.
        autofill_note = ""
        try:
            result = await tb_platoon_autofill.autofill_plan(guild_id, plan_id)
            reasons = {}
            for outcome in result.unfilled:
                if outcome.reason == "held_back":
                    continue
                reasons[outcome.reason] = reasons.get(outcome.reason, 0) + 1
            labels = {
                "no_owner": "нет владельцев", "no_eligible_owner": "нет подходящих доноров",
                "unit_excluded": "юнит исключён фильтром", "unit_not_resolved": "юнит не распознан",
            }
            autofill_note = f"\n🧩 Взводы подготовлены: {result.filled_slots}/{result.total_slots} слотов занято"
            if result.held_back:
                autofill_note += f", {result.held_back} намеренно не добито («держим»)"
            if reasons:
                parts = [f"{labels.get(r, r)}: {c}" for r, c in reasons.items()]
                autofill_note += " — не хватает (" + ", ".join(parts) + ")"
            autofill_note += "\nПроверить/скорректировать: /tb/platoons на сайте."
        except (ValueError, RuntimeError) as e:
            autofill_note = f"\n⚠️ Не удалось автоматически расставить взводы: {e}. План сохранён, расставьте вручную на /tb/platoons."

        await inter.edit_original_response(
            f"{warning}✅ Опубликовано 6 сообщений в тред «{thread.name}».\n"
            f"💾 План сохранён как «{название}» ({total} ★) — выбрать его для ежедневной публикации: "
            f"`/тб_план выбрать`."
            f"{autofill_note}"
        )


    # ------------------ Управление сохранёнными планами ------------------
    @commands.slash_command(name="тб_план", description="Управление сохранёнными планами ордера ТБ")
    @commands.check(lambda inter: guild_resolver.is_officer_for_resolved_guild(inter.author))
    async def tb_plan_group(self, inter: disnake.ApplicationCommandInteraction):
        pass

    @tb_plan_group.sub_command(name="список", description="Список сохранённых планов ордера ТБ")
    async def tb_plan_list(self, inter: disnake.ApplicationCommandInteraction):
        guild_id = guild_resolver.resolve_guild_id(inter.author)
        plans = database.get_tb_saved_plans(guild_id)
        if not plans:
            await inter.response.send_message(
                "Сохранённых планов пока нет — соберите ордер через `/тб_ордер_из_картинки`.", ephemeral=True
            )
            return
        guild_cfg = database.get_guild_config(guild_id) or {}
        active_id = int(guild_cfg["tb_active_plan_id"]) if guild_cfg.get("tb_active_plan_id") else None
        lines = []
        for p in plans:
            mark = " — 🟢 активен" if p["id"] == active_id else ""
            lines.append(f"**{p['name']}** — {p['total_stars']} ★, <#{p['thread_id']}>{mark}")
        await inter.response.send_message("\n".join(lines), ephemeral=True)

    @tb_plan_group.sub_command(
        name="выбрать", description="Выбрать план, из которого будет постится ежедневный ордер ТБ"
    )
    async def tb_plan_select(
        self,
        inter: disnake.ApplicationCommandInteraction,
        название: str = commands.Param(description="Название сохранённого плана", autocomplete=autocomplete_tb_plans),
    ):
        guild_id = guild_resolver.resolve_guild_id(inter.author)
        plan = database.get_tb_saved_plan_by_name(guild_id, название)
        if plan is None:
            await inter.response.send_message(f"❌ План «{название}» не найден.", ephemeral=True)
            return
        database.update_guild_config(guild_id, tb_active_plan_id=plan["id"])
        await inter.response.send_message(
            f"✅ Активный план ордера ТБ: «{plan['name']}» ({plan['total_stars']} ★, <#{plan['thread_id']}>). "
            "Ежедневная публикация будет брать текст этапов из этой ветки.",
            ephemeral=True,
        )

    @tb_plan_group.sub_command(
        name="сохранить",
        description="Сохранить как план уже готовую ветку с ордером на 6 этапов (например, дописанную вручную)",
    )
    async def tb_plan_save_manual(
        self,
        inter: disnake.ApplicationCommandInteraction,
        ссылка: str = commands.Param(description="Ссылка на тред, где уже есть ордер на все 6 этапов"),
        название: str = commands.Param(description="Название плана — под ним он сохранится для /тб_план выбрать"),
        звёзды: int = commands.Param(description="Total Stars по этому плану — для списка планов"),
    ):
        await inter.response.defer(ephemeral=True)

        channel_id = extract_channel_id(ссылка)
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

        posted_phases = await _find_posted_phases_any_author(thread)
        if posted_phases != [1, 2, 3, 4, 5, 6]:
            missing = sorted(set(range(1, 7)) - set(posted_phases))
            found = ", ".join(map(str, posted_phases)) or "ни одного"
            await inter.edit_original_response(
                f"⚠️ В треде нашлись заголовки этапов: {found}. Не хватает: {', '.join(map(str, missing))}. "
                "План не сохранён — в треде должен быть текст ордера на все 6 этапов "
                "(заголовки вида «Восход Империи — N этап»)."
            )
            return

        guild_id = guild_resolver.resolve_guild_id(inter.author)
        database.save_tb_plan(guild_id, название, thread.id, звёзды, created_by=str(inter.author.id))
        await inter.edit_original_response(
            f"💾 План сохранён как «{название}» ({звёзды} ★, <#{thread.id}>) — выбрать его для ежедневной "
            f"публикации: `/тб_план выбрать`."
        )

    @tb_plan_group.sub_command(name="удалить", description="Удалить сохранённый план ордера ТБ")
    async def tb_plan_delete(
        self,
        inter: disnake.ApplicationCommandInteraction,
        название: str = commands.Param(description="Название сохранённого плана", autocomplete=autocomplete_tb_plans),
    ):
        guild_id = guild_resolver.resolve_guild_id(inter.author)
        deleted = database.delete_tb_saved_plan(guild_id, название)
        if not deleted:
            await inter.response.send_message(f"❌ План «{название}» не найден.", ephemeral=True)
            return
        await inter.response.send_message(f"🗑️ План «{название}» удалён.", ephemeral=True)


def setup(bot: commands.Bot):
    bot.add_cog(TBOrderImage(bot))
