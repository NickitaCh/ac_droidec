"""Private order editor opened from the public /тб старт panel."""
import asyncio
import time

import disnake
from services import event_panels, game_account as ga, tb_commands as orders


class Signature(disnake.ui.Modal):
    def __init__(self, editor):
        self.editor = editor
        super().__init__(title='Подпись приказа', components=[disnake.ui.TextInput(
            label='Подпись в игре (можно оставить пустой)', custom_id='message',
            value=editor.message, required=False, max_length=140)])

    async def callback(self, inter):
        if not await self.editor.interaction_check(inter):
            return
        self.editor.message = inter.text_values['message']
        await inter.response.edit_message(content=self.editor.text(), view=self.editor)


class OrderEditor(disnake.ui.View):
    def __init__(self, user, code, state):
        super().__init__(timeout=600)
        self.user, self.code, self.state = user, code, state
        self.target, self.offset, self.busy = None, 0, False
        self.command, self.message = 1, ''
        self.build()

    async def interaction_check(self, inter):
        if str(inter.author.id) != str(self.user['discord_id']):
            await inter.response.send_message('Откройте своё управление приказами из сводки ТБ.', ephemeral=True)
            return False
        if self.busy:
            await inter.response.send_message('Дождитесь завершения обновления приказа.', ephemeral=True)
            return False
        try:
            await asyncio.to_thread(orders._authorize, self.user, self.code)
            return True
        except ga.AccountError as error:
            await inter.response.send_message(str(error), ephemeral=True)
            return False

    def build(self):
        self.clear_items()
        available = orders.targets(self.state)
        keys = list(available)
        self.offset = min(self.offset, max(0, ((len(keys) - 1) // 20) * 20))
        if self.target not in keys:
            self.target = keys[self.offset] if keys else None
            self._load_target()
        if keys:
            select = disnake.ui.StringSelect(placeholder='Планета, бой или ОЗ', row=0, options=[
                disnake.SelectOption(label=available[k]['label'][:100], value=k, default=k == self.target)
                for k in keys[self.offset:self.offset + 20]])

            async def choose(inter):
                self.target = inter.values[0]
                self._load_target()
                self.build()
                await inter.response.edit_message(content=self.text(), view=self)

            select.callback = choose
            self.add_item(select)
            command = disnake.ui.StringSelect(row=1, options=[disnake.SelectOption(
                label=label, value=str(key), default=key == self.command) for key, label in orders.STATES.items()])

            async def choose_command(inter):
                self.command = int(inter.values[0])
                self.build()
                await inter.response.edit_message(content=self.text(), view=self)

            command.callback = choose_command
            self.add_item(command)
        for label, callback, row in [
            ('Применить в игре', self.apply, 2), ('Подпись', self.signature, 2),
            ('Обновить из игры', self.refresh, 2),
        ]:
            button = disnake.ui.Button(label=label, row=row, disabled=not keys and label != 'Обновить из игры')
            button.callback = callback
            self.add_item(button)
        if len(keys) > 20:
            button = disnake.ui.Button(label=f'Другие цели · {self.offset // 20 + 1}/{(len(keys) + 19) // 20}', row=3)
            button.callback = self.next_page
            self.add_item(button)
        self.add_item(disnake.ui.Button(label='Автоправила и все приказы в вебе', url=event_panels.SITE + '/tb/commands', row=4))

    def _load_target(self):
        target = orders.targets(self.state).get(self.target, {})
        self.command, self.message = target.get('command', 1), target.get('message', '')

    def text(self):
        target = orders.targets(self.state).get(self.target)
        if not target:
            return 'Нет открытых целей ТБ. Обновите данные из игры.'
        escape = disnake.utils.escape_markdown
        return (f"**Приказы ТБ · этап {self.state['phase']}**\n**{escape(target['label'])}**\n"
                f"В игре: {orders.STATES.get(target.get('command'), 'Неизвестно')} · {escape(target.get('message', '') or 'без подписи')}\n"
                f"Выбрано: **{orders.STATES[self.command]}** · {escape(self.message or 'без подписи')}\n"
                'Нажмите «Применить в игре» для сохранения. Для концентрации на бое выберите конкретный «Бой».\n'
                'Ручное применение отключает автоправила для этой цели.')

    async def signature(self, inter):
        await inter.response.send_modal(Signature(self))

    async def next_page(self, inter):
        keys = list(orders.targets(self.state))
        self.offset = self.offset + 20 if self.offset + 20 < len(keys) else 0
        self.target = keys[self.offset]
        self._load_target()
        self.build()
        await inter.response.edit_message(content=self.text(), view=self)

    async def refresh(self, inter):
        await self.update(inter, apply=False)

    async def apply(self, inter):
        await self.update(inter, apply=True)

    async def update(self, inter, *, apply):
        self.busy = True
        await inter.response.defer(with_message=False)
        try:
            if apply:
                old = orders.targets(self.state)[self.target]
                self.state = await orders.change(self.user, self.code, self.state['event'], self.state['phase'],
                                                  self.target, self.command, self.message,
                                                  [old.get('command', 1), old.get('message', '')])
                await event_panels.publish(self.user['guild_id'], 'tb', self.state, int(time.time()))
            else:
                self.state = await orders.read(self.user, self.code)
            self._load_target()
            self.build()
            await inter.edit_original_response(content=('Приказ подтверждён игрой.\n' if apply else '') + self.text(), view=self,
                                               allowed_mentions=disnake.AllowedMentions.none())
        except ga.AccountError as error:
            await inter.followup.send(str(error), ephemeral=True, allowed_mentions=disnake.AllowedMentions.none())
        finally:
            self.busy = False
