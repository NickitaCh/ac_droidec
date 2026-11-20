"""Private reports for the player's current Grand Arena and Conquest."""

import asyncio

import disnake
from disnake.ext import commands

import database
import guild_resolver
from services import arena_map_image, bot_profiles, game_account, game_account_readonly, game_personal_modes


_TITLES = {"arena": "Великая арена", "conquest": "Завоевание"}


def _profile():
    return bot_profiles.worker_profile() or bot_profiles.runtime_profile()


def _scope(author):
    access = guild_resolver.resolve_access(author)
    args = (str(author.id), access.get("guild_id"), _profile(), access.get("tier"))
    game_account.require_account_read(*args)
    return args


async def _accounts(inter, query):
    try:
        owner, guild_id, _, _ = await asyncio.to_thread(_scope, inter.author)
    except game_account.AccountError:
        return {}
    accounts = await asyncio.to_thread(database.get_user_registrations, owner, guild_id=guild_id)
    labels = [(f"{name or code} · {'основной' if main else 'альт'} · {code}", str(code))
              for code, name, main in accounts]
    return dict([(label[:100], code) for label, code in labels if query.casefold() in label.casefold()][:25])


def _fields(embed, fields):
    for label, value in fields:
        if value is not None:
            embed.add_field(name=label, value=str(value), inline=True)


def _arena(embed, state):
    embed.description = "Вы участвуете в событии." if state["joined"] else "Вы не записаны на событие."
    _fields(embed, (("Рейтинг", state["rating"]), ("Лига", state["league"]), ("Дивизион", state["division"])))
    current = next((match for match in state["matches"] if match["current"]), None)
    if current:
        _fields(embed, (
            ("Матч", current["number"]), ("Соперник", current["opponent"] or None),
            ("Раунд", current["round"]), ("Конец раунда", current["round_end"]),
        ))
        if current["defense"] is not None:
            placed, capacity = current["defense"]
            _fields(embed, (("Выставлено в защиту", f"{placed} из {capacity}"),))
        for side, heading in (("home", "Моя защита"), ("away", "Зоны атаки")):
            zones = current[side]
            if zones:
                counts = []
                for index, zone in enumerate(zones, 1):
                    summary = game_personal_modes.arena_zone_summary(zone)
                    remaining = summary["remaining"]
                    amount = "—" if remaining is None else str(remaining)
                    capacity = "—" if zone["capacity"] is None else str(zone["capacity"])
                    counts.append(f"Зона {index} · осталось {amount} из {capacity}")
                embed.add_field(name=heading, value="\n".join(counts), inline=True)
    _fields(embed, (("Конец текущего матча", state["match_end"]),))


def _conquest(embed, state):
    energy = state["energy"]
    _fields(embed, (
        ("Сложность", state["difficulty"]), ("Энергия", energy.get("Сейчас")),
        ("Следующая энергия через", energy.get("Следующая единица через")),
        ("Диски", f'{state["equipped_disks"]} установлено · {state["available_disks"]} свободно'),
        ("Пропуск", "Активен" if state["pass_active"] else "Не активен"),
    ))
    if state["sectors"]:
        sectors = [f'{row["id"]}: {row["completed"]} из {row["nodes"]}' for row in state["sectors"]]
        embed.add_field(name="Узлы с прогрессом по секторам", value="\n".join(sectors[:12])[:1024], inline=False)
    if state["stamina"]:
        lowest = state["stamina"][:8]
        names = database.get_game_unit_names([row["unit"] for row in lowest])
        lines = [f'{names.get(row["unit"], row["unit"])} · {row["value"]}' for row in lowest]
        embed.add_field(name="Самая низкая выносливость", value="\n".join(lines)[:1024], inline=False)


