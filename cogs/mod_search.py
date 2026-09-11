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
from services import activity_diff, mod_search
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
        аликод: str = commands.Param(default=None, description="Код союзника — для игрока не из нашей гильдии, вместо параметра «игрок»"),
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

        # Discord-команда без динамического конструктора всё ещё выбирает по одному
        # значению на категорию (см. docstring модуля) — движок теперь принимает списки
        # (веб умеет несколько сразу, фидбек 2026-09-11), оборачиваем.
        filt = {
            "set_ids": [сет] if сет is not None else None,
            "slot_keys": [слот] if слот is not None else None,
            "primary_stat_ids": [primary] if primary is not None else None,
            "conditions": conditions,
        }

        if гильдия:
            if игрок is not None or аликод is not None:
                await inter.edit_original_response("❌ Укажите либо игрока (или код союзника), либо `гильдия: True`, не оба сразу.")
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

            # По фидбеку 2026-09-11: сводка показывает КАЖДОГО игрока ростера, включая
            # 0 совпадений — раньше молча пропускали, было непонятно, кого вообще
            # проверили. "Детали" ниже по-прежнему только для тех, у кого есть находки.
            rows = []
            for _discord_id, ally_code, name in roster:
                matches = mod_search.search_units(units_by_ally.get(ally_code, {}), **filt)
                rows.append((name, matches))
            rows.sort(key=lambda r: len(r[1]), reverse=True)
            matched_rows = [(name, matches) for name, matches in rows if matches]

            all_base_ids = {m["base_id"] for _name, matches in matched_rows for m in matches}
            names = database.get_game_unit_names(list(all_base_ids))

            total_matches = sum(len(m) for _, m in rows)
            lines = [f"Игроков с совпадениями: {len(matched_rows)}/{len(roster)} · всего модов: {total_matches}", ""]
            lines.append("## Сводка")
            for name, matches in rows:
                lines.append(f"**{name}** — {len(matches)}")
            if matched_rows:
                lines.append("")
                lines.append("## Детали")
                for name, matches in matched_rows:
                    for m in matches:
                        char_name = names.get(m["base_id"], m["base_id"])
                        lines.append(f"{name} — " + mod_search.describe_mod(m["base_id"], m["mod"], char_name))

            title = f"🔍 Поиск модов — гильдия"
            color = DATACRON_CHECK_COLOR_FULL if matched_rows else DATACRON_CHECK_COLOR_NONE
            embeds = _lines_to_embeds(title, color, lines)
            await inter.edit_original_response(embed=embeds[0])
            for e in embeds[1:]:
                await inter.followup.send(embed=e)
            return

        fetch_live = False
        if аликод is not None:
            if игрок is not None:
                await inter.edit_original_response("❌ Укажите либо игрока из списка, либо код союзника — не оба сразу.")
                return
            ally_code = guild_resolver.normalize_ally_code(аликод)
            if ally_code is None:
                await inter.edit_original_response("❌ Код союзника должен состоять из 9 цифр.")
                return
            cache = self.bot.guild_roster_caches.get(guild_id, {})
            игрок = cache.get(ally_code, ally_code)
            fetch_live = True
        elif игрок is None:
            registration = database.get_user_registration(str(inter.author.id), guild_id=guild_id)
            if not registration:
                await inter.edit_original_response("❌ Игрок не указан, а вы не зарегистрированы — используйте `/регистрация`, укажите игрока явно или код союзника.")
                return
            ally_code, игрок = registration
        else:
            cache = self.bot.guild_roster_caches.get(guild_id, {})
            ally_code = cache.get(игрок)
            if not ally_code:
                await inter.edit_original_response("❌ Игрок не найден в составе гильдии.")
                return

        units = database.get_player_units(ally_code)
        if not units and fetch_live:
            # Игрок не из нашей гильдии — на него не распространяется часовая
            # синхронизация ростера (services/activity_diff.py), поэтому кэша ещё
            # может не быть вообще; тянем один раз напрямую из Comlink и кэшируем,
            # как это уже делает /статы (_get_unit_for_player) для того же случая.
            try:
                units = await activity_diff.fetch_player_units(self.bot.comlink, ally_code)
            except Exception as e:
                await inter.edit_original_response(f"❌ Ошибка получения данных игрока: {e}")
                return
            if units:
                database.upsert_player_units(ally_code, units)
        if not units:
            await inter.edit_original_response(f"⚠️ Нет данных по игроку {игрок} — подождите следующей синхронизации ростера.")
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
