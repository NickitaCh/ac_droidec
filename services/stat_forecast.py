"""Процесс-локальный калькулятор-прогноз статов для веб-дашборда (порт /статы и
/статы_релик) — тот же смысл, что bot.stat_calc в боте, но веб-процесс не имеет
доступа к объекту bot: StatCalc кэшируется здесь с TTL 12ч, лениво строится при
первом обращении (без фонового tasks.loop, как в датакрон-каталоге, см.
services/datacron_catalog.py — тот же паттерн).

cogs.stat_requirements._evaluate_character_player/_project_character_relic
принимают первым параметром объект с атрибутами .comlink/.stat_calc (в боте —
сам bot) — здесь передаётся лёгкий stand-in (types.SimpleNamespace), а не bot,
чтобы не трогать/не дублировать существующую расчётную логику."""

import time
import types

import stat_engine
from cogs.stat_requirements import _evaluate_character_player, _project_character_relic, _build_guild_report

_TTL_SECONDS = 12 * 60 * 60

_stat_calc = None
_cached_at = 0.0


async def get_stat_calc(comlink):
    """Может бросить исключение, если ещё ни разу не строился успешно и Comlink
    недоступен — вызывающий код должен обработать (страница показывает "ещё
    загружается", не падает с 500). Если уже был построен хотя бы раз, сетевой
    сбой при плановом обновлении молча игнорируется, возвращается прошлый."""
    global _stat_calc, _cached_at
    now = time.time()
    if _stat_calc is not None and (now - _cached_at) <= _TTL_SECONDS:
        return _stat_calc
    try:
        fresh = await stat_engine.build_stat_calc(comlink)
    except Exception as e:
        if _stat_calc is not None:
            print(f"⚠️ [web] Не удалось обновить калькулятор статов, использую прошлый: {e}")
            return _stat_calc
        raise
    _stat_calc = fresh
    _cached_at = now
    return _stat_calc


def _bot_stand_in(comlink, stat_calc):
    return types.SimpleNamespace(comlink=comlink, stat_calc=stat_calc)


async def evaluate_character_player(
    comlink, stat_calc, plate_name: str, base_id: str, ally_code, force_refresh: bool, player_label, guild_id: int = 1
):
    """Обёртка над cogs.stat_requirements._evaluate_character_player — см. её докстринг
    для формата результата (char_name, block, matched, total, updated_at, matched_relic_free,
    total_relic_free, failed_required) либо None, если для этого персонажа нет сохранённых
    требований в плейте."""
    bot_stand_in = _bot_stand_in(comlink, stat_calc)
    return await _evaluate_character_player(bot_stand_in, plate_name, base_id, ally_code, force_refresh, player_label, guild_id=guild_id)


async def project_character_relic(comlink, stat_calc, plate_name: str, base_id: str, target_relic: int, guild_id: int = 1):
    """Обёртка над cogs.stat_requirements._project_character_relic — (char_name, block)
    либо None, если для этого персонажа нет сохранённых требований в плейте."""
    bot_stand_in = _bot_stand_in(comlink, stat_calc)
    return await _project_character_relic(bot_stand_in, plate_name, base_id, target_relic, guild_id=guild_id)


async def build_guild_report(comlink, stat_calc, plate_name: str, char_keys: list, guild_id: int = 1, account_for_relic: bool = True) -> dict:
    """Обёртка над cogs.stat_requirements._build_guild_report — см. её докстринг для формата
    результата ({"error", "total_players", "compliant", "problem", "no_data"}) и параметра
    account_for_relic."""
    bot_stand_in = _bot_stand_in(comlink, stat_calc)
    return await _build_guild_report(bot_stand_in, plate_name, char_keys, guild_id=guild_id, account_for_relic=account_for_relic)
