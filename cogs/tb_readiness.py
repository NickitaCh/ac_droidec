"""/тб оз_готовность — кто в гильдии проходит спецмиссии (ОЗ) «Восхода Империи» по
требованиям игры: таблица «игрок × спецмиссия» картинкой, как /рейд готовность, или разбор
одного игрока по всем спецмиссиям. Логика — services/tb_covert.py, данные — кэш
ежечасного синка ростеров."""

import asyncio
import io

import disnake
from disnake.ext import commands

import database
import guild_resolver
from cogs.violations import autocomplete_players
from services import activity_diff, guild_units_image, tb_covert

STATUS_ICON = {"ready": "✅", "short": "⚠️", "missing": "❌"}


async def autocomplete_covert_planets(inter: disnake.ApplicationCommandInteraction, string: str):
    needle = string.strip().lower()
    return [disnake.OptionChoice(name=f"{ru} (этап {phase})", value=planet)
            for planet, ru, phase in tb_covert.planets() if needle in ru.lower() or needle in planet.lower()][:25]


def _mission_title(m: dict, lead: str) -> str:
    return f"{m['phase']} · {tb_covert.planet_ru(m['planet'])} — {lead} ({tb_covert.mission_label(m)})"


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
        планета: str = commands.Param(default=None, description="Планета (пусто → все со спецмиссиями)", autocomplete=autocomplete_covert_planets),
        этап: int = commands.Param(default=None, ge=1, le=6, description="Этап ВИ (пусто → все этапы)"),
        игрок: str = commands.Param(default=None, description="Один игрок гильдии — разбор, чего не хватает", autocomplete=autocomplete_players),
        аликод: str = commands.Param(default=None, description="Код союзника любого игрока вместо выбора из списка"),
    ):
        await inter.response.defer()
        guild_id = await guild_resolver.require_guild_id(inter)
        if guild_id is None:
            return
        if not tb_covert.missions():
            await inter.edit_original_response("❌ Требования спецмиссий ещё не загружены из игры — попробуйте позже.")
            return
        planet = None
        if планета:
            planet = tb_covert.resolve_planet(планета)
            if planet is None:
                await inter.edit_original_response("❌ На этой планете нет спецмиссий — выберите планету из списка.")
                return
        ms = tb_covert.missions(этап, planet)
        if not ms:
            await inter.edit_original_response(
                f"❌ У планеты {tb_covert.planet_ru(planet)} нет спецмиссий на {этап} этапе." if planet
                else f"На {этап} этапе спецмиссий нет.")
            return
        if игрок or аликод:
            await self._player(inter, guild_id, ms, игрок, аликод)
            return

        r = await asyncio.to_thread(tb_covert.guild_grid, guild_id, этап, planet)
        if not r["players"]:
            await inter.edit_original_response("❌ Нет данных по ростерам гильдии — дождитесь ежечасного обновления.")
            return
        cfg = database.get_guild_config(guild_id) or {}
        title = "Спецмиссии ВИ" + (f", {tb_covert.planet_ru(planet)}" if planet else "") + (f", этап {этап}" if этап else "")
        lines, total, phase = [], len(r["players"]), None
        for m, u, n in zip(r["missions"], r["units"], r["ready_counts"]):
            if m["phase"] != phase:
                phase = m["phase"]
                lines.append(f"### Этап {phase}")
            dot = "🟢" if n >= total * 0.9 else "🟡" if n >= total * 0.5 else "🔴"
            extra = [x for x in (tb_covert.reward_text(m, short=True),
                                 f"🔓 {tb_covert.planet_ru(m['unlocks'])}" if m["unlocks"] else "") if x]
            lines.append(f"{dot} **{tb_covert.planet_ru(m['planet'])}** · {u['name']} — **{n}/{total}**"
                         + "".join(f" · {x}" for x in extra))
        lines.append("-# проходят / всего · 🪙 жетоны событий гильдии за одну победу · 🔓 открывает планету")
        png = await asyncio.to_thread(
            guild_units_image.render, r, cfg.get("name") or "Гильдия", "данные ежечасного обновления", title,
            ["✓ — проходит · R5 — уровень обязательного персонажа ниже порога · — — его нет",
             "Над колонкой — порог реликвии, портрет — обязательный персонаж; ×5 и 3/5 — только у миссий без обязательных",
             "% — доля пройденных спецмиссий"],
            f"Проходят все: {sum(1 for p in r['players'] if p['ready'])} из {len(r['players'])}",
            128,
        )
        await inter.edit_original_response(
            content="\n".join(lines)[:2000],
            file=disnake.File(io.BytesIO(png), filename="tb_covert_ready.png"),
        )

    async def _player(self, inter, guild_id: int, ms: list, игрок, аликод):
        if аликод:
            ally_code = guild_resolver.normalize_ally_code(аликод)
            if ally_code is None:
                await inter.edit_original_response("❌ Код союзника должен состоять из 9 цифр.")
                return
        else:
            cache = self.bot.guild_roster_caches.get(guild_id) or {}
            ally_code = cache.get(игрок) if игрок in cache else None
            if ally_code is None:
                await inter.edit_original_response("❌ Игрок не найден — выберите его из списка.")
                return
        ally_code = str(ally_code)
        roster = await asyncio.to_thread(database.get_player_units, ally_code)
        note = "данные ежечасного обновления"
        if not roster:  # не из гильдии — берём из игры
            try:
                roster = await activity_diff.fetch_player_units(self.bot.comlink, ally_code)
            except Exception:
                roster = {}
            note = "данные из игры на момент запроса"
        if not roster:
            await inter.edit_original_response(f"❌ Не удалось получить игрока с кодом {ally_code}.")
            return
        name = database.get_player_names(guild_id).get(ally_code) or игрок or ally_code
        report = await asyncio.to_thread(tb_covert.player_report, ms, roster)
        passed = sum(1 for x in report if x["status"] == "ready")
        embed = disnake.Embed(title=f"Спецмиссии ВИ — {name}", description=f"Проходит {passed} из {len(report)}",
                              color=disnake.Color.green() if passed == len(report) else disnake.Color.orange())
        for x in report:
            m = x["mission"]
            lead = " + ".join(n for n, _lv, _ok in x["mandatory"]) or "любые подходящие"
            lines = [f"{'✅' if ok else '❌'} {n}: {lv or 'нет'}" for n, lv, ok in x["mandatory"]]
            reward = tb_covert.reward_text(m)
            if reward:
                lines.insert(0, f"🎁 За прохождение: {reward}")
            if x["status"] != "ready" and not m["mandatory"]:
                lines.append(f"Подходят {x['have']} из {x['need']}")
                if x["best"]:
                    lines.append("Докачать: " + ", ".join(f"{n} ({lv})" for n, lv in x["best"]))
                elif x["missing_slots"]:
                    lines.append(f"Не хватает юнитов: {x['missing_slots']}")
            if m["unlocks"]:
                lines.append(f"Открывает {tb_covert.planet_ru(m['unlocks'])}")
            embed.add_field(name=f"{STATUS_ICON[x['status']]} {_mission_title(m, lead)}"[:256],
                            value=("\n".join(lines) or "Все требования выполнены")[:1024], inline=False)
        embed.set_footer(text=note)
        await inter.edit_original_response(embed=embed)


def setup(bot):
    bot.add_cog(TBReadinessCog(bot))
