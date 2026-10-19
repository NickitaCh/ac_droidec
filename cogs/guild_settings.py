"""/настройки — панель с per-guild каналами, ролями и модулями (guilds.*_channel_id/
*_role_id, feature_flags). До 2026-10-14 это были 16 подкоманд с автодополнением
каналов из guild_channels; теперь одна команда: раздел — выпадашкой, каналы и роли —
нативными списками Discord с поиском, вкл/выкл — кнопками. Сайт (/settings) —
полный эквивалент, плюс время рассылок и расписание тегов."""

from datetime import datetime, timezone

import disnake
from disnake.ext import commands

import database
import guild_resolver
from services import feature_flags


# (поле в guilds, человекочитаемое название) — подписи полей (services/config_status.py).
# "tb_ping"/"tb_plan_order" — НЕ колонки guilds, а services.feature_flags (см.
# _FEATURE_FLAG_FIELDS ниже и settings_list) — оставлены здесь только чтобы
# идти по порядку рядом с полями своего раздела, показываются отдельной
# веткой, не через guild_cfg.get(field).
SETTINGS_FIELDS = [
    ("tb_ping", "Модуль тегов-напоминаний (взводы/ордер) включён"),
    ("ping_channel_id", "Канал для тега на ротацию/взводы перед ТБ"),
    ("ping_role_id", "Роль, тегаемая на ротацию/взводы перед ТБ"),
    ("birthday_channel_id", "Канал для поздравлений с ДР"),
    ("birthday_role_id", "Роль, выдаваемая в ДР"),
    ("officer_channel_id", "Канал для автоотчёта и уведомлений по ТБ"),
    ("tb_plan_order", "Модуль автопубликации плана/ордера включён"),
    ("tb_plan_channel_id", "Канал анонсов плана ТБ (планеты + автоордера)"),
    ("tb_order_source_channel_id", "Канал/ветка-источник со стратегией на этапы ТБ"),
    ("tb_order_role_id", "Роль, тегаемая в автоордере ТБ"),
    ("tw_guide_forum_channel_id", "Форум-канал гайдов по контрам ВГ (для /вг_ордер)"),
    ("antispam_enabled", "Антиспам-детектор включён"),
    ("antispam_alert_channel_id", "Канал алертов антиспам-детектора (кросс-постинг/компрометация)"),
    ("antispam_alert_role_id", "Роль, тегаемая в алерте антиспама"),
    ("antispam_alert_message", "Кастомный текст алерта антиспама"),
    ("antispam_timeout_minutes", "Длительность автотайм-аута при обнаружении спам-рассылки (мин.)"),
    ("tasks_log_channel_id", "Канал уведомлений о задачах на прокачку (выполнено/просрочено/напоминание)"),
    ("tasks_mention", "Тег игрока при постановке задачи включён"),
    ("raid_tickets_channel_id", "Канал напоминаний про энку (рейдовые билеты)"),
]


# ---------------------------------------------------------------------------
# /настройки — панель (2026-10-14): одна команда без параметров вместо 16 подкоманд.
# Раздел выбирается в выпадашке, каналы и роли — нативными списками Discord с
# поиском (ChannelSelect/RoleSelect, с текущим значением), вкл/выкл — кнопками.
# Выбранный канал сам регистрируется в guild_channels (как /канал). Пустой выбор в
# списке — сброс значения. Время рассылок и расписание тегов — только на сайте.
# ---------------------------------------------------------------------------
SITE_URL = "https://swgoh-sng.ru"
TEXT_CHANNELS = [disnake.ChannelType.text, disnake.ChannelType.news]
THREAD_TYPES = [disnake.ChannelType.public_thread, disnake.ChannelType.private_thread, disnake.ChannelType.news_thread]
FEATURE_FLAG_FIELDS = {"tb_ping", "tb_plan_order", "tasks_mention", "raid_tickets"}

