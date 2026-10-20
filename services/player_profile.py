"""/профиль — сводка игрока или всей гильдии в духе HotBot /profile (картинку рисует
services/profile_image.py). Здесь только расчёт: без Discord и PIL, чтобы тем же
пользоваться и с сайта.

Игрок считается по свежему comlink.get_player; гильдия — по кэшам ежечасного синка
(player_unit_cache, player_datacron_cache и player_profile_cache — последний пишет
services/activity_diff.py::fetch_player_units через player_meta ниже), без 50 запросов
к Comlink на каждый вызов. Статы — StatCalc.calc_roster_stats по всему ростеру (≈0,2 с
на игрока): он же даёт ГМ каждого юнита (топ-80) и вклад модов в скорость (stats.mods)."""

import copy
import json
import statistics

import database

# Мета и флот — ровно как в /profile у HotBot (решение пользователя 2026-10-20; список
# снят с картинки HotBot, порядок — по английскому имени, как там); правится здесь.
META_CHARACTERS = (
    "BAYLANSKOLL", "MANDALORBOKATAN", "COMMANDERAHSOKA", "DARTHBANE", "DARTHMALGUS",
    "EZRAEXILE", "JARJARBINKS", "MORGANELSBETH", "STARKILLER", "TARONMALICOS", "THIRDSISTER",
)
FLEET_META = ("CAPITALLEVIATHAN", "CAPITALPROFUNDITY", "CAPITALEXECUTOR")
# ГЛ — в порядке выхода в игре, как у HotBot; новые (из игровых данных) — в конец.
GL_ORDER = (
    "GLREY", "SUPREMELEADERKYLOREN", "GRANDMASTERLUKE", "SITHPALPATINE", "JEDIMASTERKENOBI",
    "LORDVADER", "JABBATHEHUTT", "GLLEIA", "GLAHSOKATANO", "GLHONDO",
)
# Список ГЛ берётся из игровых данных (категория galactic_legend, пишет units_sync в
# bot_state "galactic_legends"); это — запасной вариант до первого синка.
FALLBACK_GALACTIC_LEGENDS = (
    "GLAHSOKATANO", "GLHONDO", "GLLEIA", "GLREY", "GRANDMASTERLUKE", "JABBATHEHUTT",
    "JEDIMASTERKENOBI", "LORDVADER", "SITHPALPATINE", "SUPREMELEADERKYLOREN",
)

LEAGUE_NAMES = {
    "KYBER": "Кайбер", "AURODIUM": "Ауродий", "CHROMIUM": "Хромий",
    "BRONZIUM": "Бронзий", "CARBONITE": "Карбонит",
}

# Вторички модов (unitStatId → statValueDecimal / 10000 = доля). Пороги «O»/«D» сверены
# с HotBot на реальном ростере: O = нападение %, D = защита %.
STAT_SPEED, STAT_OFFENSE_PCT, STAT_DEFENSE_PCT = 5, 48, 49

# Финальные статы кораблей StatCalc отдаёт по номерам UnitStat, персонажей — по именам
# (по этому же признаку отличаем корабль: у него нет ключа "Speed").
_SHIP_STAT_KEYS = {"Health": "1", "Speed": "5", "Physical Damage": "6", "Special Damage": "7",
                   "Potency": "17", "Tenacity": "18"}


def galactic_legends() -> list[str]:
    raw = database.get_bot_state("galactic_legends", guild_id=0)
    if raw:
        try:
            ids = json.loads(raw)
            if ids:
                return list(ids)
        except ValueError:
            pass
    return list(FALLBACK_GALACTIC_LEGENDS)


def ordered_gls(gls: list[str]) -> list[str]:
    known = [g for g in GL_ORDER if g in gls]
    return known + sorted(g for g in gls if g not in GL_ORDER)


def _base_id(unit: dict) -> str:
    return unit.get("baseId") or (unit.get("definitionId") or "").split(":")[0]