class PersonalGameModesCog(commands.Cog):
    @commands.slash_command(name="ва", description="Показать текущую Великую арену своего аккаунта")
    async def arena(self, inter: disnake.ApplicationCommandInteraction,
                    аккаунт: str = commands.Param(default=None, autocomplete=_accounts, description="Свой основной аккаунт или альт")):
        await self._report(inter, "arena", аккаунт)

    @commands.slash_command(name="завоевание", description="Показать текущее Завоевание своего аккаунта")
    async def conquest(self, inter: disnake.ApplicationCommandInteraction,
                       аккаунт: str = commands.Param(default=None, autocomplete=_accounts, description="Свой основной аккаунт или альт")):
        await self._report(inter, "conquest", аккаунт)

    async def _report(self, inter, mode, account, *, reconnect=False):
        if not inter.response.is_done():
            await inter.response.defer(ephemeral=True)
        try:
            args = await asyncio.to_thread(_scope, inter.author)
            code = await asyncio.to_thread(game_account.select_account, args[0], args[1], ally_code=account)
            if reconnect:
                await game_account.reconnect(*args, code, optimizer_read=True)
            state = await game_account_readonly.read(*args, code, event=mode, member_read=True)
        except game_account.ConnectionRequired as error:
            expired = isinstance(error, game_account.SessionExpired) and not reconnect
            if expired:
                view = PersonalReportView(self, inter.author.id, mode, code, reconnect=True)
            else:
                path = await asyncio.to_thread(game_account.link_path, args[0], args[1], code)
                view = disnake.ui.View()
                view.add_item(disnake.ui.Button(style=disnake.ButtonStyle.link, label="Подключить через EA",
                                                url=f"https://swgoh-sng.ru{path}"))
            await inter.edit_original_response(
                content=game_account.LOGIN_WARNING if expired else "Подключите игровой аккаунт для просмотра.",
                embed=None, view=view, attachments=[],
            )
            return
        except game_account.AccountError as error:
            await inter.edit_original_response(content=str(error), embed=None, view=None, attachments=[])
            return
        if state is None:
            await inter.edit_original_response(content="Игра не вернула активное событие для этого аккаунта.",
                                               embed=None, view=PersonalReportView(self, inter.author.id, mode, code),
                                               attachments=[])
            return
        embed = disnake.Embed(title=f"{_TITLES[mode]} · {code}")
        if mode == "arena":
            await asyncio.to_thread(game_personal_modes.name_arena_units, state)
            _arena(embed, state)
        else:
            await asyncio.to_thread(_conquest, embed, state)
        match = next((item for item in state["matches"] if item["current"]), None) if mode == "arena" else None
        view = PersonalReportView(self, inter.author.id, mode, code, match=match)
        board = await asyncio.to_thread(arena_map_image.render, match) if match else None
        if board:
            embed.set_image(url="attachment://arena-map.png")
            await inter.edit_original_response(content=None, embed=embed, view=view, attachments=[],
                                               file=disnake.File(board, filename="arena-map.png"),
                                               allowed_mentions=disnake.AllowedMentions.none())
        else:
            await inter.edit_original_response(content=None, embed=embed, view=view, attachments=[],
                                               allowed_mentions=disnake.AllowedMentions.none())


class PersonalReportView(disnake.ui.View):
    def __init__(self, cog, owner, mode, code, *, match=None, reconnect=False):
        super().__init__(timeout=600)
        self.owner = owner
        button = disnake.ui.Button(label="Войти и обновить" if reconnect else "Обновить",
                                   style=disnake.ButtonStyle.primary)

        async def refresh(inter):
            await inter.response.defer(with_message=False)
            await cog._report(inter, mode, code, reconnect=reconnect)

        button.callback = refresh
        self.add_item(button)
        if match:
            for side, label in (("home", "Моя защита"), ("away", "Зоны атаки")):
                detail = disnake.ui.Button(label=label, style=disnake.ButtonStyle.secondary)

                async def show(inter, side=side, label=label):
                    zones = match[side]
                    if not zones:
                        await inter.response.send_message("Игра пока не раскрыла эти зоны.", ephemeral=True)
                        return
                    await inter.response.defer(ephemeral=True)
                    for index, zone in enumerate(zones, 1):
                        lines = [f"**{label} · зона {index}**"]
                        summary = game_personal_modes.arena_zone_summary(zone)
                        if summary["remaining"] is not None:
                            lines.append(f"Осталось {summary['remaining']} отрядов")
                        if zone["capacity"] is not None and zone["placed"] is not None:
                            lines.append(f"Выставлено {zone['placed']} из {zone['capacity']}")
                        if zone["defeated"] is not None:
                            lines.append(f"Побеждено {zone['defeated']}")
                        for team in zone["teams"]:
                            line = " · ".join(team["names"]) or "Состав скрыт игрой"
                            status = game_personal_modes.arena_team_status(team)
                            icon = {1: "🛡️", 2: "⚔️", 3: "❌"}.get(status["state"], "❔")
                            line = f"{icon} {status['label']} · {line}"
                            lines.append(line)
                        if not zone["teams"]:
                            lines.append("Игра не передала составы." if zone["placed"] or side == "away" else "Пусто.")
                        text = ""
                        for line in lines:
                            if len(text) + len(line) + 1 > 1900:
                                await inter.followup.send(text, ephemeral=True, allowed_mentions=disnake.AllowedMentions.none())
                                text = ""
                            text += line + "\n"
                        await inter.followup.send(text, ephemeral=True, allowed_mentions=disnake.AllowedMentions.none())

                detail.callback = show
                self.add_item(detail)

    async def interaction_check(self, inter):
        return inter.author.id == self.owner


def setup(bot):
    bot.add_cog(PersonalGameModesCog())
