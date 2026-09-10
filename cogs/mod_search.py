"""Поиск надетых модов гильдии по гибкому фильтру (сет/слот/primary + до 3 условий по
вторичкам, всё через И) — по запросу NicolozZ, тред "Гайд по АС Боту" #ас-задачи,
2026-09-10. См. services/mod_search.py за движком decode/match — здесь только слэш-команда
и рендер в Discord. Данные — уже закэшированный player_unit_cache, без обращений к Comlink.

Discord не умеет в динамический конструктор условий (в отличие от веб-версии, /mod-search) —
поэтому здесь фиксированные 3 слота под вторички вместо произвольного числа строк; трёх
независимых условий по вторичкам с запасом хватает на реальные запросы вида "сет+слот+2-3
вторички", более сложные сценарии — через веб."""

import disnake
from disnake.ext import commands

import database
import guild_resolver
import stat_engine
from services import mod_search
from cogs.violations import autocomplete_players
from cogs.stat_requirements import OPERATOR_CHOICES
from cogs.datacron_requirements import (
    DATACRON_CHECK_COLOR_FULL,
    DATACRON_CHECK_COLOR_NONE,
    _lines_to_embeds,
)

SET_CHOICES = [
    disnake.OptionChoice(name=name, value=int(set_id))
    for set_id, name in sorted(stat_engine.MOD_SET_IDS.items(), key=lambda kv: kv[1])
]
SLOT_CHOICES = [disnake.OptionChoice(name=label, value=key) for key, label in mod_search.SLOT_CHOICES]
STAT_CHOICES = [disnake.OptionChoice(name=name, value=sid) for sid, name, _is_pct in mod_search.STAT_CHOICES]


class ModSearchCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.slash_command(name="моды_поиск", description="Поиск надетых модов гильдии по фильтру: сет/слот/primary/вторички")
    async def mod_search_cmd(
        self,
        inter: disnake.ApplicationCommandInteraction,
        игрок: str = commands.Param(default=None, description="Игрок — без гильдия=True и без указания берётся ваша регистрация", autocomplete=autocomplete_players),
        гильдия: bool = commands.Param(default=False, description="Таблица по всей гильдии вместо одного игрока — только для офицеров"),
        сет: int = commands.Param(default=None, description="Сет мода", choices=SET_CHOICES),
        слот: str = commands.Param(default=None, description="Форма/слот мода", choices=SLOT_CHOICES),
        primary: int = commands.Param(default=None, description="Primary-стат мода", choices=STAT_CHOICES),
        стат_1: int = commands.Param(default=None, description="Вторичка №1 — стат", choices=STAT_CHOICES),
        оператор_1: str = commands.Param(default=None, description="Вторичка №1 — оператор", choices=OPERATOR_CHOICES),
        значение_1: float = commands.Param(default=None, description="Вторичка №1 — значение"),
        стат_2: int = commands.Param(default=None, description="Вторичка №2 — стат", choices=STAT_CHOICES),
        оператор_2: str = commands.Param(default=None, description="Вторичка №2 — оператор", choices=OPERATOR_CHOICES),
        значение_2: float = commands.Param(default=None, description="Вторичка №2 — значение"),
        стат_3: int = commands.Param(default=None, description="Вторичка №3 — стат", choices=STAT_CHOICES),
        оператор_3: str = commands.Param(default=None, description="Вторичка №3 — оператор", choices=OPERATOR_CHOICES),
        значение_3: float = commands.Param(default=None, description="Вторичка №3 — значение"),
    ):
        await inter.response.defer()

        guild_id = await guild_resolver.require_guild_id(inter)
        if guild_id is None:
            return

        conditions = []
        for stat_id, op, value in ((стат_1, оператор_1, значение_1), (стат_2, оператор_2, значение_2), (стат_3, оператор_3, значение_3)):
            if stat_id is None and op is None and value is None:
                continue
            if stat_id is None or op is None or value is None:
                await inter.edit_original_response("❌ Для каждой вторички нужно заполнить все три поля: стат, оператор и значение.")
                return
            conditions.append((stat_id, op, value))

        if сет is None and слот is None and primary is None and not conditions:
            await inter.edit_original_response("❌ Укажите хотя бы одно условие поиска (сет / слот / primary / вторичка).")
            return

        filt = {"set_id": сет, "slot_key": слот, "primary_stat_id": primary, "conditions": conditions}

        if гильдия:
            if игрок is not None:
                await inter.edit_original_response("❌ Укажите либо игрока, либо `гильдия: True`, не оба сразу.")
                return
            if not guild_resolver.is_officer_for_resolved_guild(inter.author):
                await inter.edit_original_response("❌ Проверка по всей гильдии доступна только офицерам.")
                return

            roster = database.get_all_user_mappings(guild_id)
            if not roster:
                await inter.edit_original_response("❌ Ростер гильдии пуст — подождите синхронизации с игрой.")
                return

            ally_codes = [ally_code for _, ally_code, _ in roster]
            units_by_ally = database.get_player_units_bulk(ally_codes)

            rows = []
            for _discord_id, ally_code, name in roster:
                matches = mod_search.search_units(units_by_ally.get(ally_code, {}), **filt)
                if matches:
                    rows.append((name, matches))
            rows.sort(key=lambda r: len(r[1]), reverse=True)

            all_base_ids = {m["base_id"] for _name, matches in rows for m in matches}
            names = database.get_game_unit_names(list(all_base_ids))

            total_matches = sum(len(m) for _, m in rows)
            lines = [f"Игроков с совпадениями: {len(rows)}/{len(roster)} · всего модов: {total_matches}", ""]
            if not rows:
                lines.append("Совпадений не найдено.")
            else:
                lines.append("## Сводка")
                for name, matches in rows:
                    lines.append(f"**{name}** — {len(matches)}")
                lines.append("")
                lines.append("## Детали")
                for name, matches in rows:
                    for m in matches:
                        char_name = names.get(m["base_id"], m["base_id"])
                        lines.append(f"{name} — " + mod_search.describe_mod(m["base_id"], m["mod"], char_name))

            title = f"🔍 Поиск модов — гильдия"
            color = DATACRON_CHECK_COLOR_FULL if rows else DATACRON_CHECK_COLOR_NONE
            embeds = _lines_to_embeds(title, color, lines)
            await inter.edit_original_response(embed=embeds[0])
            for e in embeds[1:]:
                await inter.followup.send(embed=e)
            return

        if игрок is None:
            registration = database.get_user_registration(str(inter.author.id), guild_id=guild_id)
            if not registration:
                await inter.edit_original_response("❌ Игрок не указан, а вы не зарегистрированы — используйте `/регистрация` или укажите игрока явно.")
                return
            ally_code, игрок = registration
        else:
            cache = self.bot.guild_roster_caches.get(guild_id, {})
            ally_code = cache.get(игрок)
            if not ally_code:
                await inter.edit_original_response("❌ Игрок не найден в составе гильдии.")
                return

        units = database.get_player_units(ally_code)
        if not units:
            await inter.edit_original_response(f"⚠️ Нет закэшированных данных по игроку {игрок} — подождите следующей синхронизации ростера.")
            return

        matches = mod_search.search_units(units, **filt)
        names = database.get_game_unit_names(list({m["base_id"] for m in matches}))

        lines = [mod_search.describe_mod(m["base_id"], m["mod"], names.get(m["base_id"], m["base_id"])) for m in matches]
        if not lines:
            lines = ["Совпадений не найдено."]

        title = f"🔍 Поиск модов — {игрок} ({len(matches)} совпадений)"
        color = DATACRON_CHECK_COLOR_FULL if matches else DATACRON_CHECK_COLOR_NONE
        embeds = _lines_to_embeds(title, color, lines)
        await inter.edit_original_response(embed=embeds[0])
        for e in embeds[1:]:
            await inter.followup.send(embed=e)


def setup(bot):
    bot.add_cog(ModSearchCog(bot))