# (ключ, название, поля [(вид, поле, подпись, типы каналов)], только супер-админу)
SETTINGS_SECTIONS = [
    ("tb_ping", "ТБ — тег на подготовку", [
        ("flag", "tb_ping", "Тег-напоминания (взводы/ордер)", None),
        ("channel", "ping_channel_id", "Канал для тега на ротацию/взводы", TEXT_CHANNELS),
        ("role", "ping_role_id", "Роль, которую тегать", None),
    ], False),
    ("tb_plan", "ТБ — план и ордер", [
        ("flag", "tb_plan_order", "Автопубликация плана/ордера", None),
        ("channel", "tb_plan_channel_id", "Канал анонсов плана и автоордера", TEXT_CHANNELS),
        ("channel", "tb_order_source_channel_id", "Канал/ветка со стратегией по этапам", TEXT_CHANNELS + THREAD_TYPES),
        ("role", "tb_order_role_id", "Роль, которую тегать в ордере", None),
    ], False),
    ("tb_report", "ТБ — итоговый отчёт", [
        ("channel", "officer_channel_id", "Канал автоотчёта и уведомлений по ТБ", TEXT_CHANNELS),
    ], False),
    ("birthday", "День рождения", [
        ("channel", "birthday_channel_id", "Канал для поздравлений", TEXT_CHANNELS),
        ("role", "birthday_role_id", "Роль имениннику", None),
    ], False),
    ("tw", "ВГ — гайды по контрам", [
        ("channel", "tw_guide_forum_channel_id", "Форум-канал гайдов (для /вг_ордер)", [disnake.ChannelType.forum]),
    ], False),
    ("tasks", "Задачи на прокачку", [
        ("channel", "tasks_log_channel_id", "Канал уведомлений по задачам", TEXT_CHANNELS),
        ("flag", "tasks_mention", "Тег игрока при постановке задачи", None),
    ], False),
    ("raid_tickets", "Энка (рейдовые билеты)", [
        ("flag", "raid_tickets", "Напоминания и итог дня", None),
        ("channel", "raid_tickets_channel_id", "Канал напоминаний про энку", TEXT_CHANNELS),
    ], False),
    ("antispam", "Антиспам (супер-админ)", [
        ("flag", "antispam_enabled", "Антиспам-детектор", None),
        ("channel", "antispam_alert_channel_id", "Канал алертов", TEXT_CHANNELS),
        ("role", "antispam_alert_role_id", "Роль, которую тегать в алерте", None),
    ], True),
]


def _flag_enabled(guild_id: int, field: str, guild_cfg: dict) -> bool:
    if field in FEATURE_FLAG_FIELDS:
        return feature_flags.is_enabled(guild_id, field)
    return bool(guild_cfg.get(field))


def _set_flag(guild_id: int, field: str, value: bool, author_id) -> None:
    if field in FEATURE_FLAG_FIELDS:
        feature_flags.set_enabled(guild_id, field, value, updated_by=str(author_id))
    else:
        database.update_guild_config(guild_id, **{field: int(value)})


