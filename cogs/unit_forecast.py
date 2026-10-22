"""/юнит_прогноз — юнит игрока в его текущих модах сейчас и на заданных звёздах/гире/
релике, картинкой (как C3PO `p unit`). Расчёт — services/unit_forecast.py, картинка —
services/unit_forecast_image.py. Игрок — свежий get_player: свой (или альт) по
умолчанию, любой код союзника — через «аликод»; открыто всем участникам, как /профиль.

/юнит — персонажи игроков бок о бок (как HotBot `/toon`): services/toon.py + toon_image.py.
Набор параметров — как у HotBot (5 юнитов, 4 участника Discord, 4 кода союзника, с_собой,
обновить, альт) плюс includerokamora (просьба пользователя 2026-10-20). Ростер — из кэша
ежечасного синка, если игрок в нём есть; «обновить» или игрок не из гильдии — из игры.

С 2026-10-21 в Discord обе команды заменены объединённой /юнит (cogs/unit_test.py, бывшая
/юнит_тест — решение по треду). Код оставлен для отката: LEGACY_UNIT_COMMANDS = True вернёт
/юнит и /юнит_прогноз, а новая команда снова станет /юнит_тест. После переключения — ручная
синхронизация команд с Discord."""

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
from services import activity_diff, toon, toon_image, unit_forecast, unit_forecast_image

MSK = ZoneInfo("Europe/Moscow")
ROKAMORA_ALLY_CODE = "365732527"  # includerokamora — просьба пользователя 2026-10-20
MAX_CARD_PLAYERS = 10  # 1 + 4 участника + 4 кода + Rokamora — как у HotBot
LEGACY_UNIT_COMMANDS = False
# Имена старых команд, пока они выключены, — чтобы не занимать «юнит» у новой.
RETIRED_UNIT_COMMANDS = () if LEGACY_UNIT_COMMANDS else ("юнит_старый", "юнит_прогноз")


def _parse_bracket_id(text: str) -> str:
    # «Имя [BASE_ID] 🚀» у кораблей — хвост после скобок не мешает.
    m = re.search(r"\[(.*?)\]", text or "")
    return m.group(1) if m else (text or "").strip()


