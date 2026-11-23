"""Officer commands to publish an event panel in the current Discord channel."""
import asyncio
from io import BytesIO
from urllib.parse import urlencode

import disnake
from disnake.ext import commands

import database
import guild_resolver
from services import bot_profiles, event_panels, game_account, game_event_live, game_watch, tw_battle_log, tw_score


AC_DISCORD_SERVER = 931280548402442310
WATCH_INTERVAL = 300


def _attack_name(value):
    return disnake.utils.escape_markdown(" ".join(value.split())[:45])


def _attack_table(zones):
    rows = ["Зона      Снято  Удерж.   1-я  −Стяги"]
    for zone in zones:
        defeated = event_panels._score(zone["defeated"])
        capacity = event_panels._score(zone["capacity"])
        holds = event_panels._score(zone["holds"])
        first = "—" if zone["first"] is None else f"{zone['first']}%"
        lost = event_panels._score(zone["lost"])
        label = zone["name"] + ("✓" if zone["captured"] else "")
        rows.append(f"{label:<8} {defeated + '/' + capacity:>6} {holds:>7} {first:>5} {lost:>7}")
    return "```\n" + "\n".join(rows) + "\n```"


def _profile():
    return bot_profiles.worker_profile() or bot_profiles.runtime_profile()


def _view(kind, token, expanded=()):
    view = disnake.ui.View(timeout=None)
    for index, (label, action) in enumerate(event_panels.BUTTONS[kind]):
        if action in expanded:
            label = {"details": {"tw": "Скрыть зоны", "raid": "Скрыть игроков"}.get(kind, "Скрыть детали"),
                     "mine": "Скрыть защиту" if kind == "tw" else "Скрыть взводы"}[action]
        view.add_item(disnake.ui.Button(
            label=label, style=disnake.ButtonStyle.primary if action in ("refresh", "watch") else disnake.ButtonStyle.secondary,
            row=index // 2, custom_id=f"glp:{kind}:{token}:{action}",
        ))
    if kind == "tw" and "details" in expanded:
        view.add_item(disnake.ui.Button(label="Следующая страница зон", row=2,
                                       custom_id=f"glp:{kind}:{token}:zones_next"))
    if kind == "tw":
        view.add_item(disnake.ui.Button(label="Журнал боёв", style=disnake.ButtonStyle.secondary,
                                       row=2, custom_id=f"glp:{kind}:{token}:battles"))
        view.add_item(disnake.ui.Button(label="Анализ ВГ", style=disnake.ButtonStyle.secondary,
                                       row=2, custom_id=f"glp:{kind}:{token}:attack_report"))
    return view