def player_meta(player: dict) -> dict:
    """Небольшая часть ответа get_player, нужная профилю помимо ростера (кэшируется)."""
    stats = {s.get("nameKey"): int(s.get("value") or 0) for s in player.get("profileStat") or []}
    pvp = {p.get("tab"): p.get("rank") for p in player.get("pvpProfile") or []}
    rating = player.get("playerRating") or {}
    rank_status = rating.get("playerRankStatus") or {}
    division_id = rank_status.get("divisionId")
    return {
        "name": player.get("name") or "",
        "ally_code": str(player.get("allyCode") or ""),
        "guild_id": player.get("guildId") or "",
        "guild_name": player.get("guildName") or "",
        "stats": stats,
        "arena_rank": pvp.get(1),
        "fleet_rank": pvp.get(2),
        "league": rank_status.get("leagueId") or "",
        "division": (30 - division_id) // 5 if division_id else None,
        "skill_rating": (rating.get("playerSkillRating") or {}).get("skillRating"),
    }


def _stat(final: dict, name: str, is_ship: bool):
    value = final.get(_SHIP_STAT_KEYS[name]) if is_ship else final.get(name)
    return float(value) if value is not None else None


def _skill_counts(unit: dict, skills: dict) -> tuple[int, list[str]]:
    """(зет, [режимы омикронов]) — пороги свои у каждой способности (skill_tier_thresholds)."""
    zetas, omicrons = 0, []
    for sk in unit.get("skill") or []:
        info = skills.get(sk.get("id"))
        if not info:
            continue
        zeta_tier, omicron_tier, mode = info
        tier = sk.get("tier", -1)
        if zeta_tier is not None and tier >= zeta_tier:
            zetas += 1
        if omicron_tier is not None and tier >= omicron_tier:
            omicrons.append(mode or "")
    return zetas, omicrons


def _unit_row(unit: dict, names: dict, skills: dict, is_ship: bool) -> dict:
    base_id = _base_id(unit)
    stats = unit.get("stats") or {}
    final = stats.get("final") or {}
    gear = unit.get("currentTier") or 0
    relic_tier = (unit.get("relic") or {}).get("currentTier") or 0
    zetas, omicrons = _skill_counts(unit, skills)
    speed_mods = (stats.get("mods") or {}).get("Speed") if not is_ship else None
    return {
        "base_id": base_id,
        "name": names.get(base_id, base_id),
        "stars": unit.get("currentRarity") or 0,
        "gear": gear,
        "relic": relic_tier - 2 if gear >= 13 and relic_tier >= 2 else None,
        "level": unit.get("currentLevel") or 0,
        "zetas": zetas,
        "omicrons": len(omicrons),
        "ultimate": bool(unit.get("purchasedAbilityId")),
        "gp": unit.get("gp") or 0,
        "is_ship": is_ship,
        "speed": _stat(final, "Speed", is_ship),
        "speed_mods": float(speed_mods) if speed_mods is not None else None,
        "health": _stat(final, "Health", is_ship),
        "protection": None if is_ship else _stat(final, "Protection", False),
        "potency": _stat(final, "Potency", is_ship),
        "tenacity": _stat(final, "Tenacity", is_ship),
        "phys": _stat(final, "Physical Damage", is_ship),
        "spec": _stat(final, "Special Damage", is_ship),
    }


def _mod_summary(roster: list[dict]) -> dict:
    speed = []
    out = {"six_dot": 0, "off6": 0, "off4": 0, "def14": 0, "def9": 0}
    for unit in roster:
        for mod in unit.get("equippedStatMod") or []:
            if str(mod.get("definitionId", ""))[1:2] == "6":
                out["six_dot"] += 1
            for sec in mod.get("secondaryStat") or []:
                stat = sec.get("stat") or {}
                sid = stat.get("unitStatId")
                value = int(stat.get("statValueDecimal") or 0) / 10000
                if sid == STAT_SPEED:
                    speed.append(value)
                elif sid == STAT_OFFENSE_PCT:
                    out["off6"] += value >= 0.06
                    out["off4"] += value >= 0.04
                elif sid == STAT_DEFENSE_PCT:
                    out["def14"] += value >= 0.14
                    out["def9"] += value >= 0.09
    out.update({
        "s25": sum(v >= 25 for v in speed), "s20_24": sum(20 <= v < 25 for v in speed),
        "s15_19": sum(15 <= v < 20 for v in speed), "s10_14": sum(10 <= v < 15 for v in speed),
        "s20": sum(v >= 20 for v in speed), "s15": sum(v >= 15 for v in speed),
        "s10": sum(v >= 10 for v in speed),
    })
    return out


