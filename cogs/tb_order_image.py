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
звёздами. Проверка на "появился ли ордер" здесь ищет номер этапа по тому же общему
паттерну, что и tb_order_loop (guild_events.TB_ORDER_STAGE_RE — "1 этап"/
"этап 1"/вариации, не завязан на наш конкретный текст заголовка), а не только
среди сообщений бота — офицерский текст в любом формате тоже считается.

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
import os

import disnake
from disnake.ext import commands

import database
import guild_resolver
import tb_platoon_autofill
import tb_platoon_notify
from services import feature_flags
from cogs.guild_events import (
    TB_ORDER_STAGE_RE,
    _tb_order_stage_number,
    chunk_tb_order_message,
)
from services.config_status import config_warning_text
from services.message_image import extract_channel_id, guess_mime_type, is_image_attachment
from services.openrouter_vision import call_vision_json, daily_used_ratio, OPENROUTER_DAILY_REQUEST_LIMIT
# Промпт и сборка 6 этапов — общие с сайтом (/tb/order-plans, «Ордер из картинки»).
from services.tb_order_image import (
    ORDER_HEADER_RE, PROMPT, summarize_autofill as _summarize_autofill,
    build_order_blocks as _build_order_blocks, validate_response as _validate_response,
)

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


async def _find_posted_phases_any_author(thread: disnake.Thread) -> list:
    """Как _find_existing_order_phases, но не привязано к автору-боту и не к конкретному
    тексту заголовка — использует тот же общий паттерн номера этапа, что и tb_order_loop
    (guild_events.TB_ORDER_STAGE_RE: "1 этап"/"этап 1"/вариации), чтобы можно было
    вручную сохранить план, где текст ордера писали офицеры в своём формате (не все
    гильдии на этом боте используют наш "Восход Империи — N этап"), а не только бот."""
    phases = set()
    async for msg in thread.history(limit=None):
        match = TB_ORDER_STAGE_RE.search(msg.content or "")
        if match:
            phases.add(int(_tb_order_stage_number(match)))
    return sorted(phases)


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
        название: str = commands.Param(description="Название плана — под ним он сохранится для /тб_план"),
        звёзды: int = commands.Param(description="Total Stars с картинки — для проверки, что распознано верно"),
        принудительно: bool = commands.Param(
            default=False,
            description="Опубликовать заново, даже если ордер для этих этапов уже есть в треде (будут дубли)",
        ),
    ):
        if not self.bot.openrouter_api_key:
            await inter.response.send_message(
                "❌ OPENROUTER_API_KEY не настроен на сервере — обратитесь к администратору бота.",
                ephemeral=True,
            )
            return

        # Общий с /фарм дневной лимит OpenRouter (см. services/openrouter_vision.py) —
        # проверяем ДО вызова API, чтобы не тратить последний запрос из дневной квоты
        # впустую, если лимит и так почти исчерпан.
        if daily_used_ratio(OPENROUTER_DAILY_REQUEST_LIMIT) >= self.bot.openrouter_daily_warning_ratio:
            await inter.response.send_message(
                "⏳ Команда временно недоступна — почти исчерпан дневной лимит на распознавание "
                "картинок (~90%). Лимит сбрасывается в начале следующих суток (UTC).",
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
                call_vision_json, image_bytes, mime_type, self.bot.openrouter_api_key, PROMPT, _validate_response
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
        if guild_id is not None and not feature_flags.is_enabled(guild_id, "tb_plan_order"):
            guild_id = None
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
            autofill_note = "\n" + _summarize_autofill(result)
            autofill_note += "\nПроверить/скорректировать: /tb/platoons на сайте."
        except (ValueError, RuntimeError) as e:
            autofill_note = f"\n⚠️ Не удалось автоматически расставить взводы: {e}. План сохранён, расставьте вручную на /tb/platoons."

        await inter.edit_original_response(
            f"{warning}✅ Опубликовано 6 сообщений в тред «{thread.name}».\n"
            f"💾 План сохранён как «{название}» ({total} ★) — выбрать его для ежедневной публикации: "
            f"`/тб_план`."
            f"{autofill_note}"
        )


    # ------------------ /тб_план — панель (2026-10-14) ------------------
    # Одна команда без параметров вместо 6 подкоманд: в сообщении — список планов,
    # выпадашки «план» и «этап», кнопки действий. Ответы на действия — отдельными
    # скрытыми сообщениями, сама панель обновляется на месте.
    @commands.slash_command(name="тб_план", description="Планы ордера ТБ: выбрать активный, предпросмотр, взводы, сохранить ветку")
    @commands.check(lambda inter: guild_resolver.is_officer_for_resolved_guild(inter.author))
    async def tb_plan_panel(self, inter: disnake.ApplicationCommandInteraction):
        await inter.response.defer(ephemeral=True)
        guild_id = guild_resolver.resolve_guild_id(inter.author)
        if guild_id is None or not feature_flags.is_enabled(guild_id, "tb_plan_order"):
            await inter.edit_original_response("❌ Не удалось определить гильдию, либо функция плана/ордера ТБ отключена для неё.")
            return
        view = TBPlanPanel(self, guild_id, inter)
        await inter.edit_original_response(content=view.render(), view=view.build())

    async def plan_preview_chunks(self, plan: dict) -> list[str]:
        """Что бот опубликует на каждый этап — тем же выбором, что автопост: тексты с сайта
        (services/tb_order_store.py) главнее треда, ордер с сайта может быть без треда."""
        from services import tb_order_store

        blocks, error = await tb_order_store.resolve_blocks(plan)
        parts, missing = [], []
        for phase in range(1, 7):
            block = blocks.get(phase)
            if block:
                mark = "" if block["source"] == "thread" else f" ({tb_order_store.SOURCE_LABELS[block['source']]})"
                parts.append(f"**— Этап {phase}{mark} —**\n{block['text']}")
            else:
                missing.append(phase)
                parts.append(f"**— Этап {phase} —**\n⚠️ нет текста")
        status = f"⚠️ Не хватает этапов: {', '.join(map(str, missing))}" if missing else "✅ Все 6 этапов найдены."
        if error:
            status += f"\n⚠️ {error}"
        where = f" (<#{plan['thread_id']}>)" if plan.get("thread_id") else " (ордер с сайта)"
        header = (
            f"🔍 Предпросмотр плана «{plan['name']}»{where} — так уйдёт в канал ежедневным "
            f"автоордером, публикации в канал не было.\n{status}\n\n"
        )
        return chunk_tb_order_message(header + "\n\n".join(parts))

    async def save_thread_plan(self, guild_id: int, link: str, name: str, stars: int, author_id: int) -> str:
        """Регистрирует готовую ветку как план — с проверкой, что в ней есть все 6 этапов."""
        channel_id = extract_channel_id(link)
        if channel_id is None:
            return "❌ Не удалось распознать ссылку на тред."
        try:
            thread = self.bot.get_channel(channel_id) or await self.bot.fetch_channel(channel_id)
        except disnake.NotFound:
            return "❌ Тред не найден — проверьте ссылку."
        except disnake.Forbidden:
            return "❌ Нет доступа к этому треду."
        posted_phases = await _find_posted_phases_any_author(thread)
        if posted_phases != [1, 2, 3, 4, 5, 6]:
            missing = sorted(set(range(1, 7)) - set(posted_phases))
            found = ", ".join(map(str, posted_phases)) or "ни одного"
            return (
                f"⚠️ В треде нашлись заголовки этапов: {found}. Не хватает: {', '.join(map(str, missing))}. "
                "План не сохранён — в треде должен быть текст ордера на все 6 этапов "
                "(в каждом сообщении должен быть виден номер этапа, например «1 этап» или «этап 1»)."
            )
        database.save_tb_plan(guild_id, name, thread.id, stars, created_by=str(author_id))
        return f"💾 План сохранён как «{name}» ({stars} ★, <#{thread.id}>)."


class TBPlanPanel(disnake.ui.View):
    def __init__(self, cog, guild_id: int, inter: disnake.ApplicationCommandInteraction):
        super().__init__(timeout=840)  # токен ответа на команду живёт 15 минут
        self.cog = cog
        self.guild_id = guild_id
        self.inter = inter
        self.author_id = inter.author.id
        self.stage = 1
        self.confirm_delete = None
        self.plan_id = None
        self._load()

    def _load(self):
        self.plans = database.get_tb_saved_plans(self.guild_id)
        cfg = database.get_guild_config(self.guild_id) or {}
        self.active_id = int(cfg["tb_active_plan_id"]) if cfg.get("tb_active_plan_id") else None
        ids = [p["id"] for p in self.plans]
        if self.plan_id not in ids:
            self.plan_id = self.active_id if self.active_id in ids else (ids[0] if ids else None)

    def _plan(self):
        return next((p for p in self.plans if p["id"] == self.plan_id), None)

    async def interaction_check(self, interaction: disnake.MessageInteraction) -> bool:
        return interaction.author.id == self.author_id

    def render(self) -> str:
        if not self.plans:
            return ("🗺️ **Планы ордера ТБ** — сохранённых планов пока нет. Соберите ордер командой "
                    "`/тб_ордер_из_картинки` (или на сайте, /tb/order-plans) либо сохраните готовую ветку кнопкой ниже.")
        lines = ["🗺️ **Планы ордера ТБ** — активный план бот публикует по этапам в канал плана ТБ."]
        for p in self.plans[:25]:
            mark = " — 🟢 **активный**" if p["id"] == self.active_id else ""
            where = f"<#{p['thread_id']}>" if p["thread_id"] else "на сайте"
            lines.append(f"• **{p['name']}** — {p['total_stars'] or '?'} ★, {where}{mark}")
        if len(self.plans) > 25:
            lines.append(f"…и ещё {len(self.plans) - 25} — удалите старые, в списке помещается 25.")
        return "\n".join(lines)

    def build(self):
        self.clear_items()
        has_plans = bool(self.plans)
        if has_plans:
            plan_select = disnake.ui.Select(
                placeholder="План", row=0,
                options=[disnake.SelectOption(label=p["name"][:100], value=str(p["id"]),
                                              description=f"{p['total_stars'] or '?'} ★" + (" · активный" if p["id"] == self.active_id else ""),
                                              default=p["id"] == self.plan_id)
                         for p in self.plans[:25]],
            )
            plan_select.callback = self._on_plan
            self.add_item(plan_select)
            stage_select = disnake.ui.Select(
                placeholder="Этап (для рассылки взводов)", row=1,
                options=[disnake.SelectOption(label=f"Этап {n}", value=str(n), default=n == self.stage) for n in range(1, 7)],
            )
            stage_select.callback = self._on_stage
            self.add_item(stage_select)

        buttons = []
        if has_plans:
            buttons += [
                ("Сделать активным", disnake.ButtonStyle.success, self._on_activate, 2, self.plan_id == self.active_id),
                ("Предпросмотр", disnake.ButtonStyle.secondary, self._on_preview, 2, False),
                ("Разослать взводы", disnake.ButtonStyle.primary, self._on_notify, 2, False),
            ]
            plan = self._plan()
            delete_label = (f"Точно удалить «{plan['name']}»?"[:80] if self.confirm_delete == self.plan_id and plan else "Удалить")
            buttons.append((delete_label, disnake.ButtonStyle.danger, self._on_delete, 3, False))
        buttons.append(("➕ Сохранить ветку", disnake.ButtonStyle.secondary, self._on_save, 3, False))
        for label, style, callback, row, disabled in buttons:
            button = disnake.ui.Button(label=label, style=style, row=row, disabled=disabled)
            button.callback = callback
            self.add_item(button)
        return self

    async def refresh(self):
        self._load()
        await self.inter.edit_original_response(content=self.render(), view=self.build())

    async def _on_plan(self, interaction: disnake.MessageInteraction):
        self.plan_id = int(interaction.values[0])
        self.confirm_delete = None
        await interaction.response.edit_message(content=self.render(), view=self.build())

    async def _on_stage(self, interaction: disnake.MessageInteraction):
        self.stage = int(interaction.values[0])
        await interaction.response.edit_message(view=self.build())

    async def _on_activate(self, interaction: disnake.MessageInteraction):
        plan = self._plan()
        database.update_guild_config(self.guild_id, tb_active_plan_id=plan["id"])
        self._load()
        await interaction.response.edit_message(content=self.render(), view=self.build())
        warning = config_warning_text(database.get_guild_config(self.guild_id), "tb_plan_order")
        await interaction.followup.send(
            f"✅ Активный план: «{plan['name']}» — ежедневная публикация берёт этапы из этой ветки."
            + (f"\n{warning}" if warning else ""), ephemeral=True)

    async def _on_preview(self, interaction: disnake.MessageInteraction):
        await interaction.response.defer(ephemeral=True)
        for chunk in await self.cog.plan_preview_chunks(self._plan()):
            await interaction.followup.send(chunk, ephemeral=True)

    async def _on_notify(self, interaction: disnake.MessageInteraction):
        await interaction.response.defer(ephemeral=True)
        plan = self._plan()
        rows = await tb_platoon_notify.build_player_rows(self.guild_id, plan, self.stage)
        if not rows:
            await interaction.followup.send(f"На этапе {self.stage} плана «{plan['name']}» никому не назначено ни одного слота.", ephemeral=True)
            return
        eligible = [r for r in rows if r["discord_id"]]
        skipped = [r for r in rows if not r["discord_id"]]
        if not eligible:
            await interaction.followup.send(
                f"На этапе {self.stage} назначено {len(rows)} игрок(ов), но ни у кого из них нет привязки Discord "
                f"(`/регистрация`) — рассылка невозможна.", ephemeral=True)
            return
        summary = f"Этап {self.stage}, план «{plan['name']}» — получат сообщение: {len(eligible)} игрок(ов)."
        if skipped:
            summary += f" Без привязки Discord (пропущены всегда): {len(skipped)}."
        summary += "\nМожно исключить конкретных игроков из выпадающих списков ниже — например тех, кто уже задонатил."
        await interaction.followup.send(summary, view=PlatoonNotifyView(self.guild_id, plan, self.stage, rows), ephemeral=True)

    async def _on_delete(self, interaction: disnake.MessageInteraction):
        plan = self._plan()
        if self.confirm_delete != self.plan_id:
            self.confirm_delete = self.plan_id
            await interaction.response.edit_message(view=self.build())
            return
        database.delete_tb_saved_plan(self.guild_id, plan["name"])
        self.confirm_delete = None
        self._load()
        await interaction.response.edit_message(content=self.render(), view=self.build())
        await interaction.followup.send(f"🗑️ План «{plan['name']}» удалён.", ephemeral=True)

    async def _on_save(self, interaction: disnake.MessageInteraction):
        await interaction.response.send_modal(SaveTBPlanModal(self))


class SaveTBPlanModal(disnake.ui.Modal):
    def __init__(self, panel: TBPlanPanel):
        self.panel = panel
        super().__init__(title="Сохранить ветку как план", components=[
            disnake.ui.TextInput(label="Ссылка на ветку с ордером на 6 этапов", custom_id="link",
                                 placeholder="https://discord.com/channels/…/…", max_length=200),
            disnake.ui.TextInput(label="Название плана", custom_id="name", max_length=80),
            disnake.ui.TextInput(label="Total Stars по плану", custom_id="stars", max_length=3, placeholder="например 44"),
        ])

    async def callback(self, interaction: disnake.ModalInteraction):
        await interaction.response.defer(ephemeral=True)
        values = interaction.text_values
        stars_raw = values["stars"].strip()
        if not stars_raw.isdigit():
            await interaction.followup.send("❌ Total Stars — целое число.", ephemeral=True)
            return
        name = values["name"].strip()
        if not name:
            await interaction.followup.send("❌ Название не может быть пустым.", ephemeral=True)
            return
        message = await self.panel.cog.save_thread_plan(self.panel.guild_id, values["link"], name, int(stars_raw),
                                                         interaction.author.id)
        await interaction.followup.send(message, ephemeral=True)
        try:
            await self.panel.refresh()
        except disnake.HTTPException:
            pass  # панель старше 15 минут — сам план уже сохранён


# =====================================================================
# Окно выбора получателей перед рассылкой взводов в личку (/тб_план → «Разослать взводы») —
# по прямому запросу пользователя 2026-09-14: "окно, где можно указать, кому отправить
# или кому НЕ отправлять, например при повторной отправке не тем, кто уже задонатил".
# Бот не знает, кто реально уже задонатил в игре (Comlink это не отдаёт надёжно) —
# исключение конкретных игроков офицер делает вручную через выпадающие списки.
# Сообщение всегда ephemeral, поэтому проверка "кто нажал кнопку" не нужна — кроме
# автора команды его вообще никто не видит.
# =====================================================================
class PlatoonNotifyView(disnake.ui.View):
    # 4 select'а по 25 — оставляем 5-й ряд под кнопки (Discord: максимум 5 рядов
    # компонентов на сообщение, один select занимает ряд целиком).
    MAX_SELECTABLE = 100

    def __init__(self, guild_id: int, plan: dict, round_num: int, rows: list[dict]):
        super().__init__(timeout=600)
        self.guild_id = guild_id
        self.plan = plan
        self.round_num = round_num
        self.eligible = [r for r in rows if r["discord_id"]]
        self.skipped_no_discord = [r for r in rows if not r["discord_id"]]
        self._selects: list[disnake.ui.Select] = []

        selectable = self.eligible[: self.MAX_SELECTABLE]
        for start in range(0, len(selectable), 25):
            chunk = selectable[start:start + 25]
            select = disnake.ui.Select(
                placeholder=f"Исключить из рассылки ({start + 1}-{start + len(chunk)})",
                min_values=0, max_values=len(chunk),
                options=[
                    disnake.SelectOption(
                        label=r["name"][:100], value=r["ally_code"],
                        description=f"{len(r['entries'])} слот(ов)"[:100],
                    )
                    for r in chunk
                ],
            )
            select.callback = self._on_select
            self._selects.append(select)
            self.add_item(select)

    async def _on_select(self, interaction: disnake.MessageInteraction):
        await interaction.response.defer()

    @disnake.ui.button(label="Отправить", style=disnake.ButtonStyle.success, row=4)
    async def send(self, button: disnake.ui.Button, interaction: disnake.MessageInteraction):
        excluded = {v for select in self._selects for v in (select.values or [])}
        send_to = {r["ally_code"] for r in self.eligible} - excluded
        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(content="⏳ Рассылаю...", view=self)

        token = os.environ.get("DISCORD_TOKEN")
        report = await tb_platoon_notify.send_broadcast(token, self.guild_id, self.plan, self.round_num, send_to)
        sent = sum(1 for r in report if r["status"] == "sent")
        failed = [r for r in report if r["status"] == "failed"]

        lines = [f"✅ Отправлено: {sent} из {len(report)}."]
        if excluded:
            lines.append(f"⏭ Исключено вручную: {len(excluded)}.")
        if self.skipped_no_discord:
            lines.append(f"⚠️ Без привязки Discord (пропущены всегда): {len(self.skipped_no_discord)}.")
        if failed:
            lines.append("❌ Ошибки:\n" + "\n".join(f"— {r['name']}: {r['detail']}" for r in failed[:10]))
        await interaction.edit_original_response(content="\n".join(lines), view=None)

    @disnake.ui.button(label="Отмена", style=disnake.ButtonStyle.secondary, row=4)
    async def cancel(self, button: disnake.ui.Button, interaction: disnake.MessageInteraction):
        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(content="Рассылка отменена.", view=None)


def setup(bot: commands.Bot):
    bot.add_cog(TBOrderImage(bot))
