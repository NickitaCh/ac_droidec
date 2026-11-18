"""Officer commands to publish an event panel in the current Discord channel."""
import asyncio
from io import BytesIO

import disnake
from disnake.ext import commands

import database
import guild_resolver
from services import bot_profiles, event_panels, game_account, game_event_live, game_watch


AC_DISCORD_SERVER = 931280548402442310
WATCH_INTERVAL = 300


def _profile():
    return bot_profiles.worker_profile() or bot_profiles.runtime_profile()


def _view(kind, token, expanded=()):
    view = disnake.ui.View(timeout=None)
    for index, (label, action) in enumerate(event_panels.BUTTONS[kind]):
        if action in expanded:
            label = {"details": "Скрыть зоны" if kind == "tw" else "Скрыть детали",
                     "mine": "Скрыть защиту" if kind == "tw" else "Скрыть взводы"}[action]
        view.add_item(disnake.ui.Button(
            label=label, style=disnake.ButtonStyle.primary if action in ("refresh", "watch") else disnake.ButtonStyle.secondary,
            row=index // 2, custom_id=f"glp:{kind}:{token}:{action}",
        ))
    if kind == "tw" and "details" in expanded:
        view.add_item(disnake.ui.Button(label="Следующая страница зон", row=2,
                                       custom_id=f"glp:{kind}:{token}:zones_next"))
    return view


