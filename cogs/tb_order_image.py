"""/тб_план — панель поверх database.tb_saved_plans: выбрать активный ордер
(guilds.tb_active_plan_id — его этапы tb_order_loop в cogs/guild_events.py публикует в 20:00),
предпросмотр, взводы, удалить; «Перенести из треда» — разовый перенос текстов этапов на сайт
(services/tb_order_store.py::import_thread). Ордер хранится на сайте, тред к нему не привязан.
Команда /тб_ордер_из_картинки удалена 2026-10-28 (неактуальна, решение пользователя)."""

import os

import disnake
from disnake.ext import commands

import database
import guild_resolver
import tb_platoon_notify
from services import feature_flags
from cogs.guild_events import chunk_tb_order_message
from services.config_status import config_warning_text
from services.message_image import extract_channel_id


class TBOrderImage(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # ------------------ /тб_план — панель (2026-10-14) ------------------
    # Одна команда без параметров вместо 6 подкоманд: в сообщении — список планов,
    # выпадашки «план» и «этап», кнопки действий. Ответы на действия — отдельными
    # скрытыми сообщениями, сама панель обновляется на месте.
    @commands.slash_command(name="тб_план", description="Ордер ТБ: выбрать активный, предпросмотр, взводы, перенести ордер из треда")
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
        """Разовый перенос ордера из треда на сайт (все 6 этапов); тред к ордеру не привязывается."""
        from services import tb_order_store

        channel_id = extract_channel_id(link)
        if channel_id is None:
            return "❌ Не удалось распознать ссылку на тред."
        try:
            await tb_order_store.import_thread(guild_id, channel_id, name, stars, str(author_id))
        except RuntimeError as e:
            return f"⚠️ {e} Ордер не сохранён."
        return f"💾 Ордер «{name}» ({stars} ★) перенесён из треда на сайт — тред больше не нужен."


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
                    "на сайте (вкладка ВИ → «План» → «Собрать ордер») либо перенесите готовый ордер из треда кнопкой ниже.")
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
        buttons.append(("➕ Перенести из треда", disnake.ButtonStyle.secondary, self._on_save, 3, False))
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
            f"✅ Активный план: «{plan['name']}» — бот публикует его этапы в 20:00."
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
                f"На этапе {self.stage} назначено {len(rows)} игрок(ов), но никто из них не зарегистрирован "
                f"(`/регистрация`) — писать некуда.", ephemeral=True)
            return
        summary = f"Этап {self.stage}, план «{plan['name']}» — получат сообщение: {len(eligible)} игрок(ов)."
        if skipped:
            summary += f" Не зарегистрированы (пропущены всегда): {len(skipped)}."
        mode = tb_platoon_notify.guild_mode(self.guild_id)
        summary += (f"\nКуда: {tb_platoon_notify.MODES[mode].lower()} (меняется на сайте: /tb/platoons, "
                    "блок «Рассылка взводов»).")
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
        super().__init__(title="Перенести ордер из треда", components=[
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

        by_where = {}
        for r in report:
            if r["status"] == "sent":
                where = "Telegram (Discord не принял)" if "→" in r["detail"] else r["detail"].split(" (")[0]
                by_where[where] = by_where.get(where, 0) + 1
        lines = [f"✅ Отправлено: {sent} из {len(report)}"
                 + (" — " + ", ".join(f"{k}: {v}" for k, v in by_where.items()) if by_where else "") + "."]
        if excluded:
            lines.append(f"⏭ Исключено вручную: {len(excluded)}.")
        if self.skipped_no_discord:
            lines.append(f"⚠️ Не зарегистрированы (пропущены всегда): {len(self.skipped_no_discord)}.")
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
