"""Реестр шуточных фич бота + их вкл/выкл-состояние (database.get/set_fun_toggle).

Единственное действие сейчас — "tp_confirm": перед ЛЮБОЙ командой конкретного
Discord-пользователя (target_user_id) публично спрашивает "Будешь делать ТП?"
с кнопками Да/Нет. Хук, который реально перехватывает команды ДО их выполнения,
живёт в main.py (GuildManagerBot.process_application_commands — единственное
место, откуда можно и показать confirm-окно, и оставить оригинальную
interaction нетронутой для дальнейшего ответа самой команды, см. комментарий
там). Здесь — сам View с кнопками и функция run_confirm_gate, которые тот хук
вызывает, плюс database-обвязка для /фан (cogs/fun.py) и веб /admin/fun.
"""

import disnake

import database

DENY_MESSAGE = "🚫 Доступ к боту закрыт."

# key -> {label, description, question, target_user_id}. Добавлять новые
# шуточные действия сюда же — /фан и веб /admin/fun сами подхватят их через
# all_actions_with_status(), ничего больше трогать не нужно (кроме того места
# в main.py, которое реально решает КОГДА конкретное действие срабатывает).
FUN_ACTIONS: dict[str, dict] = {
    "tp_confirm": {
        "label": "«Будешь делать ТП?» перед каждой командой",
        "description": (
            "Перед выполнением любой команды у одного конкретного пользователя "
            "показывает публичное (видно всем в канале) окно с вопросом "
            "«Будешь делать ТП?» и кнопками Да/Нет. «Да» — команда выполняется "
            "как обычно. «Нет» (или если никто не нажал за 2 минуты) — команда "
            "не выполняется, показывается «Доступ к боту закрыт»."
        ),
        "question": "Будешь делать ТП?",
        "target_user_id": "1057173494451941476",
    },
}


def is_enabled(action_key: str) -> bool:
    return database.get_fun_toggle(action_key)


def all_actions_with_status() -> list[dict]:
    return [
        {"key": key, "enabled": is_enabled(key), **action}
        for key, action in FUN_ACTIONS.items()
    ]


def set_enabled(action_key: str, enabled: bool, updated_by: str) -> bool:
    if action_key not in FUN_ACTIONS:
        return False
    database.set_fun_toggle(action_key, enabled, updated_by)
    return True


def should_trigger(action_key: str, author_id: int) -> bool:
    action = FUN_ACTIONS.get(action_key)
    if action is None or str(author_id) != action["target_user_id"]:
        return False
    return is_enabled(action_key)


class _ConfirmView(disnake.ui.View):
    def __init__(self, author_id: int):
        super().__init__(timeout=120)
        self.author_id = author_id
        self.result = False

    async def interaction_check(self, interaction: disnake.MessageInteraction) -> bool:
        if interaction.author.id != self.author_id:
            await interaction.response.send_message("Это окно не для тебя.", ephemeral=True)
            return False
        return True

    async def _finish(self, interaction: disnake.MessageInteraction, result: bool, content: str):
        self.result = result
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(content=content, view=self)
        self.stop()

    @disnake.ui.button(label="Да", style=disnake.ButtonStyle.success)
    async def yes_button(self, button: disnake.ui.Button, interaction: disnake.MessageInteraction):
        await self._finish(interaction, True, "✅ Погнали.")

    @disnake.ui.button(label="Нет", style=disnake.ButtonStyle.danger)
    async def no_button(self, button: disnake.ui.Button, interaction: disnake.MessageInteraction):
        await self._finish(interaction, False, DENY_MESSAGE)


async def run_confirm_gate(interaction: disnake.ApplicationCommandInteraction, action_key: str) -> bool:
    """Показывает публичное Да/Нет-окно и ждёт ответа. Возвращает True, если
    исходную команду можно выполнять дальше — сама interaction при этом НЕ
    трогается (ни response, ни defer), чтобы обработчик команды мог ответить
    на неё как обычно, будто confirm-окна не было вовсе. При отказе/таймауте
    отвечает на interaction сам (ephemeral), иначе Discord показал бы
    "приложение не ответило" в клиенте вызвавшего."""
    action = FUN_ACTIONS[action_key]
    channel = interaction.channel
    if channel is None:
        return True

    view = _ConfirmView(interaction.author.id)
    msg = await channel.send(content=f"{interaction.author.mention} {action['question']}", view=view)
    timed_out = await view.wait()

    if not timed_out and view.result:
        return True

    if timed_out:
        try:
            await msg.edit(content=DENY_MESSAGE, view=None)
        except disnake.HTTPException:
            pass
    try:
        await interaction.response.send_message(DENY_MESSAGE, ephemeral=True)
    except disnake.HTTPException:
        pass
    return False
