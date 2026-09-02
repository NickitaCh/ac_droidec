"""Даёт офицерам самим задавать per-guild каналы/роли (guilds.*_channel_id/
*_role_id в database.py), которые раньше можно было прописать только прямой
правкой БД при сидировании AbsoluteChaos в main.py — вторая обслуживаемая
гильдия оставалась с этими полями NULL без ручного вмешательства в БД. Один
общий /настройки со отдельными сабкомандами на каждое поле (по образцу уже
существующего /омикрон_текст канал в stat_requirements.py) — не единая
generic-команда с выбором типа настройки, это отдельная задача на будущее."""

from datetime import datetime, timezone
from typing import Union

import disnake
from disnake.ext import commands

import database
import guild_resolver

# (поле в guilds, человекочитаемое название) — для /настройки список
SETTINGS_FIELDS = [
    ("ping_channel_id", "Канал для тега на ротацию/взводы перед ТБ"),
    ("ping_role_id", "Роль, тегаемая на ротацию/взводы перед ТБ"),
    ("birthday_channel_id", "Канал для поздравлений с ДР"),
    ("birthday_role_id", "Роль, выдаваемая в ДР"),
    ("officer_channel_id", "Канал для автоотчёта и уведомлений по ТБ"),
    ("tb_plan_channel_id", "Канал анонсов плана ТБ (планеты + автоордера)"),
    ("tb_order_source_channel_id", "Канал/ветка-источник со стратегией на этапы ТБ"),
    ("tb_order_role_id", "Роль, тегаемая в автоордере ТБ"),
    ("tw_guide_forum_channel_id", "Форум-канал гайдов по контрам ВГ (для /вг_ордер)"),
    ("antispam_enabled", "Антиспам-детектор включён"),
    ("antispam_alert_channel_id", "Канал алертов антиспам-детектора (кросс-постинг/компрометация)"),
    ("antispam_alert_role_id", "Роль, тегаемая в алерте антиспама"),
    ("antispam_alert_message", "Кастомный текст алерта антиспама"),
    ("antispam_timeout_minutes", "Длительность автотайм-аута при обнаружении спам-рассылки (мин.)"),
]

# Те же поля, сгруппированные по режиму — используется только для отображения
# в /настройки список (эмбед по секциям) и должно соответствовать
# web/routes/guild_dashboard.py::GUILD_SETTINGS_GROUPS. Внутри ТБ раньше был
# один блок на все 6 полей — разбит на тег/план+ордер/отчёт вслед за тем, как
# уже названы сабкоманды (тб_ротация_*/тб_план_*/тб_ордер_*/тб_отчет_*), иначе
# один блок на 6 разнородных полей читался плохо что в Discord, что в вебе.
SETTINGS_GROUPS = [
    ("ТБ — тег на подготовку", ["ping_channel_id", "ping_role_id"]),
    ("ТБ — план и ордер", ["tb_plan_channel_id", "tb_order_source_channel_id", "tb_order_role_id"]),
    ("ТБ — итоговый отчёт", ["officer_channel_id"]),
    ("День рождения", ["birthday_channel_id", "birthday_role_id"]),
    ("Территориальная Война (ВГ)", ["tw_guide_forum_channel_id"]),
    ("Антиспам (защита от компрометации)", [
        "antispam_enabled", "antispam_alert_channel_id", "antispam_alert_role_id",
        "antispam_alert_message", "antispam_timeout_minutes",
    ]),
]
_SETTINGS_LABELS = dict(SETTINGS_FIELDS)


