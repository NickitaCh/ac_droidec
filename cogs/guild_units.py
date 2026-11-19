"""Гильдия по одному юниту и готовность к «Руководству» — по кэшу автообновления:
/юнит_гильдия (как C3PO `g unit`) и /готовность (как WookieeBot `guild ready journey`);
/фракция — персонажи фракции у игрока (как HotBot `/faction`, services/faction.py).
Логика — services/guild_units.py, картинка готовности — services/guild_units_image.py.
Данные — публичный ростер гильдии, поэтому обе команды открыты всем участникам."""

from services.discord_options import yes_no_param
import asyncio
import io
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

import disnake
from disnake.ext import commands

import database
import guild_resolver
import stat_engine
from cogs.unit_forecast import _parse_bracket_id, autocomplete_character_unit
from services import faction as faction_svc
from services import faction_image, guild_units, guild_units_image, journey_guide, unit_aliases
from services import public_checks
from services import bot_profiles, game_account, game_account_readonly, unit_shards

log = logging.getLogger(__name__)

FIELD_LIMIT = 1024  # лимит Discord на значение поля эмбеда
MESSAGE_EMBED_LIMIT = 5800  # лимит Discord — 6000 символов на все эмбеды одного сообщения


async def autocomplete_factions(inter: disnake.ApplicationCommandInteraction, string: str):
    return [disnake.OptionChoice(name=f"{name} ({count})"[:100], value=cid) for cid, name, count in faction_svc.search(string)]


async def autocomplete_guides(inter: disnake.ApplicationCommandInteraction, string: str):
    needle = string.strip().lower()
    by_alias = unit_aliases.matching(string)
    options = sorted(journey_guide.guide_options(), key=lambda g: g["base_id"] not in by_alias)
    out = []
    for g in options:
        label = f"{g['name']} · {g['kind_label']}"
        if needle in label.lower() or g["base_id"] in by_alias:
            out.append(disnake.OptionChoice(name=label[:100], value=g["base_id"]))
        if len(out) >= 25:
            break
    return out


def _gp(n) -> str:
    return f"{n / 1000:.1f}K" if n else "—"


def _owner_line(o: dict, is_ship: bool) -> str:
    level = guild_units.level_text(o, is_ship)
    extra = "" if is_ship else f"{o['zetas']}/{o['omicrons']}" + ("·А" if o["ultimate"] else "")
    return f"{_gp(o['gp']):>6} : {level:<6} : {extra:<6} : {o['name'][:18]}"


class GuildUnitsCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.slash_command(name="юнит_гильдия", description="У кого в гильдии есть юнит и на какой прокачке (как C3PO g unit)")
    async def guild_unit(
        self,
        inter: disnake.ApplicationCommandInteraction,
        юнит: str = commands.Param(description="Персонаж или корабль 🚀", autocomplete=autocomplete_character_unit),
    ):
        await inter.response.defer()
        guild_id = guild_resolver.resolve_guild_id(inter.author)
        base_id = _parse_bracket_id(юнит)
        if not database.get_game_unit_name(base_id):
            await inter.edit_original_response("❌ Юнит не найден — выберите его из списка.")
            return
        if not await stat_engine.wait_stat_calc(self.bot, inter):
            await inter.edit_original_response("⏳ Калькулятор статов не загрузился — попробуйте через пару минут.")
            return
        try:
            if guild_id:
                r = await asyncio.to_thread(guild_units.unit_report, guild_id, base_id, self.bot.stat_calc)
            else:
                r = await guild_units.public_unit_report(self.bot.comlink, inter.author, base_id, self.bot.stat_calc)
        except ValueError as exc:
            await inter.edit_original_response(str(exc))
            return
        if not r["members"]:
            await inter.edit_original_response("❌ Нет данных по составу гильдии — дождитесь автообновления.")
            return

        cfg = database.get_guild_config(guild_id) if guild_id else {}
        cfg = dict(cfg or {}, name=r.get("guild_name") or (cfg or {}).get("name"))
        head = " ГМ    : ур.    : " + ("" if r["is_ship"] else "з/о    : ") + "Игрок"
        groups: dict[int, list] = {}
        for o in r["owners"]:
            groups.setdefault(o["stars"], []).append(o)
        blocks = []
        for stars in sorted(groups, reverse=True):
            rows = groups[stars]
            # Поле эмбеда — не длиннее 1024 символов вместе с ```: режем по длине, не по строкам.
            chunk, first = [head], True
            for o in rows:
                line = _owner_line(o, r["is_ship"])
                if len("\n".join(chunk + [line])) + 8 > FIELD_LIMIT:
                    blocks.append((f"{stars}★ ({len(rows)})" if first else f"{stars}★ (продолжение)", "```\n" + "\n".join(chunk) + "\n```"))
                    chunk, first = [head], False
                chunk.append(line)
            blocks.append((f"{stars}★ ({len(rows)})" if first else f"{stars}★ (продолжение)", "```\n" + "\n".join(chunk) + "\n```"))
        if r["missing"]:
            blocks.append((f"Нет у ({len(r['missing'])})", ", ".join(r["missing"])[:1024]))

        embeds, embed = [], None
        for name, value in blocks:
            if embed is None or len(embed.fields) >= 4 or len(embed) + len(value) > 5500:
                embed = disnake.Embed(
                    title=f"{cfg.get('name') or 'Гильдия'} ({r['members']})",
                    description=f"**{r['name']}** — есть у {len(r['owners'])} из {r['members']}"
                    + ("" if r["is_ship"] else " · з/о — зеты/омикроны, А — абсолютная способность"),
                    color=disnake.Color.gold(),
                )
                embeds.append(embed)
            embed.add_field(name=name, value=value, inline=False)
        if not embeds:
            embeds = [disnake.Embed(title=cfg.get("name") or "Гильдия", description=f"**{r['name']}** — ни у кого нет.")]
        embeds[-1].set_footer(text=r.get("note") or "Данные автообновления составов")
        # В одном сообщении — не больше 10 эмбедов и 6000 символов на все; остальное — следующими сообщениями.
        messages, current = [], []
        for e in embeds:
            if current and (len(current) >= 10 or sum(len(x) for x in current) + len(e) > MESSAGE_EMBED_LIMIT):
                messages.append(current)
                current = []
            current.append(e)
        messages.append(current)
        await inter.edit_original_response(embeds=messages[0])
        for chunk in messages[1:]:
            await inter.followup.send(embeds=chunk)

    @commands.slash_command(name="готовность", description="Готовность к цели «Руководства» картинкой: игрока (как C3PO panic) или гильдии")
    async def readiness(
        self,
        inter: disnake.ApplicationCommandInteraction,
        цель: str = commands.Param(description="ГЛ, флагман или персонаж из «Руководства»", autocomplete=autocomplete_guides),
        участник: disnake.User = commands.Param(default=None, description=guild_resolver.TARGET_USER_DESCRIPTION),
        аликод: str = commands.Param(default=None, description=guild_resolver.TARGET_CODE_DESCRIPTION),
        альт: bool = yes_no_param(default=False, description=guild_resolver.ALT_PARAM_DESCRIPTION),
        гильдия: bool = yes_no_param(default=False, description="Только подключённая гильдия: вся гильдия одной таблицей"),
        осколки: bool = yes_no_param(default=False, description="Показать осколки до следующей звезды — только свой аккаунт, требуется EA"),
    ):
        with_shards = осколки is True
        await inter.response.defer(ephemeral=with_shards)
        guild_id = guild_resolver.resolve_guild_id(inter.author)
        context = None
        if with_shards:
            if гильдия or (участник and участник.id != inter.author.id):
                await inter.edit_original_response("❌ Осколки доступны только для своего аккаунта, без режима «гильдия».")
                return
            try:
                owner = str(inter.author.id)
                profile = bot_profiles.worker_profile() or bot_profiles.runtime_profile()
                game_account.require_account_read(owner, guild_id, profile)
                code, _, error = guild_resolver.target_player(участник, аликод, guild_id)
                if error:
                    raise game_account.AccountError(error)
                if альт and (участник or аликод):
                    raise game_account.AccountError("❌ «альт» нельзя указывать вместе с другим игроком.")
                if code is None:
                    account = public_checks.account(inter.author, alt=альт)
                    if not account:
                        raise game_account.AccountError("Зарегистрируйте свой аккаунт через /регистрация.")
                    code = account["ally_code"]
                code = game_account.select_account(owner, guild_id, ally_code=code)
                context = (owner, guild_id, profile, code)
            except game_account.AccountError as exc:
                await inter.edit_original_response(str(exc))
                return
        if гильдия and guild_id is None:
            await inter.edit_original_response("❌ Отчёт по гильдии доступен участникам подключённых гильдий. Личная проверка — без параметра «гильдия».")
            return
        if not database.get_journey_guide_requirements(цель, guild_id):
            await inter.edit_original_response("❌ Цель не найдена или у неё нет требований — выберите её из списка.")
            return
        if гильдия:
            await self._guild_readiness(inter, guild_id, цель)
            return
        got = await self._player_roster(inter, guild_id, участник, аликод, альт)
        if got is None:
            return
        player, roster, note = got
        await self._send_readiness(inter, guild_id, цель, player, roster, note, context)

    async def _send_readiness(self, inter, guild_id, goal, player, roster, note, context=None):
        if context:
            owner, guild_id, profile, code = context
            try:
                snapshot = await game_account_readonly.read(owner, guild_id, profile, None, code, member_read=True)
                recipes = await unit_shards.catalog(self.bot.comlink)
                roster = unit_shards.private_roster(snapshot)
                stamp = datetime.fromtimestamp(snapshot["read_at"], ZoneInfo("Europe/Moscow"))
                note = f"Данные игрового аккаунта: {stamp:%d.%m.%Y %H:%M} МСК"
            except game_account.SessionExpired:
                await inter.edit_original_response(content=game_account.LOGIN_WARNING,
                    view=ReadinessSessionView(self, context, goal, player))
                return
            except game_account.ConnectionRequired:
                await self._offer_connection(inter, context)
                return
            except game_account.AccountError as exc:
                await inter.edit_original_response(content=str(exc), view=None)
                return
            except Exception as exc:
                log.warning("Readiness shard read failed (%s)", type(exc).__name__)
                await inter.edit_original_response(content="Не удалось прочитать осколки. Попробуйте позже.", view=None)
                return
        ally_code = player.get("allyCode")
        r = await asyncio.to_thread(guild_units.player_readiness, guild_id, goal, roster)
        if context:
            unit_shards.add_progress(r, snapshot, recipes)
        png = await asyncio.to_thread(guild_units_image.render_player, r, player.get("name") or str(ally_code), note)
        met = sum(1 for u in r["units"] if u["status"] == "met")
        await inter.edit_original_response(
            content=f"**{player.get('name')}** — {r['guide']['name']}: выполнено {met} из {len(r['units'])}",
            file=disnake.File(io.BytesIO(png), filename=f"ready_{goal}_{ally_code}.png"),
            view=None,
        )

    async def _offer_connection(self, inter, context):
        owner, guild_id, _profile, code = context
        path = game_account.link_path(owner, guild_id, code)
        view = disnake.ui.View()
        view.add_item(disnake.ui.Button(style=disnake.ButtonStyle.link, label="Подключить через EA",
                                       url=f"https://swgoh-sng.ru{path}"))
        await inter.edit_original_response(content="Подключите свой игровой аккаунт через EA для чтения осколков.", view=view)

    async def _player_roster(self, inter, guild_id: int, участник, аликод, альт):
        """(player, {base_id: rosterUnit}, подпись о свежести) выбранного игрока или None —
        тогда ошибка уже отправлена. Без игрока — вы, «альт» — ваш второй аккаунт."""
        ally_code, _name, error = guild_resolver.target_player(участник, аликод, guild_id)
        if error:
            await inter.edit_original_response(error)
            return None
        if альт and (участник or аликод):
            await inter.edit_original_response("❌ «альт» нельзя указывать вместе с другим игроком.")
            return None
        if ally_code is None:
            account = public_checks.account(inter.author, alt=альт)
            if not account:
                await inter.edit_original_response("❌ Вы не зарегистрированы — используйте `/регистрация`, укажите участника или код союзника.")
                return None
            ally_code = account["ally_code"]

        card_cog = self.bot.get_cog("UnitForecastCog")
        players, ages = await card_cog._card_players([str(ally_code)], guild_id, False)
        player = players[0]
        if not player or not player.get("rosterUnit"):
            await inter.edit_original_response(f"❌ Не удалось получить игрока с кодом {ally_code} из игры.")
            return None
        player.setdefault("allyCode", str(ally_code))
        roster = {guild_units.unit_base_id(u): u for u in player["rosterUnit"]}
        note = (f"данные автообновления, {max(1, int(ages[0]) // 60)} мин назад" if ages[0] is not None
                else "данные из игры на момент запроса")
        return player, roster, note

    @commands.slash_command(name="фракция", description="Все персонажи фракции у игрока картинкой (как HotBot /faction)")
    async def faction(
        self,
        inter: disnake.ApplicationCommandInteraction,
        фракция: str = commands.Param(description="Принадлежность, профессия или вид", autocomplete=autocomplete_factions),
        участник: disnake.User = commands.Param(default=None, description=guild_resolver.TARGET_USER_DESCRIPTION),
        аликод: str = commands.Param(default=None, description=guild_resolver.TARGET_CODE_DESCRIPTION),
        альт: bool = yes_no_param(default=False, description=guild_resolver.ALT_PARAM_DESCRIPTION),
    ):
        await inter.response.defer()
        guild_id = guild_resolver.resolve_guild_id(inter.author)
        faction_id = faction_svc.resolve(фракция)
        if faction_id is None:
            await inter.edit_original_response("❌ Фракция не найдена — выберите её из списка.")
            return
        got = await self._player_roster(inter, guild_id, участник, аликод, альт)
        if got is None:
            return
        player, roster, note = got
        r = await asyncio.to_thread(faction_svc.build, faction_id, roster)
        name = player.get("name") or str(player.get("allyCode"))
        png = await asyncio.to_thread(faction_image.render, r, name, note)
        await inter.edit_original_response(
            content=f"**{name}** — {r['name']}: открыто {r['owned']} из {r['total']}",
            file=disnake.File(io.BytesIO(png), filename=f"faction_{faction_id}.png"),
        )

    async def _guild_readiness(self, inter, guild_id: int, guide: str):
        r = await asyncio.to_thread(guild_units.readiness, guild_id, guide)
        if not r["players"]:
            await inter.edit_original_response("❌ Нет данных по ростерам гильдии — дождитесь автообновления.")
            return
        cfg = database.get_guild_config(guild_id) or {}
        png = await asyncio.to_thread(guild_units_image.render, r, cfg.get("name") or "Гильдия", "данные автообновления")
        ready = sum(1 for p in r["players"] if p["ready"])
        await inter.edit_original_response(
            content=f"**{r['guide']['name']}** — готовы {ready} из {len(r['players'])}",
            file=disnake.File(io.BytesIO(png), filename=f"ready_{guide}.png"),
        )


