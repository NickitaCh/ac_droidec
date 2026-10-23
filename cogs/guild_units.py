"""Гильдия по одному юниту и готовность к «Руководству» — по кэшу ежечасного синка:
/юнит_гильдия (как C3PO `g unit`) и /готовность (как WookieeBot `guild ready journey`);
/фракция — персонажи фракции у игрока (как HotBot `/faction`, services/faction.py).
Логика — services/guild_units.py, картинка готовности — services/guild_units_image.py.
Данные — публичный ростер гильдии, поэтому обе команды открыты всем участникам."""

import asyncio
import io

import disnake
from disnake.ext import commands

import database
import guild_resolver
import stat_engine
from cogs.unit_forecast import _parse_bracket_id, autocomplete_character_unit
from services import faction as faction_svc
from services import faction_image, guild_units, guild_units_image, journey_guide, unit_aliases

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
        guild_id = await guild_resolver.require_guild_id(inter)
        if guild_id is None:
            return
        base_id = _parse_bracket_id(юнит)
        if not database.get_game_unit_name(base_id):
            await inter.edit_original_response("❌ Юнит не найден — выберите его из списка.")
            return
        if not await stat_engine.wait_stat_calc(self.bot, inter):
            await inter.edit_original_response("⏳ Калькулятор статов не загрузился — попробуйте через пару минут.")
            return
        r = await asyncio.to_thread(guild_units.unit_report, guild_id, base_id, self.bot.stat_calc)
        if not r["members"]:
            await inter.edit_original_response("❌ Нет данных по составу гильдии — дождитесь ежечасного обновления.")
            return

        cfg = database.get_guild_config(guild_id) or {}
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
        embeds[-1].set_footer(text="Данные ежечасного обновления составов")
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
        альт: bool = commands.Param(default=False, description=guild_resolver.ALT_PARAM_DESCRIPTION),
        гильдия: bool = commands.Param(default=False, description="Вся гильдия одной таблицей вместо одного игрока"),
    ):
        await inter.response.defer()
        guild_id = await guild_resolver.require_guild_id(inter)
        if guild_id is None:
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
        ally_code = player.get("allyCode")
        r = await asyncio.to_thread(guild_units.player_readiness, guild_id, цель, roster)
        png = await asyncio.to_thread(guild_units_image.render_player, r, player.get("name") or str(ally_code), note)
        met = sum(1 for u in r["units"] if u["status"] == "met")
        await inter.edit_original_response(
            content=f"**{player.get('name')}** — {r['guide']['name']}: выполнено {met} из {len(r['units'])}",
            file=disnake.File(io.BytesIO(png), filename=f"ready_{цель}_{ally_code}.png"),
        )

    async def _player_roster(self, inter, guild_id: int, участник, аликод, альт):
        """(player, {base_id: rosterUnit}, подпись о свежести) выбранного игрока или None —
        тогда ошибка уже отправлена. Без игрока — вы, «альт» — ваш второй аккаунт."""
        ally_code, _name, error = guild_resolver.target_player(участник, аликод, guild_id)
        if error:
            await inter.edit_original_response(error)
            return None
        if ally_code is None and альт:
            alt = await guild_resolver.require_alt(inter, "unit_forecast")
            if alt is None:
                return None
            ally_code = alt["ally_code"]
        elif ally_code is None:
            reg = database.get_user_registration(str(inter.author.id), guild_id=guild_id)
            if not reg:
                await inter.edit_original_response("❌ Вы не зарегистрированы — используйте `/регистрация`, укажите участника или код союзника.")
                return None
            ally_code = reg[0]

        card_cog = self.bot.get_cog("UnitForecastCog")
        players, ages = await card_cog._card_players([str(ally_code)], guild_id, False)
        player = players[0]
        if not player or not player.get("rosterUnit"):
            await inter.edit_original_response(f"❌ Не удалось получить игрока с кодом {ally_code} из игры.")
            return None
        player.setdefault("allyCode", str(ally_code))
        roster = {guild_units.unit_base_id(u): u for u in player["rosterUnit"]}
        note = (f"данные ежечасного обновления, {max(1, int(ages[0]) // 60)} мин назад" if ages[0] is not None
                else "данные из игры на момент запроса")
        return player, roster, note

    @commands.slash_command(name="фракция", description="Все персонажи фракции у игрока картинкой (как HotBot /faction)")
    async def faction(
        self,
        inter: disnake.ApplicationCommandInteraction,
        фракция: str = commands.Param(description="Принадлежность, профессия или вид", autocomplete=autocomplete_factions),
        участник: disnake.User = commands.Param(default=None, description=guild_resolver.TARGET_USER_DESCRIPTION),
        аликод: str = commands.Param(default=None, description=guild_resolver.TARGET_CODE_DESCRIPTION),
        альт: bool = commands.Param(default=False, description=guild_resolver.ALT_PARAM_DESCRIPTION),
    ):
        await inter.response.defer()
        guild_id = await guild_resolver.require_guild_id(inter)
        if guild_id is None:
            return
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
            await inter.edit_original_response("❌ Нет данных по ростерам гильдии — дождитесь ежечасного обновления.")
            return
        cfg = database.get_guild_config(guild_id) or {}
        png = await asyncio.to_thread(guild_units_image.render, r, cfg.get("name") or "Гильдия", "данные ежечасного обновления")
        ready = sum(1 for p in r["players"] if p["ready"])
        await inter.edit_original_response(
            content=f"**{r['guide']['name']}** — готовы {ready} из {len(r['players'])}",
            file=disnake.File(io.BytesIO(png), filename=f"ready_{guide}.png"),
        )


def setup(bot):
    bot.add_cog(GuildUnitsCog(bot))