class GuildSettings(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def _set_field(self, inter: disnake.ApplicationCommandInteraction, field: str, value, human_label: str):
        guild_id = await guild_resolver.require_guild_id(inter)
        if guild_id is None:
            return
        database.update_guild_config(guild_id, **{field: str(value.id)})
        await inter.response.send_message(f"✅ {human_label} теперь: {value.mention}", ephemeral=True)

    @commands.slash_command(name="настройки", description="Каналы и роли гильдии для авто-уведомлений бота (только офицеры)")
    @commands.check(lambda inter: guild_resolver.is_officer_for_resolved_guild(inter.author))
    async def settings_group(self, inter: disnake.ApplicationCommandInteraction):
        pass

    @settings_group.sub_command(name="тб_ротация_канал", description="Канал, куда бот шлёт тег на ротацию/взводы перед ТБ")
    async def set_ping_channel(
        self, inter: disnake.ApplicationCommandInteraction,
        канал: disnake.TextChannel = commands.Param(description="Канал для тега на ротацию перед ТБ"),
    ):
        await self._set_field(inter, "ping_channel_id", канал, "Канал для тега на ротацию/взводы перед ТБ")

    @settings_group.sub_command(name="тб_ротация_роль", description="Роль, которую бот тегает на ротацию/взводы перед ТБ")
    async def set_ping_role(
        self, inter: disnake.ApplicationCommandInteraction,
        роль: disnake.Role = commands.Param(description="Тегаемая роль перед ТБ"),
    ):
        await self._set_field(inter, "ping_role_id", роль, "Роль, тегаемая на ротацию/взводы перед ТБ")

    @settings_group.sub_command(name="др_канал", description="Канал, куда бот пишет поздравления с днём рождения")
    async def set_birthday_channel(
        self, inter: disnake.ApplicationCommandInteraction,
        канал: disnake.TextChannel = commands.Param(description="Канал для поздравлений"),
    ):
        await self._set_field(inter, "birthday_channel_id", канал, "Канал для поздравлений с ДР")

    @settings_group.sub_command(name="др_роль", description="Роль, которую бот выдаёт имениннику на день рождения")
    async def set_birthday_role(
        self, inter: disnake.ApplicationCommandInteraction,
        роль: disnake.Role = commands.Param(description="Роль именинника"),
    ):
        await self._set_field(inter, "birthday_role_id", роль, "Роль, выдаваемая в ДР")

    @settings_group.sub_command(name="тб_отчет_канал", description="Канал, куда бот публикует автоотчёт по итогам ТБ и уведомления офицерам о ТБ")
    async def set_officer_channel(
        self, inter: disnake.ApplicationCommandInteraction,
        канал: disnake.TextChannel = commands.Param(description="Канал для автоотчёта и уведомлений по ТБ"),
    ):
        await self._set_field(inter, "officer_channel_id", канал, "Канал для автоотчёта и уведомлений по ТБ")

    @settings_group.sub_command(name="тб_план_канал", description="Канал анонсов плана ТБ — планеты по фазам и автоордера")
    async def set_tb_plan_channel(
        self, inter: disnake.ApplicationCommandInteraction,
        канал: disnake.TextChannel = commands.Param(description="Канал анонсов плана ТБ"),
    ):
        await self._set_field(inter, "tb_plan_channel_id", канал, "Канал анонсов плана ТБ")

    @settings_group.sub_command(name="тб_ордер_источник", description="Канал или ветка, откуда бот берёт стратегию по этапам ТБ")
    async def set_tb_order_source(
        self, inter: disnake.ApplicationCommandInteraction,
        канал: Union[disnake.TextChannel, disnake.Thread] = commands.Param(description="Канал или ветка со стратегией"),
    ):
        await self._set_field(inter, "tb_order_source_channel_id", канал, "Канал/ветка-источник стратегии ТБ")

    @settings_group.sub_command(name="тб_ордер_роль", description="Роль, которую бот тегает в автоматическом ордере на этап ТБ")
    async def set_tb_order_role(
        self, inter: disnake.ApplicationCommandInteraction,
        роль: disnake.Role = commands.Param(description="Тегаемая роль в ордере"),
    ):
        await self._set_field(inter, "tb_order_role_id", роль, "Роль, тегаемая в автоордере ТБ")

    @settings_group.sub_command(name="вг_гайды_канал", description="Форум-канал с гайдами по контрам ВГ, откуда бот берёт данные для /вг_ордер")
    async def set_tw_guide_forum_channel(
        self, inter: disnake.ApplicationCommandInteraction,
        канал: disnake.ForumChannel = commands.Param(description="Форум-канал гайдов по контрам ВГ"),
    ):
        await self._set_field(inter, "tw_guide_forum_channel_id", канал, "Форум-канал гайдов по контрам ВГ")

    @settings_group.sub_command(name="антиспам_режим", description="Включить или выключить антиспам-детектор (только супер-админ)")
    @commands.check(lambda inter: guild_resolver.is_super_admin(inter.author))
    async def set_antispam_mode(
        self, inter: disnake.ApplicationCommandInteraction,
        включить: bool = commands.Param(description="True — включить детектор, False — выключить"),
    ):
        guild_id = await guild_resolver.require_guild_id(inter)
        if guild_id is None:
            return
        database.update_guild_config(guild_id, antispam_enabled=int(включить))
        if включить:
            await inter.response.send_message(
                "✅ Антиспам-детектор включён. Проверьте, что задан канал алертов (`/настройки антиспам`) — без него сработавший "
                "детектор всё равно удалит сообщения и выдаст тайм-аут, но алерт офицерам отправить будет некуда.",
                ephemeral=True,
            )
        else:
            await inter.response.send_message("✅ Антиспам-детектор выключен.", ephemeral=True)

    @settings_group.sub_command(name="антиспам", description="Канал/роль/текст/таймаут алерта антиспама (только супер-админ)")
    @commands.check(lambda inter: guild_resolver.is_super_admin(inter.author))
    async def set_antispam(
        self, inter: disnake.ApplicationCommandInteraction,
        канал: disnake.TextChannel = commands.Param(default=None, description="Канал для алертов антиспама"),
        роль: disnake.Role = commands.Param(default=None, description="Роль, тегаемая в алерте"),
        таймаут: int = commands.Param(default=None, description="Длительность тайм-аута в минутах, 1–40320", min_value=1, max_value=40320),
        текст: str = commands.Param(
            default=None,
            description="Свой текст алерта. Плейсхолдеры: {role} {member} {timeout} {reason} {channels} {count}",
        ),
    ):
        guild_id = await guild_resolver.require_guild_id(inter)
        if guild_id is None:
            return
        updates = {}
        if канал is not None:
            updates["antispam_alert_channel_id"] = str(канал.id)
        if роль is not None:
            updates["antispam_alert_role_id"] = str(роль.id)
        if таймаут is not None:
            updates["antispam_timeout_minutes"] = таймаут
        if текст is not None:
            updates["antispam_alert_message"] = текст
        if not updates:
            await inter.response.send_message("❌ Укажите хотя бы один параметр: канал, роль, таймаут или текст.", ephemeral=True)
            return
        database.update_guild_config(guild_id, **updates)

        guild_cfg = database.get_guild_config(guild_id)
        ch = self.bot.get_channel(int(guild_cfg["antispam_alert_channel_id"])) if guild_cfg.get("antispam_alert_channel_id") else None
        r = inter.guild.get_role(int(guild_cfg["antispam_alert_role_id"])) if guild_cfg.get("antispam_alert_role_id") and inter.guild else None
        lines = [
            f"Канал: {ch.mention if ch else '*не задано*'}",
            f"Роль: {r.mention if r else '*не задано*'}",
            f"Таймаут: {guild_cfg.get('antispam_timeout_minutes') or 60} мин.",
            f"Текст: {guild_cfg.get('antispam_alert_message') or '*по умолчанию*'}",
        ]
        await inter.response.send_message("✅ Настройки антиспама обновлены:\n" + "\n".join(lines), ephemeral=True)

    @settings_group.sub_command(name="антиспам_история", description="Последние срабатывания антиспам-детектора (только супер-админ)")
    @commands.check(lambda inter: guild_resolver.is_super_admin(inter.author))
    async def antispam_history(
        self, inter: disnake.ApplicationCommandInteraction,
        лимит: int = commands.Param(default=10, description="Сколько последних записей показать", min_value=1, max_value=25),
    ):
        guild_id = await guild_resolver.require_guild_id(inter)
        if guild_id is None:
            return
        rows = database.get_antispam_log(guild_id, limit=лимит)
        if not rows:
            await inter.response.send_message("Пока нет ни одного срабатывания антиспам-детектора.", ephemeral=True)
            return
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
        await inter.response.send_message(embed=embed, ephemeral=True)

    @settings_group.sub_command(name="список", description="Показать текущие настройки каналов и ролей гильдии")
    async def settings_list(self, inter: disnake.ApplicationCommandInteraction):
        guild_id = await guild_resolver.require_guild_id(inter)
        if guild_id is None:
            return
        guild_cfg = database.get_guild_config(guild_id)
        embed = disnake.Embed(title="⚙️ Настройки гильдии", color=disnake.Color.blurple())
        for group_name, fields in SETTINGS_GROUPS:
            lines = []
            for field in fields:
                label = _SETTINGS_LABELS[field]
                raw = guild_cfg.get(field)
                if field == "antispam_enabled":
                    lines.append(f"• {label}: {'✅ да' if raw else '❌ нет'}")
                    continue
                if not raw:
                    lines.append(f"• {label}: *не задано*")
                    continue
                if field.endswith("_role_id"):
                    resolved = inter.guild.get_role(int(raw)) if inter.guild else None
                    shown = resolved.mention if resolved else f"`{raw}` (не найден)"
                elif field.endswith("_channel_id"):
                    resolved = self.bot.get_channel(int(raw))
                    shown = resolved.mention if resolved else f"`{raw}` (не найден)"
                else:
                    shown = raw
                lines.append(f"• {label}: {shown}")
            embed.add_field(name=group_name, value="\n".join(lines), inline=False)
        await inter.response.send_message(embed=embed, ephemeral=True)


def setup(bot: commands.Bot):
    bot.add_cog(GuildSettings(bot))