async def autocomplete_ally_codes(inter: disnake.ApplicationCommandInteraction, string: str):
    """Игроки гильдии «Ник · код» со значением = код союзника; можно вписать любой код."""
    guild_id = guild_resolver.resolve_guild_id(inter.author)
    cache = (inter.bot.guild_roster_caches.get(guild_id) or {}) if guild_id is not None else {}
    needle = string.strip().lower()
    options = []
    if guild_resolver.normalize_ally_code(string):
        options.append(disnake.OptionChoice(name=string.strip()[:100], value=string.strip()[:100]))
    for name, code in cache.items():
        if needle in name.lower() or needle in str(code):
            options.append(disnake.OptionChoice(name=f"{name} · {code}"[:100], value=str(code)))
        if len(options) >= 25:
            break
    return options


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

    @commands.slash_command(name="юнит_прогноз", description="Статы юнита в текущих модулях сейчас и на других звёздах, гире или релике")
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
        is_ship = database.get_unit_types([_parse_bracket_id(юнит)]).get(_parse_bracket_id(юнит)) == "ship"
        error = unit_forecast.validate(звёзды, None if is_ship else гир, None if is_ship else релик)
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

        await self.send_forecast(inter, ally_code, _parse_bracket_id(юнит), звёзды, гир, релик)

    async def send_forecast(self, inter, ally_code: str, base_id: str, звёзды, гир, релик):
        """Картинка прогноза одного юнита (персонажа или корабля) игрока — ответом на inter.
        Общая для /юнит_прогноз и корабля в /юнит."""
        is_ship = database.get_unit_types([base_id]).get(base_id) == "ship"
        if not await stat_engine.wait_stat_calc(self.bot, inter):
            await inter.edit_original_response("⏳ Калькулятор статов не загрузился — попробуйте через пару минут.")
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

    @commands.slash_command(name="юнит" if LEGACY_UNIT_COMMANDS else "юнит_старый", description="Персонажи игроков бок о бок: статы, модули, зеты и омикроны (как HotBot /toon)")
    async def unit_card_cmd(
        self,
        inter: disnake.ApplicationCommandInteraction,
        юнит: str = commands.Param(description="Персонаж", autocomplete=autocomplete_character_unit),
        юнит2: str = commands.Param(default=None, description="Ещё персонаж", autocomplete=autocomplete_character_unit),
        юнит3: str = commands.Param(default=None, description="Ещё персонаж", autocomplete=autocomplete_character_unit),
        юнит4: str = commands.Param(default=None, description="Ещё персонаж", autocomplete=autocomplete_character_unit),
        юнит5: str = commands.Param(default=None, description="Ещё персонаж", autocomplete=autocomplete_character_unit),
        участник: disnake.User = commands.Param(default=None, description="Участник Discord (по его /регистрация)"),
        участник2: disnake.User = commands.Param(default=None, description="Ещё участник Discord"),
        участник3: disnake.User = commands.Param(default=None, description="Ещё участник Discord"),
        участник4: disnake.User = commands.Param(default=None, description="Ещё участник Discord"),
        аликод: str = commands.Param(default=None, description="Код союзника или игрок гильдии из списка", autocomplete=autocomplete_ally_codes),
        аликод2: str = commands.Param(default=None, description="Ещё код союзника или игрок гильдии", autocomplete=autocomplete_ally_codes),
        аликод3: str = commands.Param(default=None, description="Ещё код союзника или игрок гильдии", autocomplete=autocomplete_ally_codes),
        аликод4: str = commands.Param(default=None, description="Ещё код союзника или игрок гильдии", autocomplete=autocomplete_ally_codes),
        с_собой: bool = commands.Param(default=False, description="Добавить и вас (без других игроков вы и так в карточке)"),
        альт: bool = commands.Param(default=False, description="Вместо вашего основного аккаунта — второй (альт)"),
        обновить: bool = commands.Param(default=False, description="Свежие данные из игры вместо ежечасного обновления"),
        includerokamora: bool = commands.Param(default=False, description="Добавить Rokamora"),
    ):
        await inter.response.defer()
        base_ids = list(dict.fromkeys(_parse_bracket_id(u) for u in (юнит, юнит2, юнит3, юнит4, юнит5) if u))
        types = database.get_unit_types(base_ids)
        for base_id in base_ids:
            if not database.get_game_unit_name(base_id):
                await inter.edit_original_response(f"❌ Персонаж «{base_id}» не найден — выберите его из списка.")
                return
            if types.get(base_id) == "ship":
                await inter.edit_original_response("❌ Здесь только персонажи: у кораблей нет модов. Корабль — в `/юнит_прогноз`.")
                return

        guild_id = await guild_resolver.require_guild_id(inter)
        if guild_id is None:
            return
        users = [u for u in (участник, участник2, участник3, участник4) if u]
        raw_codes = [c for c in (аликод, аликод2, аликод3, аликод4) if c]

        ally_codes = []
        if с_собой or альт or not (users or raw_codes or includerokamora):
            if альт:
                alt = await guild_resolver.require_alt(inter, "unit_forecast")
                if alt is None:
                    return
                ally_codes.append(alt["ally_code"])
            else:
                registration = database.get_user_registration(str(inter.author.id), guild_id=guild_id)
                if not registration:
                    await inter.edit_original_response(
                        "❌ Вы не зарегистрированы — используйте `/регистрация` или укажите участника либо код союзника."
                    )
                    return
                ally_codes.append(registration[0])
        for user in users:
            registration = (database.get_user_registration(str(user.id), guild_id=guild_id)
                            or database.get_any_main_registration(str(user.id)))
            if not registration:
                await inter.edit_original_response(f"❌ {user.mention} не привязал(а) аккаунт через `/регистрация` — укажите его код союзника.")
                return
            ally_codes.append(registration[0])
        for raw in raw_codes:
            code = guild_resolver.normalize_ally_code(raw)
            if code is None:
                await inter.edit_original_response(f"❌ «{raw}» — не код союзника: нужно 9 цифр или игрок из списка.")
                return
            ally_codes.append(code)
        if includerokamora:
            ally_codes.append(ROKAMORA_ALLY_CODE)
        ally_codes = list(dict.fromkeys(str(c) for c in ally_codes))
        if len(ally_codes) > MAX_CARD_PLAYERS:
            await inter.edit_original_response(f"❌ Не больше {MAX_CARD_PLAYERS} игроков в одной карточке.")
            return

        if not await stat_engine.wait_stat_calc(self.bot, inter):
            await inter.edit_original_response("⏳ Калькулятор статов не загрузился — попробуйте через пару минут.")
            return

        players, ages = await self._card_players(ally_codes, guild_id, обновить)
        for code, player in zip(ally_codes, players):
            if not player or not player.get("rosterUnit"):
                await inter.edit_original_response(f"❌ Не удалось получить игрока с кодом {code} из игры.")
                return

        note = toon_image.freshness_note(max((age for age in ages if age is not None), default=None))

        def work():
            rows = toon.build(self.bot.stat_calc, players, base_ids)
            return toon_image.render(rows)

        try:
            png = await asyncio.to_thread(work)
        except Exception as e:
            print(f"⚠️ [Юнит] {base_ids} для {ally_codes}: {e!r}")
            await inter.edit_original_response("❌ Не удалось собрать карточку.")
            return
        await inter.edit_original_response(content=note, file=disnake.File(io.BytesIO(png), filename="toon.png"))

    async def _card_players(self, ally_codes: list[str], guild_id: int, refresh: bool) -> tuple[list, list]:
        """([сырой игрок {name, rosterUnit} | None], [возраст кэша в секундах | None = из игры]).
        Игрок из кэша синка — без запроса в игру; «обновить» освежает кэш (кулдаун 30 с,
        общий с /статы), игрок не из кэша — всегда свежий get_player."""
        names = database.get_player_names(guild_id)
        profiles = database.get_player_profiles_bulk(ally_codes)

        async def one(code):
            if database.get_player_units_age_seconds(code) is not None:
                if refresh:
                    try:
                        await activity_diff.refresh_player_units(self.bot.comlink, code)
                    except Exception as e:
                        print(f"⚠️ [Юнит] обновление {code}: {e!r}")
                units = await asyncio.to_thread(database.get_player_units, code)
                if units:
                    name = (profiles.get(code) or {}).get("name") or names.get(code) or code
                    age = database.get_player_units_age_seconds(code)
                    return {"name": name, "rosterUnit": list(units.values())}, (None if refresh else age)
            try:
                return await asyncio.to_thread(self.bot.comlink.get_player, allycode=str(code)), None
            except Exception as e:
                print(f"⚠️ [Юнит] get_player {code}: {e!r}")
                return None, None

        results = await asyncio.gather(*(one(c) for c in ally_codes))
        return [r[0] for r in results], [r[1] for r in results]

def setup(bot):
    bot.add_cog(UnitForecastCog(bot))
    for name in RETIRED_UNIT_COMMANDS:
        bot.remove_slash_command(name)
