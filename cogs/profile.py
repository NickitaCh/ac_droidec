"""/профиль — профиль игрока или гильдии картинкой, как HotBot /profile (guild=true).

Игрок — свежий get_player (любой код союзника, даже не из гильдии); гильдия — по кэшам
ежечасного синка, без запросов по каждому участнику. Расчёт — services/player_profile.py,
картинка — services/profile_image.py. Доступно всем участникам (решение 2026-10-19)."""

import asyncio
import io

import disnake
from disnake.ext import commands

import database
import stat_engine
import guild_resolver
from cogs.violations import autocomplete_players
from services import player_profile as pp
from services import profile_image


def _guild_payload(resp: dict) -> dict:
    return (resp or {}).get("guild", resp or {})


class ProfileCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    async def _guild_gp(self, swgoh_guild_id: str) -> int | None:
        if not swgoh_guild_id:
            return None
        try:
            resp = await asyncio.to_thread(self.bot.comlink.get_guild, swgoh_guild_id)
            return int(_guild_payload(resp).get("profile", {}).get("guildGalacticPower") or 0) or None
        except Exception as e:
            print(f"⚠️ [Профиль] ГМ гильдии {swgoh_guild_id}: {e!r}")
            return None

    @commands.slash_command(name="профиль", description="Профиль игрока или гильдии картинкой: ВА, моды, реликвии, ДК, ГЛ и мета")
    async def profile(
        self,
        inter: disnake.ApplicationCommandInteraction,
        игрок: str = commands.Param(default=None, description="Пусто → вы (/регистрация)", autocomplete=autocomplete_players),
        аликод: str = commands.Param(default=None, description="Код союзника любого игрока, в том числе не из гильдии"),
        гильдия: bool = commands.Param(default=False, description="Сводка по всей гильдии: итоги, средний игрок, средние статы ГЛ и меты"),
    ):
        await inter.response.defer()
        guild_id = await guild_resolver.require_guild_id(inter)
        if guild_id is None:
            return
        if not await stat_engine.wait_stat_calc(self.bot, inter):
            await inter.edit_original_response("⏳ Калькулятор статов не загрузился — попробуйте через пару минут.")
            return
        ctx = await asyncio.to_thread(pp.load_context)
        if гильдия:
            await self._guild_profile(inter, guild_id, ctx)
            return

        if аликод is not None:
            if игрок is not None:
                await inter.edit_original_response("❌ Укажите либо игрока из списка, либо код союзника — не оба сразу.")
                return
            ally_code = guild_resolver.normalize_ally_code(аликод)
            if ally_code is None:
                await inter.edit_original_response("❌ Код союзника должен состоять из 9 цифр.")
                return
        elif игрок is None:
            registration = database.get_user_registration(str(inter.author.id), guild_id=guild_id)
            if not registration:
                await inter.edit_original_response(
                    "❌ Игрок не указан, а вы не зарегистрированы — используйте `/регистрация`, укажите игрока или код союзника."
                )
                return
            ally_code = registration[0]
        else:
            ally_code = self.bot.guild_roster_caches.get(guild_id, {}).get(игрок)
            if not ally_code:
                await inter.edit_original_response("❌ Игрок не найден в составе гильдии.")
                return

        try:
            player = await asyncio.to_thread(self.bot.comlink.get_player, allycode=str(ally_code))
        except Exception as e:
            print(f"⚠️ [Профиль] get_player {ally_code}: {e!r}")
            player = None
        if not player or not player.get("rosterUnit"):
            await inter.edit_original_response(f"❌ Не удалось получить игрока с кодом {ally_code} из игры.")
            return
        meta = pp.player_meta(player)
        guild_gp = await self._guild_gp(player.get("guildId"))

        def work():
            summary = pp.summarize(player["rosterUnit"], player.get("datacron") or [], meta, self.bot.stat_calc, ctx)
            return profile_image.render_player(summary, guild_gp)

        png = await asyncio.to_thread(work)
        await inter.edit_original_response(file=disnake.File(io.BytesIO(png), filename=f"profile_{ally_code}.png"))

    async def _guild_profile(self, inter, guild_id: int, ctx: dict):
        cfg = database.get_guild_config(guild_id) or {}
        codes = [code for code, _name in database.get_roster_by_player_id(guild_id).values() if code]
        units = await asyncio.to_thread(database.get_player_units_bulk, codes)
        datacrons = await asyncio.to_thread(database.get_player_datacrons_bulk, codes)
        profiles = await asyncio.to_thread(database.get_player_profiles_bulk, codes)
        names = database.get_player_names(guild_id)
        if not any(units.get(c) for c in codes):
            await inter.edit_original_response("❌ Нет данных по ростерам гильдии — дождитесь ежечасного обновления.")
            return
        guild_gp = await self._guild_gp(cfg.get("swgoh_guild_id"))

        def work():
            players = []
            for code in codes:
                roster = units.get(code)
                if not roster:
                    continue
                meta = profiles.get(code) or {"name": names.get(code, code), "ally_code": code, "stats": {}}
                players.append(pp.summarize(list(roster.values()), datacrons.get(code) or [], meta, self.bot.stat_calc, ctx))
            summary = pp.summarize_guild(cfg.get("name") or "Гильдия", guild_gp, players, ctx)
            return profile_image.render_guild(summary)

        png = await asyncio.to_thread(work)
        await inter.edit_original_response(file=disnake.File(io.BytesIO(png), filename=f"guild_profile_{guild_id}.png"))


def setup(bot):
    bot.add_cog(ProfileCog(bot))
