"""/грант — панель ручного доступа для участников своей гильдии (офицеры). Офицерская
версия супер-админских грантов (/admin/access на сайте), но с жёстко зафиксированным
guild_id (своя гильдия) — см. services/guild_admin.py::add_guild_scoped_grant/
remove_guild_scoped_grant за проверкой, что офицер не может перехватить/снять чужой
грант. С 2026-10-14 — одна команда с панелью вместо добавить/удалить/список."""

import disnake
from disnake.ext import commands

import guild_resolver
from services.guild_admin import add_guild_scoped_grant, list_grants_for_guild, remove_guild_scoped_grant

TIER_LABELS = {"member": "Игрок", "officer": "Офицер"}


class GuildGrantsCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.slash_command(name="грант", description="Ручной доступ к боту для участника вашей гильдии — панель")
    @commands.check(lambda inter: guild_resolver.is_officer_for_resolved_guild(inter.author))
    async def grant_panel(self, inter: disnake.ApplicationCommandInteraction):
        guild_id = await guild_resolver.require_guild_id(inter)
        if guild_id is None:
            return
        if inter.guild is None:
            await inter.response.send_message("❌ Откройте `/грант` на сервере гильдии — участник выбирается из списка сервера.",
                                              ephemeral=True)
            return
        panel = GrantPanel(self, guild_id, inter)
        await inter.response.send_message(panel.render(), view=panel.build(), ephemeral=True,
                                          allowed_mentions=disnake.AllowedMentions.none())


class GrantPanel(disnake.ui.View):
    def __init__(self, cog, guild_id: int, inter: disnake.ApplicationCommandInteraction):
        super().__init__(timeout=840)  # токен ответа на команду живёт 15 минут
        self.cog = cog
        self.guild_id = guild_id
        self.inter = inter
        self.author_id = inter.author.id
        self.user = None
        self.tier = "member"
        self.confirm_remove = False

    async def interaction_check(self, interaction: disnake.MessageInteraction) -> bool:
        return interaction.author.id == self.author_id

    def render(self) -> str:
        grants = list_grants_for_guild(self.guild_id)
        lines = [
            f"• <@{g['discord_id']}> — `{g['ally_code']}` ({g.get('ingame_name') or '?'}), **{TIER_LABELS.get(g['tier'], g['tier'])}** "
            f"(выдал <@{g['granted_by']}>, {str(g['granted_at'])[:10]})"
            for g in grants
        ] or ["— никого —"]
        return ("👑 **Ручной доступ вашей гильдии** — для тех, кого бот не узнаёт по игровому рангу.\n"
                + "\n".join(lines)
                + "\n\n-# Выберите участника → «Выдать доступ» (спросит код союзника) или «Снять доступ».")

    def build(self):
        self.clear_items()
        user_select = disnake.ui.UserSelect(
            placeholder="Участник сервера", row=0,
            default_values=[disnake.SelectDefaultValue(self.user.id, disnake.SelectDefaultValueType.user)] if self.user else None)
        user_select.callback = self._on_user
        self.add_item(user_select)
        tier_select = disnake.ui.Select(placeholder="Уровень доступа", row=1, options=[
            disnake.SelectOption(label=label, value=key, default=key == self.tier) for key, label in TIER_LABELS.items()])
        tier_select.callback = self._on_tier
        self.add_item(tier_select)
        add = disnake.ui.Button(label="Выдать доступ", style=disnake.ButtonStyle.success, row=2, disabled=self.user is None)
        add.callback = self._on_add
        self.add_item(add)
        remove = disnake.ui.Button(label="Точно снять?" if self.confirm_remove else "Снять доступ",
                                   style=disnake.ButtonStyle.danger, row=2, disabled=self.user is None)
        remove.callback = self._on_remove
        self.add_item(remove)
        return self

    async def refresh(self, interaction=None):
        kwargs = dict(content=self.render(), view=self.build(), allowed_mentions=disnake.AllowedMentions.none())
        if interaction is not None:
            await interaction.response.edit_message(**kwargs)
        else:
            await self.inter.edit_original_response(**kwargs)

    async def _on_user(self, interaction):
        self.user = interaction.resolved_values[0] if interaction.resolved_values else None
        self.confirm_remove = False
        await self.refresh(interaction)

    async def _on_tier(self, interaction):
        self.tier = interaction.values[0]
        await self.refresh(interaction)

    async def _on_add(self, interaction):
        await interaction.response.send_modal(GrantAllyCodeModal(self))

    async def _on_remove(self, interaction):
        if not self.confirm_remove:
            self.confirm_remove = True
            await self.refresh(interaction)
            return
        self.confirm_remove = False
        result = remove_guild_scoped_grant(str(self.user.id), self.guild_id)
        await self.refresh(interaction)
        await interaction.followup.send(f"✅ Доступ снят с {self.user.mention}." if result.ok else f"❌ {result.error}",
                                        ephemeral=True)


class GrantAllyCodeModal(disnake.ui.Modal):
    def __init__(self, panel: GrantPanel):
        self.panel = panel
        super().__init__(title=f"Доступ «{TIER_LABELS[panel.tier]}»: {panel.user.display_name}"[:45], components=[
            disnake.ui.TextInput(label="Код союзника (9 цифр)", custom_id="ally_code", min_length=9, max_length=11),
        ])

    async def callback(self, interaction: disnake.ModalInteraction):
        await interaction.response.defer(ephemeral=True)
        panel = self.panel
        result = await add_guild_scoped_grant(panel.cog.bot.comlink, str(panel.user.id), interaction.text_values["ally_code"],
                                              panel.tier, panel.guild_id, str(interaction.author.id))
        if not result.ok:
            await interaction.followup.send(f"❌ {result.error}", ephemeral=True)
            return
        await interaction.followup.send(
            f"✅ {panel.user.mention} ({result.ingame_name}) получил уровень «{TIER_LABELS[panel.tier]}» в вашей гильдии.",
            ephemeral=True)
        try:
            await panel.refresh()
        except disnake.HTTPException:
            pass  # панель старше 15 минут — доступ уже выдан


def setup(bot):
    bot.add_cog(GuildGrantsCog(bot))