class EventPanelCog(commands.Cog):
    def __init__(self):
        self._locks = {kind: asyncio.Lock() for kind in event_panels.BUTTONS}
        self._watch_owners = {}
        self._busy = set()

    @commands.slash_command(name="tb", description="Оперативная сводка ТБ", guild_ids=[AC_DISCORD_SERVER])
    async def tb(self, inter):
        pass

    @tb.sub_command(name="start", description="Опубликовать сводку ТБ в этом канале")
    async def tb_start(self, inter: disnake.ApplicationCommandInteraction):
        await self._start(inter, "tb")

    @commands.slash_command(name="tw", description="Оперативная сводка ВГ", guild_ids=[AC_DISCORD_SERVER])
    async def tw(self, inter):
        pass

    @tw.sub_command(name="start", description="Опубликовать сводку ВГ в этом канале")
    async def tw_start(self, inter: disnake.ApplicationCommandInteraction):
        await self._start(inter, "tw")

    @commands.slash_command(name="raid", description="Оперативная сводка рейда", guild_ids=[AC_DISCORD_SERVER])
    async def raid(self, inter):
        pass

    @raid.sub_command(name="start", description="Опубликовать сводку рейда в этом канале")
    async def raid_start(self, inter: disnake.ApplicationCommandInteraction):
        await self._start(inter, "raid")

    async def _start(self, inter, kind):
        access = await asyncio.to_thread(guild_resolver.resolve_access, inter.author)
        guild_id = access.get("guild_id")
        if not inter.guild_id or not event_panels.allowed(guild_id, access.get("tier"), _profile()):
            await inter.response.send_message("Команда доступна офицерам АС в канале тестового бота.", ephemeral=True)
            return
        cfg = await asyncio.to_thread(database.get_guild_config, guild_id)
        if not cfg or str(cfg.get("discord_guild_id")) != str(inter.guild_id):
            await inter.response.send_message("Откройте канал Discord, привязанный к вашей гильдии.", ephemeral=True)
            return
        await inter.response.defer(ephemeral=True)
        serial, token = await asyncio.to_thread(event_panels.reserve, "discord", guild_id, kind)
        text = await asyncio.to_thread(event_panels.latest, guild_id, kind)
        files = []
        if kind == "tw":
            snapshot = await asyncio.to_thread(game_event_live.snapshot, guild_id, str(inter.author.id), kind)
            if board := await asyncio.to_thread(event_panels.tw_board, snapshot.get("data")):
                files.append(disnake.File(BytesIO(board), filename="tw-map.png"))
                text = await asyncio.to_thread(event_panels.summary, kind, snapshot["data"], snapshot["at"])
        try:
            sent = await inter.channel.send(text, view=_view(kind, token), files=files,
                                            allowed_mentions=disnake.AllowedMentions.none())
        except disnake.HTTPException:
            await inter.edit_original_response(content="Не удалось опубликовать сводку в этом канале.")
            return
        ok, replaced = await asyncio.to_thread(
            event_panels.activate, "discord", guild_id, kind, serial, token, inter.channel_id, sent.id,
        )
        if not ok:
            await sent.edit(content="Эту панель уже заменила следующая команда.", view=None)
            await inter.edit_original_response(content="Уже опубликована более новая сводка.")
            return
        await inter.edit_original_response(content=f"Сводка опубликована: {sent.jump_url}", allowed_mentions=disnake.AllowedMentions.none())
        if replaced:
            await event_panels.edit(replaced, retire=True)

    @commands.Cog.listener("on_button_click")
    async def event_panel_button(self, inter: disnake.MessageInteraction):
        custom_id = inter.component.custom_id or ""
        parts = custom_id.split(":")
        if len(parts) != 4 or parts[0] != "glp":
            return
        _, kind, token, action = parts
        if kind not in event_panels.BUTTONS:
            return
        if kind == "tw" and action == "zones_next":
            await inter.response.defer(with_message=False)
            await self._toggle_details(inter, kind, token, "details", advance=True)
            return
        if action not in dict(event_panels.BUTTONS[kind]).values():
            return
        if kind == "tw" and action == "mine":
            await inter.response.defer(with_message=True, ephemeral=True)
            await self._personal_defense(inter, token)
            return
        if action in ("details", "mine"):
            await inter.response.defer(with_message=False)
            await self._toggle_details(inter, kind, token, action)
            return
        await inter.response.defer(with_message=True, ephemeral=True)
        await self._action(inter, kind, token, action, inter.message.id)

    async def _personal_defense(self, inter, token):
        access = await asyncio.to_thread(guild_resolver.resolve_access, inter.author)
        guild_id = access.get("guild_id")
        if not event_panels.allowed(guild_id, access.get("tier"), _profile()):
            await inter.edit_original_response(content="Доступно офицерам АС в тестовой версии.")
            return
        panel = await asyncio.to_thread(event_panels.current, "discord", guild_id, "tw")
        if not panel or (panel["token"], panel["channel"], panel["message"]) != (
                token, str(inter.channel_id), str(inter.message.id)):
            await inter.edit_original_response(content="Эта панель устарела.")
            return
        try:
            owner = str(inter.author.id)
            code = await asyncio.to_thread(game_account.select_account, owner, guild_id)
            lines = await asyncio.to_thread(event_panels.discord_details, guild_id, owner, code, "tw", "mine")
            chunks = ["**Моя защита**"]
            for line in lines:
                for start in range(0, max(1, len(line)), 1900):
                    part = line[start:start + 1900]
                    if len(chunks[-1]) + len(part) + 1 > 1900:
                        chunks.append("")
                    chunks[-1] += ("\n" if chunks[-1] else "") + part
            await inter.edit_original_response(content=chunks[0], allowed_mentions=disnake.AllowedMentions.none())
            for chunk in chunks[1:]:
                await inter.followup.send(chunk, ephemeral=True, allowed_mentions=disnake.AllowedMentions.none())
        except game_account.AccountError as error:
            await inter.edit_original_response(content=str(error))

    async def _toggle_details(self, inter, kind, token, action, *, advance=False):
        access = await asyncio.to_thread(guild_resolver.resolve_access, inter.author)
        guild_id = access.get("guild_id")
        if not event_panels.allowed(guild_id, access.get("tier"), _profile()):
            await inter.followup.send("Доступно офицерам АС в тестовой версии.", ephemeral=True)
            return
        try:
            async with self._locks[kind]:
                panel = await asyncio.to_thread(event_panels.current, "discord", guild_id, kind)
                if not panel or (panel["token"], panel["channel"], panel["message"]) != (
                        token, str(inter.channel_id), str(inter.message.id)):
                    await inter.followup.send("Эта панель устарела.", ephemeral=True)
                    return
                message = await inter.channel.fetch_message(inter.message.id)
                markers = {"details": "Подробнее по зонам" if kind != "raid" else "По игрокам",
                           "mine": "Назначения игрока"}
                embed_marker = markers[action]
                expanded = [key for key, marker in markers.items()
                            if any(embed.footer.text == marker for embed in message.embeds)]
                embeds = [embed for embed in message.embeds if embed.footer.text != embed_marker]
                if action in expanded and not advance:
                    expanded.remove(action)
                else:
                    owner = str(inter.author.id)
                    code = await asyncio.to_thread(game_account.select_account, owner, guild_id)
                    lines = await asyncio.to_thread(event_panels.discord_details, guild_id, owner, code, kind, action)
                    description = "\n".join(lines)
                    page_label = None
                    if kind == "tw" and action == "details":
                        pages, page = [""], 0
                        for line in lines:
                            # Split very large composition groups as well as long lists.
                            for start in range(0, max(1, len(line)), 2400):
                                part = line[start:start + 2400]
                                if len(pages[-1]) + len(part) + 1 > 2400:
                                    pages.append("")
                                pages[-1] += ("\n" if pages[-1] else "") + part
                        previous = next((e for e in message.embeds if e.footer.text == embed_marker), None)
                        if advance and previous and previous.author.name:
                            page = int(previous.author.name.split(" / ")[0]) % len(pages)
                        description = pages[page]
                        page_label = f"{page + 1} / {len(pages)}"
                    elif len(description) > 2700:
                        description = description[:2699].rsplit("\n", 1)[0] + "\n…"
                    title = dict((key, label) for label, key in event_panels.BUTTONS[kind])[action]
                    if action == "mine":
                        title += f" · {inter.author.display_name}"
                    embed = disnake.Embed(title=title[:256], description=description)
                    if page_label:
                        embed.set_author(name=page_label)
                    embed.set_footer(text=embed_marker)
                    embeds.append(embed)
                    if action not in expanded:
                        expanded.append(action)
                await message.edit(embeds=embeds, view=_view(kind, token, expanded),
                                   allowed_mentions=disnake.AllowedMentions.none())
        except (game_account.AccountError, disnake.HTTPException) as error:
            text = str(error) if isinstance(error, game_account.AccountError) else "Не удалось изменить панель."
            await inter.followup.send(text, ephemeral=True)

    async def _action(self, inter, kind, token, action, message_id, *, reconnect=False):
        access = await asyncio.to_thread(guild_resolver.resolve_access, inter.author)
        guild_id = access.get("guild_id")
        if not event_panels.allowed(guild_id, access.get("tier"), _profile()):
            await inter.edit_original_response(content="Доступно офицерам АС в тестовой версии.", view=None)
            return
        panel = await asyncio.to_thread(event_panels.current, "discord", guild_id, kind)
        if not panel or (panel["token"], panel["channel"], panel["message"]) != (
                token, str(inter.channel_id), str(message_id)):
            await inter.edit_original_response(content="Эта панель устарела. Откройте последнюю сводку.", view=None)
            return
        owner = str(inter.author.id)
        scope = (owner, guild_id, _profile(), access["tier"])
        if kind in self._busy:
            await inter.edit_original_response(content="⏳ Обновление уже идёт. Дождитесь результата — повторно нажимать не нужно.", view=None)
            return
        self._busy.add(kind)
        try:
            await inter.edit_original_response(content="⏳ Обновляю сводку из игры… Дождитесь результата.", view=None)
            await event_panels.set_refreshing(panel, True)
            code = await asyncio.to_thread(game_account.select_account, owner, guild_id)
            async with self._locks[kind]:
                watching = self._watch_owners.get(kind)
                active = watching and game_watch.active(guild_id, watching, kind)
                if action == "watch" and active:
                    if watching == owner:
                        game_watch.stop(guild_id, owner, kind)
                        reply = "Останавливаю наблюдение."
                    else:
                        reply = "Наблюдение уже запустил другой офицер."
                else:
                    if reconnect:
                        await game_account.reconnect(*scope, code)
                    state = await game_watch.refresh_saved(*scope, code, kind, reconnect_if_expired=action == "refresh")
                    if action == "watch" and state and (kind != "raid" or state["active"]):
                        await game_watch.start(*scope, code, kind, WATCH_INTERVAL, saved_session=True)
                        self._watch_owners[kind] = owner
                        reply = "Наблюдение запущено. Обновляю сводку каждые 5 минут. Для остановки нажмите «Наблюдение» ещё раз."
                    else:
                        reply = "Сводка обновлена." if state and (kind != "raid" or state["active"]) else "Игра не вернула активное событие."
            await inter.edit_original_response(content=reply, view=None)
        except game_account.SessionExpired:
            if reconnect or action == "refresh":
                await self._link(inter, owner, guild_id, code)
                return
            view = disnake.ui.View(timeout=120)
            button = disnake.ui.Button(label="Войти и продолжить", style=disnake.ButtonStyle.primary)

            async def confirm(click):
                if click.author.id != inter.author.id:
                    await click.response.send_message("Это подтверждение для автора запроса.", ephemeral=True)
                    return
                view.stop()
                await click.response.defer(with_message=False)
                await self._action(click, kind, token, action, message_id, reconnect=True)

            button.callback = confirm
            view.add_item(button)
            await inter.edit_original_response(content=game_account.LOGIN_WARNING, view=view)
        except game_account.ConnectionRequired:
            await self._link(inter, owner, guild_id, code)
        except game_account.AccountError as error:
            await inter.edit_original_response(content=str(error), view=None)
        finally:
            try:
                await event_panels.set_refreshing(panel, False)
            finally:
                self._busy.discard(kind)

    async def _link(self, inter, owner, guild_id, code):
        path = await asyncio.to_thread(game_account.link_path, owner, guild_id, code)
        view = disnake.ui.View()
        view.add_item(disnake.ui.Button(label="Подключить через EA", url=event_panels.SITE + path,
                                        style=disnake.ButtonStyle.link))
        await inter.edit_original_response(content="Подключите игровой аккаунт для обновления сводки.", view=view)


def setup(bot):
    if _profile() == event_panels.PROFILE:
        bot.add_cog(EventPanelCog())
