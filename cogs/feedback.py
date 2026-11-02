"""Форма обратной связи, доступная без регистрации."""
import disnake
from disnake.ext import commands
import guild_resolver
from services import feedback


class FeedbackModal(disnake.ui.Modal):
    def __init__(self):
        super().__init__(title="Обратная связь", components=[
            disnake.ui.TextInput(label="Ошибка, предложение или вопрос", custom_id="message",
                                 style=disnake.TextInputStyle.paragraph, max_length=4000),
        ])

    async def callback(self, inter):
        await inter.response.defer(ephemeral=True)
        access = guild_resolver.resolve_access(str(inter.author.id))
        try:
            report_id = feedback.create(str(inter.author.id), inter.author.display_name,
                                        access.get("guild_id"), inter.text_values["message"],
                                        page_url="https://discord.com/channels/@me", user_agent="Discord")
        except ValueError as e:
            await inter.edit_original_response(str(e))
            return
        await inter.edit_original_response(f"Обращение №{report_id} отправлено Nod. Ответ придёт сюда, в личку бота.")
        await feedback.notify(report_id)


class FeedbackCog(commands.Cog):
    @commands.slash_command(name="обратная_связь", description="Сообщить об ошибке, предложить улучшение или задать вопрос Nod")
    async def feedback_cmd(self, inter):
        await inter.response.send_modal(FeedbackModal())


def setup(bot):
    bot.add_cog(FeedbackCog())
