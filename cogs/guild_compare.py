"""/вг_соперник — своя гильдия против соперника картинкой (как HotBot `/compare guild` /
C3PO `tw enemy quality`). Соперник — из прошлых ВГ (подсказка) или по коду союзника
любого его участника; его ростеры берутся из игры прямо сейчас (запрос на каждого
участника), своя гильдия — из кэшей ежечасного синка. Расчёт — services/guild_compare.py
и services/player_profile.py, картинка — services/guild_compare_image.py."""

import asyncio
import io

import disnake
from disnake.ext import commands

import database
import guild_resolver
import stat_engine
from services import guild_compare as gc
from services import guild_compare_image
from services import player_profile as pp

PARALLEL = 4  # одновременных get_player к Comlink


def _guild_payload(resp: dict) -> dict:
    return (resp or {}).get("guild", resp or {})


async def autocomplete_opponents(inter: disnake.ApplicationCommandInteraction, string: str):
    guild_id = guild_resolver.resolve_guild_id(inter.author)
    if guild_id is None:
        return []
    out = []
    for o in gc.load_recent_opponents(guild_id):
        label = gc.opponent_label(o)
        if string.lower() in label.lower():
            out.append(disnake.OptionChoice(name=label[:100], value=o["id"]))
    return out[:25]


class GuildCompareCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.slash_command(name="вг_соперник", description="Своя гильдия против соперника: ГМ, реликвии, омикроны, модули, ГЛ и мета")
    async def compare_guild(
        self,
        inter: disnake.ApplicationCommandInteraction,
        соперник: str = commands.Param(default=None, description="Гильдия из прошлых ВГ", autocomplete=autocomplete_opponents),
        аликод: str = commands.Param(default=None, description="Код союзника любого участника гильдии-соперника"),
    ):
        await inter.response.defer()
        guild_id = await guild_resolver.require_feature(inter, "tw_order")
        if guild_id is None:
            return
        if bool(соперник) == bool(аликод):
            await inter.edit_original_response("❌ Укажите соперника: выберите гильдию из прошлых ВГ или код союзника любого её участника.")
            return
        if not await stat_engine.wait_stat_calc(self.bot, inter):
            await inter.edit_original_response("⏳ Калькулятор статов не загрузился — попробуйте через пару минут.")
            return

        opponent_id = соперник
        if аликод:
            ally_code = guild_resolver.normalize_ally_code(аликод)
            if ally_code is None:
                await inter.edit_original_response("❌ Код союзника должен состоять из 9 цифр.")
                return
            try:
                player = await asyncio.to_thread(self.bot.comlink.get_player, allycode=ally_code)
            except Exception as e:
                print(f"⚠️ [ВГ соперник] get_player {ally_code}: {e!r}")
                player = None
            opponent_id = (player or {}).get("guildId")
            if not opponent_id:
                await inter.edit_original_response(f"❌ Игрок {ally_code} не найден или не состоит в гильдии.")
                return

        cfg = database.get_guild_config(guild_id) or {}
        if opponent_id == cfg.get("swgoh_guild_id"):
            await inter.edit_original_response("❌ Это ваша же гильдия.")
            return
        try:
            resp = await asyncio.to_thread(self.bot.comlink.get_guild, opponent_id)
        except Exception as e:
            print(f"⚠️ [ВГ соперник] get_guild {opponent_id}: {e!r}")
            resp = None
        guild = _guild_payload(resp)
        member_ids = [m["playerId"] for m in guild.get("member") or [] if m.get("playerId")]
        if not member_ids:
            await inter.edit_original_response("❌ Не удалось получить состав гильдии-соперника из игры.")
            return
        opp_name = (guild.get("profile") or {}).get("name") or "Соперник"
        opp_gp = int((guild.get("profile") or {}).get("guildGalacticPower") or 0) or None

        ctx = await asyncio.to_thread(pp.load_context)
        stat_calc = self.bot.stat_calc
        done = 0
        sem = asyncio.Semaphore(PARALLEL)

        async def one(pid):
            nonlocal done
            async with sem:
                try:
                    p = await asyncio.to_thread(self.bot.comlink.get_player, player_id=pid)
                except Exception as e:
                    print(f"⚠️ [ВГ соперник] get_player {pid}: {e!r}")
                    p = None
            done += 1
            if not p or not p.get("rosterUnit"):
                return None
            return await asyncio.to_thread(
                pp.summarize, p["rosterUnit"], p.get("datacron") or [], pp.player_meta(p), stat_calc, ctx
            )

        await inter.edit_original_response(f"⏳ Собираю ростеры «{opp_name}» — {len(member_ids)} игроков, это около минуты…")
        results = await asyncio.gather(*(one(pid) for pid in member_ids))
        opp_players = [r for r in results if r]
        own_players = await asyncio.to_thread(pp.guild_players_from_cache, guild_id, stat_calc, ctx)
        if not own_players:
            await inter.edit_original_response("❌ Нет данных по ростерам своей гильдии — дождитесь ежечасного обновления.")
            return
        own_gp = None
        if cfg.get("swgoh_guild_id"):
            try:
                own_resp = await asyncio.to_thread(self.bot.comlink.get_guild, cfg["swgoh_guild_id"])
                own_gp = int((_guild_payload(own_resp).get("profile") or {}).get("guildGalacticPower") or 0) or None
            except Exception as e:
                print(f"⚠️ [ВГ соперник] своя гильдия: {e!r}")

        missed = len(member_ids) - len(opp_players)

        def work():
            own = pp.summarize_guild(cfg.get("name") or "Мы", own_gp, own_players, ctx)
            opp = pp.summarize_guild(opp_name, opp_gp, opp_players, ctx)
            note = f"не загрузились {missed} игроков соперника" if missed else None
            return guild_compare_image.render(own, opp, note)

        png = await asyncio.to_thread(work)
        await inter.edit_original_response(content=None, file=disnake.File(io.BytesIO(png), filename="guild_compare.png"))


def setup(bot):
    bot.add_cog(GuildCompareCog(bot))
