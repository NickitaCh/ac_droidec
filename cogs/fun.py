"""Вкл/выкл шуточных фич бота (services/fun_features.py) — только супер-админ.
Сам перехват команд для срабатывания фичи живёт в main.py
(GuildManagerBot.process_application_commands), здесь только управление
тем, какие фичи сейчас активны."""

import disnake
from disnake.ext import commands

import guild_resolver
from services import fun_features


def _super_admin_only(inter: disnake.ApplicationCommandInteraction) -> bool:
    return guild_resolver.is_super_admin(inter.author)


# Строится один раз при импорте модуля из статического FUN_ACTIONS — как
# TIER_CHOICES в cogs/admin_management.py: name — то, что видит человек в
# Discord (кириллица), value — ключ действия, тот же, что в database.*_fun_toggle.
ACTION_CHOICES = [
    disnake.OptionChoice(name=action["label"], value=key)
    for key, action in fun_features.FUN_ACTIONS.items()
]


class FunCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.slash_command(name="фан", description="Управление шуточными фичами бота (только супер-админ)")
    @commands.check(_super_admin_only)
    async def fun_group(self, inter: disnake.ApplicationCommandInteraction):
        pass

    @fun_group.sub_command(name="список", description="Список шуточных фич и их текущего состояния")
    async def fun_list(self, inter: disnake.ApplicationCommandInteraction):
        await inter.response.defer(ephemeral=True)
        actions = fun_features.all_actions_with_status()
        lines = [
            f"{'✅' if a['enabled'] else '🚫'} **{a['label']}**\n{a['description']}"
            for a in actions
        ]
        embed = disnake.Embed(
            title="🎉 Шуточные фичи",
            description="\n\n".join(lines) or "— пока ничего нет —",
            color=disnake.Color.gold(),
        )
        await inter.edit_original_response(embed=embed)

    @fun_group.sub_command(name="включить", description="Включить шуточную фичу")
    async def fun_enable(
        self,
        inter: disnake.ApplicationCommandInteraction,
        фича: str = commands.Param(choices=ACTION_CHOICES, description="Какую фичу включить"),
    ):
        await inter.response.defer(ephemeral=True)
        fun_features.set_enabled(фича, True, str(inter.author.id))
        await inter.edit_original_response(content=f"✅ Включено: {fun_features.FUN_ACTIONS[фича]['label']}")

    @fun_group.sub_command(name="выключить", description="Выключить шуточную фичу")
    async def fun_disable(
        self,
        inter: disnake.ApplicationCommandInteraction,
        фича: str = commands.Param(choices=ACTION_CHOICES, description="Какую фичу выключить"),
    ):
        await inter.response.defer(ephemeral=True)
        fun_features.set_enabled(фича, False, str(inter.author.id))
        await inter.edit_original_response(content=f"🚫 Выключено: {fun_features.FUN_ACTIONS[фича]['label']}")


def setup(bot):
    bot.add_cog(FunCog(bot))