class EventPanelCog(commands.Cog):
    def __init__(self):
        self._locks = {kind: asyncio.Lock() for kind in event_panels.BUTTONS}
        self._watch_owners = {}
        self._busy = set()

    @commands.slash_command(name="вг", description="Оперативная сводка ВГ", guild_ids=[AC_DISCORD_SERVER])
    async def tw(self, inter):
        pass

    @tw.sub_command(name="старт", description="Опубликовать сводку ВГ в этом канале")
    async def tw_start(self, inter: disnake.ApplicationCommandInteraction):
        await self._start(inter, "tw")

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
        elif kind == "raid":
            from services.raid_live_image import guild_card

            snapshot = await asyncio.to_thread(game_event_live.latest_guild_snapshot, guild_id, kind)
            data = snapshot.get("data")
            if data and data["active"]:
                board = await asyncio.to_thread(guild_card, data["active"][0])
                files.append(disnake.File(BytesIO(board), filename="raid-guild.png"))
                text = await asyncio.to_thread(event_panels.summary, kind, data, snapshot["at"])
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
        if kind == "tw" and action == "battles":
            await inter.response.defer(with_message=True, ephemeral=True)
            await self._battle_log(inter, token)
            return
        if kind == "tw" and action == "attack_report":
            await inter.response.defer(with_message=True, ephemeral=True)
            await self._attack_analysis(inter, token)
            return
        if kind == "tw" and action.startswith("battle-page|"):
            _, zone, page = action.split("|", 2)
            await inter.response.defer(with_message=False)
            await self._battle_log(inter, token, zone=zone, page=int(page))
            return
        if action not in dict(event_panels.BUTTONS[kind]).values():
            return
        if action == "mine":
            await inter.response.defer(with_message=True, ephemeral=True)
            await self._personal_assignments(inter, kind, token)
            return
        if action == "details":
            await inter.response.defer(with_message=False)
            await self._toggle_details(inter, kind, token, action)
            return
        await inter.response.defer(with_message=True, ephemeral=True)
        await self._action(inter, kind, token, action, inter.message.id)

    @commands.Cog.listener("on_dropdown")
    async def event_battle_zone(self, inter: disnake.MessageInteraction):
        parts = (inter.component.custom_id or "").split(":")
        if len(parts) == 4 and parts[:2] == ["glp", "tw"] and parts[3] == "battle-zone":
            await inter.response.defer(with_message=False)
            await self._battle_log(inter, parts[2], zone=inter.values[0])

    async def _attack_analysis(self, inter, token):
        access = await asyncio.to_thread(guild_resolver.resolve_access, inter.author)
        guild_id = access.get("guild_id")
        if inter.guild_id != AC_DISCORD_SERVER or not event_panels.allowed(guild_id, access.get("tier"), _profile()):
            await inter.edit_original_response(content="Доступно офицерам АС в тестовой версии.")
            return
        panel = await asyncio.to_thread(event_panels.current, "discord", guild_id, "tw")
        if not panel or (panel["token"], panel["channel"], panel["message"]) != (
                token, str(inter.channel_id), str(inter.message.id)):
            await inter.edit_original_response(content="Эта панель устарела.")
            return
        snapshot = await asyncio.to_thread(game_event_live.latest_tw_snapshot, guild_id)
        sides = await asyncio.to_thread(tw_score.analysis, snapshot.get("data"))
        if not sides:
            await inter.edit_original_response(content="Анализ появится после начала боёв и обновления панели.")
            return
        report = disnake.Embed(
            title="Анализ ВГ", url=f"{event_panels.SITE}/game-account/events/tw#attack-analysis",
            description=f"Снимок игры · {event_panels._clock(snapshot['at'])} МСК",
            color=0x5865F2,
        )
        for side in sides:
            report.add_field(name=_attack_name(side["name"]), value="\n".join((
                f"**{event_panels._score(side['current'])}** · текущий счёт",
                f"**+{event_panels._score(side['potential'])}** · потенциал" if side["potential"] is not None
                else "**—** · потенциал",
                f"**{event_panels._score(side['maximum'])}** · максимум",
            )), inline=True)
        for side in sides:
            name = _attack_name(side["name"])
            if side["zones"]:
                report.add_field(name=f"Атака · {name}", value=_attack_table(side["zones"]), inline=False)
        for side in sides:
            if side["defenders"]:
                report.add_field(name=f"Лучшая защита · {_attack_name(side['name'])}", value="\n".join(
                    f"{_attack_name(row['name'])} · {row['holds']}" for row in side["defenders"]), inline=True)
        report.set_footer(text="1-я — доля побед с первой попытки. −Стяги — потери от идеального прохода.\n"
                               "Максимум при зачистке всех зон. Скрытые отряды считаются нетронутыми.")
        await inter.edit_original_response(embed=report, allowed_mentions=disnake.AllowedMentions.none())

    async def _battle_log(self, inter, token, *, zone=None, page=0):
        access = await asyncio.to_thread(guild_resolver.resolve_access, inter.author)
        guild_id = access.get("guild_id")
        if not event_panels.allowed(guild_id, access.get("tier"), _profile()):
            await inter.edit_original_response(content="Доступно офицерам АС в тестовой версии.")
            return
        panel = await asyncio.to_thread(event_panels.current, "discord", guild_id, "tw")
        if (not panel or (panel["token"], panel["channel"]) != (token, str(inter.channel_id))
                or (zone is None and panel["message"] != str(inter.message.id))):
            await inter.edit_original_response(content="Эта панель устарела.", view=None)
            return
        log = await asyncio.to_thread(tw_battle_log.latest, guild_id)
        zones = tw_battle_log.zone_options(log)
        zone = tw_battle_log.selected_zone(log, zones, zone)
        rows = await asyncio.to_thread(tw_battle_log.display_rows, log, zone=zone)
        lines = [f"**{disnake.utils.escape_markdown(zones[zone][:100])} · Журнал боёв**"]
        if log:
            lines.append(f"Снимок журнала {event_panels._clock(log.get('at'))} МСК")
        if not rows:
            lines.append("В этой зоне пока нет записей об атаках.")
        else:
            lines.append("**Начало (МСК) · Атакующий → Защитник · Исход**")
        page_size = 5
        last_page = max(0, (len(rows) - 1) // page_size)
        page = min(max(page, 0), last_page)
        for row in rows[page * page_size:(page + 1) * page_size]:
            attacker, defender, leader = (disnake.utils.escape_markdown(" ".join(row[key].split())[:38])
                                          for key in ("attacker", "defender", "leader"))
            target = f"{defender} ({leader})" if leader else defender
            start = event_panels._clock(row["started_at"]) if row["started_at"] is not None else "—"
            result = row["result"]
            if row.get("finished_at") is not None:
                result += f" ({event_panels._clock(row['finished_at'])})"
            lines.append(f"{start} · {attacker} → {target} · {result}")
        view = disnake.ui.View(timeout=None)
        view.add_item(disnake.ui.StringSelect(
            placeholder="Выберите зону", custom_id=f"glp:tw:{token}:battle-zone",
            options=[disnake.SelectOption(label=label[:100], value=key, default=key == zone)
                     for key, label in list(zones.items())[:25]],
        ))
        if last_page:
            lines.append(f"Страница {page + 1} из {last_page + 1}")
            for label, index in (("Назад", page - 1), ("Дальше", page + 1)):
                view.add_item(disnake.ui.Button(label=label, disabled=not 0 <= index <= last_page,
                                               custom_id=f"glp:tw:{token}:battle-page|{zone}|{index}"))
        query = urlencode({"battle_zone": zone})
        lines.append(f"[Журнал зоны на сайте]({event_panels.SITE}/game-account/events/tw?{query}#battle-log)")
        await inter.edit_original_response(content="\n".join(lines), view=view,
                                           allowed_mentions=disnake.AllowedMentions.none())

    async def _personal_assignments(self, inter, kind, token):
        access = await asyncio.to_thread(guild_resolver.resolve_access, inter.author)
        guild_id = access.get("guild_id")
        if not event_panels.personal_allowed(guild_id, access.get("tier"), _profile()):
            await inter.edit_original_response(content="Доступно участникам АС в тестовой версии.")
            return
        panel = await asyncio.to_thread(event_panels.current, "discord", guild_id, kind)
        if not panel or (panel["token"], panel["channel"], panel["message"]) != (
                token, str(inter.channel_id), str(inter.message.id)):
            await inter.edit_original_response(content="Эта панель устарела.")
            return
        try:
            owner = str(inter.author.id)
            code = await asyncio.to_thread(game_account.select_account, owner, guild_id)
            lines = await asyncio.to_thread(event_panels.discord_details, guild_id, owner, code, kind, "mine")
            title = "Моя защита" if kind == "tw" else "Мои взводы"
            chunks = [f"**{title}**"]
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
                image_edit = {}
                tw_zones = kind == "tw" and action == "details"
                raid_details = kind == "raid" and action == "details"
                if tw_zones or raid_details:
                    filename = "tw-zone.png" if tw_zones else "raid-players.png"
                    image_edit["attachments"] = [item for item in getattr(message, "attachments", ())
                                                 if item.filename != filename]
                if action in expanded and not advance:
                    expanded.remove(action)
                else:
                    owner = str(inter.author.id)
                    code = await asyncio.to_thread(game_account.select_account, owner, guild_id)
                    title = dict((key, label) for label, key in event_panels.BUTTONS[kind])[action]
                    if action == "mine":
                        title += f" · {inter.author.display_name}"
                    pages = []
                    if tw_zones:
                        from services import tw_live_image

                        snapshot = await asyncio.to_thread(game_event_live.snapshot, guild_id, owner, "tw")
                        pages = tw_live_image.zone_pages(snapshot.get("data") or {})
                    if pages:
                        previous = next((e for e in message.embeds if e.footer.text == embed_marker), None)
                        page = int(previous.author.name.split(" / ")[0]) % len(pages) if (
                            advance and previous and previous.author.name) else 0
                        title, note, groups = pages[page]
                        embed = disnake.Embed(title=title, description=note or None)
                        embed.set_author(name=f"{page + 1} / {len(pages)}")
                        if groups:
                            png = await asyncio.to_thread(tw_live_image.render_zone, title, groups)
                            image_edit["file"] = disnake.File(BytesIO(png), filename="tw-zone.png")
                            embed.set_image(url="attachment://tw-zone.png")
                    elif raid_details:
                        from services.raid_live_image import player_card

                        snapshot = await asyncio.to_thread(game_event_live.latest_guild_snapshot, guild_id, kind)
                        data = snapshot.get("data")
                        rows = await asyncio.to_thread(event_panels.raid_players, guild_id, data)
                        if rows:
                            captured = event_panels._clock(snapshot["at"])
                            embed = disnake.Embed(title=title, description=f"Игроков в снимке: {len(rows)} · {captured} МСК")
                            png = await asyncio.to_thread(player_card, rows)
                            image_edit["file"] = disnake.File(BytesIO(png), filename=filename)
                            embed.set_image(url=f"attachment://{filename}")
                        else:
                            description = "Снимка нет. Нажмите «Обновить» на сводке."
                            if data:
                                description = ("В снимке рейда пока нет результатов по игрокам."
                                               if data["active"] else "Активного рейда нет.")
                            embed = disnake.Embed(title=title, description=description)
                    else:
                        lines = await asyncio.to_thread(event_panels.discord_details, guild_id, owner, code, kind, action)
                        description = "\n".join(lines)
                        if len(description) > 2700:
                            description = description[:2699].rsplit("\n", 1)[0] + "\n…"
                        embed = disnake.Embed(title=title[:256], description=description)
                    embed.set_footer(text=embed_marker)
                    embeds.append(embed)
                    if action not in expanded:
                        expanded.append(action)
                await message.edit(embeds=embeds, view=_view(kind, token, expanded),
                                   allowed_mentions=disnake.AllowedMentions.none(), **image_edit)
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
