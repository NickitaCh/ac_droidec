"""/тб оз_готовность — кто в гильдии проходит спецмиссии (ОЗ) «Восхода Империи» по
требованиям игры: таблица «игрок × спецмиссия» картинкой, как /рейд готовность. Логика —
services/tb_covert.py, данные — кэш ежечасного синка ростеров."""

import asyncio
import io

import disnake
from disnake.ext import commands

import database
import guild_resolver
from services import guild_units_image, tb_covert


class TBReadinessCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.slash_command(name="тб", description="Готовность гильдии к «Восходу Империи»")
    async def tb_group(self, inter: disnake.ApplicationCommandInteraction):
        pass

    @tb_group.sub_command(name="оз_готовность", description="Кто в гильдии проходит спецмиссии (ОЗ) по требованиям игры")
    async def covert_readiness(
        self,
        inter: disnake.ApplicationCommandInteraction,
        этап: int = commands.Param(default=None, ge=1, le=6, description="Этап ВИ (пусто → все спецмиссии)"),
    ):
        await inter.response.defer()
        guild_id = await guild_resolver.require_guild_id(inter)
        if guild_id is None:
            return
        if not tb_covert.missions():
            await inter.edit_original_response("❌ Требования спецмиссий ещё не загружены из игры — попробуйте позже.")
            return
        r = await asyncio.to_thread(tb_covert.guild_grid, guild_id, этап)
        if not r["missions"]:
            await inter.edit_original_response(f"На {этап} этапе спецмиссий нет.")
            return
        if not r["players"]:
            await inter.edit_original_response("❌ Нет данных по ростерам гильдии — дождитесь ежечасного обновления.")
            return
        cfg = database.get_guild_config(guild_id) or {}
        title = "Спецмиссии ВИ" + (f", этап {этап}" if этап else "")
        lines = []
        for m, u, n in zip(r["missions"], r["units"], r["ready_counts"]):
            lead = u["name"]
            unlock = f" · открывает {tb_covert.planet_ru(m['unlocks'])}" if m["unlocks"] else ""
            lines.append(f"**{m['phase']} · {tb_covert.planet_ru(m['planet'])}** — {lead}: проходят {n} из {len(r['players'])}{unlock}")
        png = await asyncio.to_thread(
            guild_units_image.render, r, cfg.get("name") or "Гильдия", "данные ежечасного обновления", title,
            ["✓ — проходит · 3/5 — подходят 3 юнита из 5 · — — нет обязательного персонажа",
             "Над колонкой — порог реликвии и сколько юнитов нужно, портрет — обязательный персонаж",
             "% — доля пройденных спецмиссий"],
            f"Проходят все: {sum(1 for p in r['players'] if p['ready'])} из {len(r['players'])}",
            128,
        )
        await inter.edit_original_response(
            content="\n".join(lines)[:2000],
            file=disnake.File(io.BytesIO(png), filename="tb_covert_ready.png"),
        )


def setup(bot):
    bot.add_cog(TBReadinessCog(bot))
