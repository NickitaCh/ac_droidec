"""Даёт офицерам самим задавать per-guild каналы/роли (guilds.*_channel_id/
*_role_id в database.py), которые раньше можно было прописать только прямой
правкой БД при сидировании AbsoluteChaos в main.py — вторая обслуживаемая
гильдия оставалась с этими полями NULL без ручного вмешательства в БД. Один
общий /настройки со отдельными сабкомандами на каждое поле (по образцу уже
существующего /омикрон_текст канал в stat_requirements.py) — не единая
generic-команда с выбором типа настройки, это отдельная задача на будущее."""

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
]

# Те же поля, сгруппированные по режиму — используется только для отображения
# в /настройки список (эмбед по секциям) и должно соответствовать
# web/routes/guild_dashboard.py::GUILD_SETTINGS_GROUPS.
SETTINGS_GROUPS = [
    ("Территориальная битва (ТБ)", [
        "ping_channel_id", "ping_role_id",
        "tb_plan_channel_id", "tb_order_source_channel_id", "tb_order_role_id",
        "officer_channel_id",
    ]),
    ("День рождения", ["birthday_channel_id", "birthday_role_id"]),
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
                if not raw:
                    lines.append(f"• {label}: *не задано*")
                    continue
                is_role = field.endswith("_role_id")
                resolved = (inter.guild.get_role(int(raw)) if is_role else self.bot.get_channel(int(raw))) if inter.guild else None
                shown = resolved.mention if resolved else f"`{raw}` (не найден)"
                lines.append(f"• {label}: {shown}")
            embed.add_field(name=group_name, value="\n".join(lines), inline=False)
        await inter.response.send_message(embed=embed, ephemeral=True)


def setup(bot: commands.Bot):
    bot.add_cog(GuildSettings(bot))
