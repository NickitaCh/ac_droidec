"""/мод-ку — рейтинг качества модов (МодКу), расчёт в services/mod_ku.py, данные — кэш
player_unit_cache без Comlink (кроме чужого игрока по «аликод»). Закрыто флагом mod_ku
(opt-in, сейчас только АС). Режим расчёта переключается только на вебе (/mod-ku) — здесь
параметра режима нет намеренно: слэш-параметр видели бы все гильдии, а выбор режима есть
только у АС (решение пользователя 2026-10-14)."""

import asyncio

import disnake
from disnake.ext import commands

import database
import guild_resolver
from services import activity_diff, mod_ku
from cogs.violations import autocomplete_players
from cogs.datacron_requirements import DATACRON_CHECK_COLOR_FULL, _lines_to_embeds

WEB_URL = "https://swgoh-sng.ru/mod-ku"


def _player_lines(result: dict, mode: str, names: dict) -> list[str]:
    lines = [
        f"## МодКу: **{result['modku']}**",
        f"Скорость +{result['speed_part']} · атака +{result['attack_part']} · база 10",
        f"G13-персонажей: {result['chars']} · все моды ≥15 скорости у {result['coverage']}%",
        f"Скорость в допах: 25+ — {result['speed_ge'][25]}, 20+ — {result['speed_ge'][20]}, 15+ — {result['speed_ge'][15]}",
        f"Атака в допах: 6%+ — {result['offense_ge'][6]}, 4%+ — {result['offense_ge'][4]}",
        f"Режим: {mod_ku.MODES[mode]}",
        "",
        "**Скорость в допах** (значение · модов · очков)",
        "```",
    ]
    for row in result["speed_hist"]:
        lines.append(f"{row['speed']:>3} · {row['count']:>4} · {row['points']:>6}")
    lines.append("```")
    lines.append("**Слабее всего заможены**")
    for c in result["weakest"]:
        extra = []
        if c["empty"]:
            extra.append(f"пустых слотов: {c['empty']}")
        if c["slow"]:
            extra.append(f"модов <15 скорости: {c['slow']}")
        lines.append(f"{names.get(c['base_id'], c['base_id'])} — {c['rating']:.1f}" + (f" ({', '.join(extra)})" if extra else ""))
    lines.append("")
    lines.append(f"Как считается — {WEB_URL}")
    return lines


class ModKuCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.slash_command(name="мод-ку", description="МодКу — рейтинг качества модов игрока или всей гильдии")
    async def mod_ku_cmd(
        self,
        inter: disnake.ApplicationCommandInteraction,
        игрок: str = commands.Param(default=None, description="Пусто → вы (/регистрация)", autocomplete=autocomplete_players),
        аликод: str = commands.Param(default=None, description="Код союзника вместо «игрок»"),
        гильдия: bool = commands.Param(default=False, description="Рейтинг всей гильдии"),
        альт: bool = commands.Param(default=False, description=guild_resolver.ALT_PARAM_DESCRIPTION),
    ):
        await inter.response.defer()

        if альт:
            alt = await guild_resolver.require_alt(inter, "mod_ku", other_target=bool(игрок or аликод or гильдия))
            if alt is None:
                return
            guild_id, аликод = alt["guild_id"], alt["ally_code"]
        else:
            guild_id = await guild_resolver.require_feature(inter, "mod_ku")
            if guild_id is None:
                return
        mode = mod_ku.get_mode(guild_id)

        if гильдия:
            if игрок is not None or аликод is not None:
                await inter.edit_original_response("❌ Укажите либо игрока, либо `гильдия: True`.")
                return
            if not guild_resolver.is_officer_for_resolved_guild(inter.author):
                await guild_resolver.send_denied(inter, guild_resolver.GUILD_WIDE_OFFICER_ONLY)
                return
            data = await asyncio.to_thread(mod_ku.compute_guild, guild_id, mode)
            lines = [f"Режим: {mod_ku.MODES[mode]} · подробности — {WEB_URL}", ""]
            for row in data["rows"]:
                r = row["result"]
                if r is None:
                    lines.append(f"{row['place']}. {row['name']} — нет данных")
                else:
                    lines.append(f"{row['place']}. **{row['name']}** — {r['modku']} (скор. +{r['speed_part']}, атк +{r['attack_part']}, 25+: {r['speed_ge'][25]})")
            embeds = _lines_to_embeds("🧮 МодКу — гильдия", DATACRON_CHECK_COLOR_FULL, lines)
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
            игрок = self.bot.guild_roster_caches.get(guild_id, {}).get(ally_code, ally_code)
            fetch_live = True
        elif игрок is None:
            registration = database.get_user_registration(str(inter.author.id), guild_id=guild_id)
            if not registration:
                await inter.edit_original_response("❌ Игрок не указан, а вы не зарегистрированы — используйте `/регистрация`, укажите игрока или код союзника.")
                return
            ally_code, игрок = registration
        else:
            ally_code = self.bot.guild_roster_caches.get(guild_id, {}).get(игрок)
            if not ally_code:
                await inter.edit_original_response("❌ Игрок не найден в составе гильдии.")
                return

        self_error = guild_resolver.self_only_error(inter.author, guild_id, ally_code)
        if self_error:
            await guild_resolver.send_denied(inter, self_error)
            return

        units = database.get_player_units(ally_code)
        if not units and fetch_live:
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

        weights = await asyncio.to_thread(mod_ku.attack_weights_for, guild_id, mode)
        result = await asyncio.to_thread(mod_ku.compute, units, weights)
        if result is None:
            await inter.edit_original_response(f"⚠️ У игрока {игрок} нет G13-персонажей — считать нечего.")
            return
        names = database.get_game_unit_names([c["base_id"] for c in result["weakest"]])
        embeds = _lines_to_embeds(f"🧮 МодКу — {игрок}", DATACRON_CHECK_COLOR_FULL, _player_lines(result, mode, names))
        await inter.edit_original_response(embed=embeds[0])
        for e in embeds[1:]:
            await inter.followup.send(embed=e)


def setup(bot):
    bot.add_cog(ModKuCog(bot))