def _datacron_summary(datacrons: list[dict]) -> dict:
    out = {"rerolls": 0, "l9": 0, "l6_8": 0, "l3_5": 0}
    for dc in datacrons or []:
        out["rerolls"] += int(dc.get("rerollCount") or 0)
        level = len(dc.get("affix") or [])
        if level >= 9:
            out["l9"] += 1
        elif level >= 6:
            out["l6_8"] += 1
        elif level >= 3:
            out["l3_5"] += 1
    return out


def _avg(values):
    values = [v for v in values if v is not None]
    return statistics.fmean(values) if values else None


def summarize(roster: list[dict], datacrons: list[dict], meta: dict, stat_calc, ctx: dict) -> dict:
    """roster — сырые rosterUnit; ctx = load_context(). Не мутирует roster."""
    names, skills, gls = ctx["names"], ctx["skills"], ctx["gls"]
    units = stat_calc.calc_roster_stats(copy.deepcopy(roster))
    rows = {}
    for unit in units:
        base_id = _base_id(unit)
        if base_id:
            final = (unit.get("stats") or {}).get("final") or {}
            rows[base_id] = _unit_row(unit, names, skills, "Speed" not in final)
    chars = [r for r in rows.values() if not r["is_ship"]]
    by_gp = sorted(chars, key=lambda r: r["gp"], reverse=True)
    top80 = by_gp[:80]

    zetas = sum(r["zetas"] for r in rows.values())
    omicron_modes = []
    for unit in roster:
        omicron_modes += _skill_counts(unit, skills)[1]
    relic_levels = [r["relic"] for r in chars if r["relic"] is not None]
    stats = meta.get("stats") or {}

    def with_missing(ids):
        """Все юниты списка: чего нет — заглушка {missing: True} (HotBot показывает их серыми)."""
        return [rows.get(b) or {"base_id": b, "name": names.get(b, b), "missing": True,
                                "is_ship": b.startswith("CAPITAL")} for b in ids]

    return {
        "meta": meta,
        "gp": stats.get("STAT_GALACTIC_POWER_ACQUIRED_NAME") or sum(r["gp"] for r in rows.values()),
        "char_gp": stats.get("STAT_CHARACTER_GALACTIC_POWER_ACQUIRED_NAME") or sum(r["gp"] for r in chars),
        "ship_gp": stats.get("STAT_SHIP_GALACTIC_POWER_ACQUIRED_NAME") or sum(r["gp"] for r in rows.values() if r["is_ship"]),
        "top80": sum(r["gp"] for r in top80),
        "zetas": zetas,
        "omicrons": len(omicron_modes),
        "omicrons_tw": omicron_modes.count("ВГ"),
        "omicrons_tb": omicron_modes.count("ТБ"),
        "omicrons_gac": omicron_modes.count("ВА"),
        "g13": sum(r["gear"] == 13 for r in chars),
        "g12": sum(r["gear"] == 12 for r in chars),
        "g11": sum(r["gear"] == 11 for r in chars),
        "speed_g11": _avg(r["speed_mods"] for r in chars if r["gear"] >= 11),
        "speed_top80": _avg(r["speed_mods"] for r in top80),
        "relics": {
            "total": sum(relic_levels),
            **{f"r{n}": relic_levels.count(n) for n in range(5, 11)},
            "r1_4": sum(1 <= lv <= 4 for lv in relic_levels),
        },
        "mods": _mod_summary(roster),
        "datacrons": _datacron_summary(datacrons),
        "gls": with_missing(ordered_gls(gls)),
        "meta_units": with_missing(META_CHARACTERS),
        "fleet": with_missing(FLEET_META),
    }


def load_context() -> dict:
    """Справочники, общие для всех игроков одного вызова (имена, пороги зет/омикронов, ГЛ)."""
    gls = galactic_legends()
    ids = list(dict.fromkeys(gls + list(META_CHARACTERS) + list(FLEET_META)))
    return {
        "names": database.get_game_unit_names(ids),
        "skills": database.get_all_skill_tier_info(),
        "gls": gls,
    }


# ---------------------------------------------------------------- гильдия