class ReadinessSessionView(disnake.ui.View):
    def __init__(self, cog, context, goal, player):
        super().__init__(timeout=600)
        self.cog, self.context, self.goal, self.player = cog, context, goal, player

    async def interaction_check(self, inter):
        if str(inter.author.id) == self.context[0]:
            return True
        await inter.response.send_message("Это подтверждение для автора команды.", ephemeral=True)
        return False

    @disnake.ui.button(label="Войти и показать осколки", style=disnake.ButtonStyle.success)
    async def confirm(self, button, inter):
        await inter.response.defer(with_message=False)
        if self.is_finished():
            return
        self.stop()
        owner, guild_id, profile, code = self.context
        try:
            await inter.edit_original_response(content="Вхожу и читаю осколки…", view=None)
            await game_account.reconnect(owner, guild_id, profile, None, code, optimizer_read=True)
            await self.cog._send_readiness(inter, guild_id, self.goal, self.player, {}, "", self.context)
        except game_account.ConnectionRequired:
            await self.cog._offer_connection(inter, self.context)
        except game_account.AccountError as exc:
            await inter.edit_original_response(content=str(exc), view=None)
        except Exception as exc:
            log.warning("Readiness session recovery failed (%s)", type(exc).__name__)
            await inter.edit_original_response(content="Не удалось войти в игровой аккаунт. Попробуйте позже.", view=None)

    @disnake.ui.button(label="Отмена", style=disnake.ButtonStyle.secondary)
    async def cancel(self, button, inter):
        await inter.response.defer(with_message=False)
        if self.is_finished():
            return
        self.stop()
        await inter.edit_original_response(content="Чтение осколков отменено.", view=None)


def setup(bot):
    bot.add_cog(GuildUnitsCog(bot))