class GuildSettings(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @commands.slash_command(name="настройки", description="Каналы, роли и модули гильдии для авто-уведомлений бота — панель")
    @commands.check(lambda inter: guild_resolver.is_officer_for_resolved_guild(inter.author))
    async def settings(self, inter: disnake.ApplicationCommandInteraction):
        guild_id = await guild_resolver.require_guild_id(inter)
        if guild_id is None:
            return
        guild_cfg = database.get_guild_config(guild_id) or {}
        # Списки каналов и ролей Discord показывает только с того сервера, где вызвана
        # команда, — значит, вызывать нужно на сервере гильдии.
        if inter.guild is None or str(inter.guild.id) != str(guild_cfg.get("discord_guild_id")):
            await inter.response.send_message(
                "❌ Откройте `/настройки` на Discord-сервере вашей гильдии — каналы и роли выбираются из списка этого "
                f"сервера. Или настройте всё на сайте: {SITE_URL}/settings", ephemeral=True)
            return
        panel = SettingsPanel(self, guild_id, inter)
        await inter.response.send_message(panel.render(), view=panel.build(), ephemeral=True,
                                          allowed_mentions=disnake.AllowedMentions.none())

    def antispam_history_embed(self, guild_id: int, limit: int = 10):
        rows = database.get_antispam_log(guild_id, limit=limit)
        if not rows:
            return None
        embed = disnake.Embed(title=f"🚨 История антиспама (последние {len(rows)})", color=disnake.Color.orange())
        for row in rows:
            try:
                ts = int(datetime.strptime(row["created_at"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc).timestamp())
                when = f"<t:{ts}:f>"
            except ValueError:
                when = row["created_at"]
            timeout_part = f"тайм-аут {row['timeout_minutes']} мин." if row["timeout_applied"] else "⚠️ тайм-аут НЕ выдан (не хватило прав)"
            value = (
                f"🗑 Удалено: {row['messages_deleted']}"
                + (f" (не вышло: {row['messages_delete_failed']})" if row["messages_delete_failed"] else "")
                + f"\n⏱ {timeout_part}"
                + (f"\n📍 {row['channels']}" if row["channels"] else "")
            )
            embed.add_field(name=f"<@{row['discord_user_id']}> — {when}", value=value, inline=False)
        return embed


class SettingsPanel(disnake.ui.View):
    def __init__(self, cog: GuildSettings, guild_id: int, inter: disnake.ApplicationCommandInteraction):
        super().__init__(timeout=840)  # токен ответа на команду живёт 15 минут
        self.cog = cog
        self.guild_id = guild_id
        self.inter = inter
        self.author_id = inter.author.id
        self.is_super_admin = guild_resolver.is_super_admin(inter.author)
        self.sections = [s for s in SETTINGS_SECTIONS if self.is_super_admin or not s[3]]
        self.section_key = self.sections[0][0]

    async def interaction_check(self, interaction: disnake.MessageInteraction) -> bool:
        return interaction.author.id == self.author_id

    def _section(self):
        return next(s for s in self.sections if s[0] == self.section_key)

    def render(self) -> str:
        cfg = database.get_guild_config(self.guild_id) or {}
        _key, title, fields, _super = self._section()
        lines = [f"⚙️ **Настройки гильдии «{cfg.get('name', '')}»** — раздел **{title}**", ""]
        for kind, field, label, _types in fields:
            if kind == "flag":
                lines.append(f"• {label}: {'✅ включено' if _flag_enabled(self.guild_id, field, cfg) else '❌ выключено'}")
            else:
                raw = cfg.get(field)
                shown = (f"<#{raw}>" if kind == "channel" else f"<@&{raw}>") if raw else "*не задано*"
                lines.append(f"• {label}: {shown}")
        if self.section_key == "antispam":
            lines.append(f"• Тайм-аут: {cfg.get('antispam_timeout_minutes') or 60} мин.")
            lines.append(f"• Текст алерта: {cfg.get('antispam_alert_message') or '*по умолчанию*'}")
        lines += ["", "-# Пустой выбор в списке — сбросить значение. Время рассылок и расписание тегов — на сайте."]
        return "\n".join(lines)

    def build(self):
        self.clear_items()
        cfg = database.get_guild_config(self.guild_id) or {}
        section_select = disnake.ui.Select(
            placeholder="Раздел настроек", row=0,
            options=[disnake.SelectOption(label=title, value=key, default=key == self.section_key)
                     for key, title, _f, _s in self.sections],
        )
        section_select.callback = self._on_section
        self.add_item(section_select)

        _key, _title, fields, _super = self._section()
        row = 1
        for kind, field, label, types in fields:
            raw = cfg.get(field)
            if kind == "channel":
                select = disnake.ui.ChannelSelect(
                    placeholder=label, row=row, min_values=0, max_values=1, channel_types=types,
                    default_values=[disnake.SelectDefaultValue(int(raw), disnake.SelectDefaultValueType.channel)] if raw else None,
                )
            elif kind == "role":
                select = disnake.ui.RoleSelect(
                    placeholder=label, row=row, min_values=0, max_values=1,
                    default_values=[disnake.SelectDefaultValue(int(raw), disnake.SelectDefaultValueType.role)] if raw else None,
                )
            else:
                continue
            select.callback = self._make_select_callback(kind, field)
            self.add_item(select)
            row += 1

        for kind, field, label, _types in fields:
            if kind != "flag":
                continue
            enabled = _flag_enabled(self.guild_id, field, cfg)
            button = disnake.ui.Button(label=f"{label}: {'вкл' if enabled else 'выкл'}"[:80], row=4,
                                       style=disnake.ButtonStyle.success if enabled else disnake.ButtonStyle.secondary)
            button.callback = self._make_flag_callback(field, not enabled)
            self.add_item(button)
        if self.section_key == "antispam":
            for label, callback in (("Тайм-аут и текст", self._on_antispam_modal), ("История", self._on_antispam_history)):
                button = disnake.ui.Button(label=label, row=4, style=disnake.ButtonStyle.secondary)
                button.callback = callback
                self.add_item(button)
        self.add_item(disnake.ui.Button(label="Все настройки на сайте", url=f"{SITE_URL}/settings", row=4))
        return self

    async def _rerender(self, interaction: disnake.MessageInteraction):
        await interaction.response.edit_message(content=self.render(), view=self.build(),
                                                allowed_mentions=disnake.AllowedMentions.none())

    async def _on_section(self, interaction: disnake.MessageInteraction):
        self.section_key = interaction.values[0]
        await self._rerender(interaction)

    def _make_select_callback(self, kind: str, field: str):
        async def callback(interaction: disnake.MessageInteraction):
            value = interaction.values[0] if interaction.values else None
            if value and kind == "channel":
                channel = next(iter(interaction.resolved_values), None)
                channel_type = getattr(getattr(channel, "type", None), "name", None)
                database.register_guild_channel(self.guild_id, value, getattr(channel, "name", None) or value,
                                                channel_type=channel_type, registered_by=interaction.author.id)
            database.update_guild_config(self.guild_id, **{field: value})
            await self._rerender(interaction)
        return callback

    def _make_flag_callback(self, field: str, value: bool):
        async def callback(interaction: disnake.MessageInteraction):
            _set_flag(self.guild_id, field, value, interaction.author.id)
            await self._rerender(interaction)
        return callback

    async def _on_antispam_modal(self, interaction: disnake.MessageInteraction):
        await interaction.response.send_modal(AntispamModal(self))

    async def _on_antispam_history(self, interaction: disnake.MessageInteraction):
        embed = self.cog.antispam_history_embed(self.guild_id)
        if embed is None:
            await interaction.response.send_message("Пока нет ни одного срабатывания антиспам-детектора.", ephemeral=True)
            return
        await interaction.response.send_message(embed=embed, ephemeral=True)


class AntispamModal(disnake.ui.Modal):
    def __init__(self, panel: SettingsPanel):
        self.panel = panel
        cfg = database.get_guild_config(panel.guild_id) or {}
        super().__init__(title="Антиспам: тайм-аут и текст алерта", components=[
            disnake.ui.TextInput(label="Тайм-аут, минут (1–40320)", custom_id="timeout", max_length=5,
                                 value=str(cfg.get("antispam_timeout_minutes") or 60)),
            disnake.ui.TextInput(label="Текст алерта (пусто — по умолчанию)", custom_id="text", required=False,
                                 style=disnake.TextInputStyle.paragraph, max_length=1500,
                                 value=cfg.get("antispam_alert_message") or "",
                                 placeholder="Плейсхолдеры: {role} {member} {timeout} {reason} {channels} {count}"),
        ])

    async def callback(self, interaction: disnake.ModalInteraction):
        raw = interaction.text_values["timeout"].strip()
        if not raw.isdigit() or not 1 <= int(raw) <= 40320:
            await interaction.response.send_message("❌ Тайм-аут — целое число минут от 1 до 40320.", ephemeral=True)
            return
        text = interaction.text_values["text"].strip() or None
        database.update_guild_config(self.panel.guild_id, antispam_timeout_minutes=int(raw), antispam_alert_message=text)
        await interaction.response.send_message("✅ Настройки антиспама сохранены.", ephemeral=True)
        try:
            await self.panel.inter.edit_original_response(content=self.panel.render(), view=self.panel.build(),
                                                          allowed_mentions=disnake.AllowedMentions.none())
        except disnake.HTTPException:
            pass  # панель старше 15 минут — значения уже сохранены


def setup(bot: commands.Bot):
    bot.add_cog(GuildSettings(bot))