def guild_players_from_cache(guild_id: int, stat_calc, ctx: dict) -> list[dict]:
    """summarize() по каждому участнику текущего состава — из кэшей ежечасного синка
    (player_unit_cache и др.), без запросов в игру. Блокирующая — звать через to_thread."""
    codes = [code for code, _name in database.get_roster_by_player_id(guild_id).values() if code]
    units = database.get_player_units_bulk(codes)
    datacrons = database.get_player_datacrons_bulk(codes)
    profiles = database.get_player_profiles_bulk(codes)
    names = database.get_player_names(guild_id)
    players = []
    for code in codes:
        roster = units.get(code)
        if not roster:
            continue
        meta = profiles.get(code) or {"name": names.get(code, code), "ally_code": code, "stats": {}}
        players.append(summarize(list(roster.values()), datacrons.get(code) or [], meta, stat_calc, ctx))
    return players


def _sum(players: list[dict], *path):
    total = 0
    for p in players:
        value = p
        for key in path:
            value = value.get(key) if isinstance(value, dict) else None
        total += value or 0
    return total


def _aggregate_units(players: list[dict], key: str, ids: list[str], names: dict) -> list[dict]:
    """Средние статы по владельцам юнита + сколько их в гильдии (как у HotBot guild=true)."""
    result = []
    for base_id in ids:
        owners = [r for p in players for r in p[key] if r["base_id"] == base_id and not r.get("missing")]
        if not owners:
            continue
        row = {"base_id": base_id, "name": names.get(base_id, base_id), "is_ship": owners[0]["is_ship"],
               "owners": len(owners), "ultimates": sum(r["ultimate"] for r in owners),
               "avg_relic": _avg(r["relic"] for r in owners),
               "r8_plus": sum((r["relic"] or 0) >= 8 for r in owners),
               "stars7": sum(r["stars"] >= 7 for r in owners)}
        for stat in ("speed", "speed_mods", "health", "protection", "potency", "tenacity", "phys", "spec"):
            row[stat] = _avg(r[stat] for r in owners)
        result.append(row)
    return result


def summarize_guild(guild_name: str, guild_gp: int | None, players: list[dict], ctx: dict) -> dict:
    """players — результаты summarize() по участникам (у кого есть кэш ростера)."""
    n = len(players) or 1
    metas = [p["meta"] for p in players]
    relic_keys = ["total"] + [f"r{i}" for i in range(5, 11)] + ["r1_4"]
    gls = ordered_gls(ctx["gls"])
    return {
        "name": guild_name,
        "members": len(players),
        "gp": guild_gp or _sum(players, "gp"),
        "char_gp": _sum(players, "char_gp"),
        "ship_gp": _sum(players, "ship_gp"),
        "omicrons": _sum(players, "omicrons"),
        "omicrons_tw": _sum(players, "omicrons_tw"),
        "omicrons_tb": _sum(players, "omicrons_tb"),
        "omicrons_gac": _sum(players, "omicrons_gac"),
        "g13": _sum(players, "g13"),
        "g12": _sum(players, "g12"),
        "relics": {k: _sum(players, "relics", k) for k in relic_keys},
        "mods": {k: _sum(players, "mods", k) for k in players[0]["mods"]} if players else {},
        "datacrons": {k: _sum(players, "datacrons", k) for k in ("rerolls", "l9", "l6_8", "l3_5")},
        "avg": {
            "gp": _sum(players, "gp") / n,
            "top80": _sum(players, "top80") / n,
            "relics": _sum(players, "relics", "total") / n,
            "six_dot": _sum(players, "mods", "six_dot") / n,
            "zetas": _sum(players, "zetas") / n,
            "g13": _sum(players, "g13") / n,
            "g12": _sum(players, "g12") / n,
            "speed_g11": _avg(p["speed_g11"] for p in players),
            "arena_rank": _avg(m.get("arena_rank") for m in metas),
            "fleet_rank": _avg(m.get("fleet_rank") for m in metas),
        },
        "gls": _aggregate_units(players, "gls", gls, ctx["names"]),
        "meta_units": _aggregate_units(players, "meta_units", list(META_CHARACTERS), ctx["names"]),
        "fleet": _aggregate_units(players, "fleet", list(FLEET_META), ctx["names"]),
    }
