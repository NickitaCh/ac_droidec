"""/юнит прогноз — юнит игрока в его текущих модах сейчас и на заданных звёздах/гире/
релике, картинкой (как C3PO `p unit`). Расчёт — services/unit_forecast.py, картинка —
services/unit_forecast_image.py. Игрок — свежий get_player: свой (или альт) по
умолчанию, любой код союзника — через «аликод»; открыто всем участникам, как /профиль."""

import asyncio
import io
import re
from datetime import datetime
from zoneinfo import ZoneInfo

import disnake
from disnake.ext import commands

import database
import stat_engine
import guild_resolver
from services import unit_forecast, unit_forecast_image

MSK = ZoneInfo("Europe/Moscow")


def _parse_bracket_id(text: str) -> str:
    # «Имя [BASE_ID] 🚀» у кораблей — хвост после скобок не мешает.
    m = re.search(r"\[(.*?)\]", text or "")
    return m.group(1) if m else (text or "").strip()


async def autocomplete_character_unit(inter: disnake.ApplicationCommandInteraction, string: str):
    matches = database.search_game_units(string.strip(), limit=100)
    types = database.get_unit_types([base_id for base_id, _ in matches])
    options = []
    for base_id, name in matches:
        label = f"{name} [{base_id}]" + (" 🚀" if types.get(base_id) == "ship" else "")
        options.append(disnake.OptionChoice(name=label[:100], value=label[:100]))
        if len(options) >= 25:
            break
    return options


class UnitForecastCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.slash_command(name="юнит", description="Юнит игрока картинкой")
    async def unit_group(self, inter: disnake.ApplicationCommandInteraction):
        pass

    @unit_group.sub_command(name="прогноз", description="Статы юнита в текущих модах сейчас и на других звёздах, гире или релике")
    async def unit_forecast_cmd(
        self,
        inter: disnake.ApplicationCommandInteraction,
        юнит: str = commands.Param(description="Персонаж или корабль 🚀", autocomplete=autocomplete_character_unit),
        звёзды: int = commands.Param(default=None, ge=1, le=7, description="Звёзды для прогноза (1–7); не заданы при гире/релике — 7★"),
        гир: int = commands.Param(default=None, ge=1, le=13, description="Гир для прогноза (1–13)"),
        релик: int = commands.Param(default=None, ge=0, le=10, description="Реликвия для прогноза (0–10), ставит G13"),
        аликод: str = commands.Param(default=None, description="Код союзника любого игрока (пусто → вы)"),
        альт: bool = commands.Param(default=False, description=guild_resolver.ALT_PARAM_DESCRIPTION),
    ):
        await inter.response.defer()
        error = unit_forecast.validate(звёзды, гир, релик)
        if error:
            await inter.edit_original_response(f"❌ {error}")
            return

        if альт:
            alt = await guild_resolver.require_alt(inter, "unit_forecast", other_target=аликод is not None)
            if alt is None:
                return
            ally_code = alt["ally_code"]
        else:
            guild_id = await guild_resolver.require_guild_id(inter)
            if guild_id is None:
                return
            if аликод is not None:
                ally_code = guild_resolver.normalize_ally_code(аликод)
                if ally_code is None:
                    await inter.edit_original_response("❌ Код союзника должен состоять из 9 цифр.")
                    return
            else:
                registration = database.get_user_registration(str(inter.author.id), guild_id=guild_id)
                if not registration:
                    await inter.edit_original_response(
                        "❌ Вы не зарегистрированы — используйте `/регистрация` или укажите код союзника."
                    )
                    return
                ally_code = registration[0]

        if not await stat_engine.wait_stat_calc(self.bot, inter):
            await inter.edit_original_response("⏳ Калькулятор статов не загрузился — попробуйте через пару минут.")
            return

        base_id = _parse_bracket_id(юнит)
        is_ship = database.get_unit_types([base_id]).get(base_id) == "ship"
        if is_ship:
            error = unit_forecast.validate_ship(гир, релик)
            if error:
                await inter.edit_original_response(f"❌ {error}")
                return
        unit_name = database.get_game_unit_name(base_id)
        if not unit_name:
            await inter.edit_original_response("❌ Юнит не найден — выберите его из списка.")
            return

        try:
            player = await asyncio.to_thread(self.bot.comlink.get_player, allycode=str(ally_code))
        except Exception as e:
            print(f"⚠️ [Юнит] get_player {ally_code}: {e!r}")
            player = None
        if not player or not player.get("rosterUnit"):
            await inter.edit_original_response(f"❌ Не удалось получить игрока с кодом {ally_code} из игры.")
            return
        unit = next((u for u in player["rosterUnit"] if u.get("definitionId", "").split(":")[0] == base_id), None)
        player_name = player.get("name") or ally_code
        if unit is None:
            await inter.edit_original_response(f"❌ У **{player_name}** нет {'корабля' if is_ship else 'персонажа'} **{unit_name}**.")
            return

        updated_at = datetime.now(MSK).strftime("%d.%m.%Y %H:%M МСК")

        def work():
            if is_ship:
                crew = unit_forecast.ship_crew(self.bot.stat_calc, player["rosterUnit"], base_id)
                result = unit_forecast.forecast_ship(self.bot.stat_calc, unit, crew, звёзды)
            else:
                result = unit_forecast.forecast(self.bot.stat_calc, unit, звёзды, гир, релик)
            return unit_forecast_image.render(unit_name, base_id, player_name, updated_at, result)

        try:
            png = await asyncio.to_thread(work)
        except Exception as e:
            print(f"⚠️ [Юнит] расчёт {base_id} для {ally_code}: {e!r}")
            await inter.edit_original_response("❌ Не удалось посчитать статы этого юнита.")
            return
        await inter.edit_original_response(file=disnake.File(io.BytesIO(png), filename=f"unit_{base_id}.png"))


def setup(bot):
    bot.add_cog(UnitForecastCog(bot))
