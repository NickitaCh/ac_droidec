"""Процесс-локальный калькулятор-прогноз статов для веб-дашборда (порт /статы и
/статы_релик) — тот же смысл, что bot.stat_calc в боте, но веб-процесс не имеет
доступа к объекту bot: StatCalc кэшируется здесь с TTL 12ч, лениво строится при
первом обращении (без фонового tasks.loop, как в датакрон-каталоге, см.
services/datacron_catalog.py — тот же паттерн).

cogs.stat_requirements._evaluate_character_player/_project_character_relic
принимают первым параметром объект с атрибутами .comlink/.stat_calc (в боте —
сам bot) — здесь передаётся лёгкий stand-in (types.SimpleNamespace), а не bot,
чтобы не трогать/не дублировать существующую расчётную логику."""

import asyncio
import time
import types

import stat_engine
from cogs.stat_requirements import _evaluate_character_player, _project_character_relic, _build_guild_report, SCENARIO_RAW

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


def cached_stat_calc():
    """Уже построенный калькулятор (даже с истёкшим TTL) или None — без сетевых вызовов.
    Для страниц, которые не должны ждать первую сборку (деф ВГ, web/routes/tw_defense.py)."""
    return _stat_calc


# ---- Фоновая сборка: прогрев при старте веба (web/app.py) и страницы, которые
# показывают «данные загружаются» вместо ожидания (деф ВГ). Сборка тяжёлая (вся
# игровая база из Comlink), но идёт в to_thread — event loop не блокирует.
_build_task: asyncio.Task | None = None
_last_error: str | None = None


async def _background_build(comlink):
    global _last_error
    try:
        await get_stat_calc(comlink)
        _last_error = None
    except Exception as e:
        _last_error = str(e) or e.__class__.__name__
        print(f"⚠️ [web] Калькулятор статов не собрался в фоне: {_last_error}")


def start_background_build(comlink) -> None:
    """Запускает сборку/обновление калькулятора в фоне, если он отсутствует или устарел
    и сборка ещё не идёт. Ничего не ждёт."""
    global _build_task
    if _stat_calc is not None and (time.time() - _cached_at) <= _TTL_SECONDS:
        return
    if _build_task is not None and not _build_task.done():
        return
    _build_task = asyncio.create_task(_background_build(comlink))


def build_status() -> dict:
    return {
        "ready": _stat_calc is not None,
        "loading": _build_task is not None and not _build_task.done(),
        "error": _last_error if _stat_calc is None else None,
    }


def _bot_stand_in(comlink, stat_calc):
    return types.SimpleNamespace(comlink=comlink, stat_calc=stat_calc)


async def evaluate_character_player(
    comlink, stat_calc, plate_name: str, base_id: str, ally_code, force_refresh: bool, player_label, guild_id: int = 1,
    scenario: str = SCENARIO_RAW, forced_scheme: int | None = None,
):
    """Обёртка над cogs.stat_requirements._evaluate_character_player — см. её докстринг
    для формата результата (char_name, block, matched, total, updated_at, failed_required,
    required_total, active_scheme, scheme_label), для смысла scenario (SCENARIO_RAW/UP/FULL)
    и forced_scheme (форсирует схему мод-билда вместо авто-детекта, только для персонажей у
    которых есть схемы), либо None, если для этого персонажа нет сохранённых требований в плейте."""
    bot_stand_in = _bot_stand_in(comlink, stat_calc)
    return await _evaluate_character_player(bot_stand_in, plate_name, base_id, ally_code, force_refresh, player_label, guild_id=guild_id, scenario=scenario, forced_scheme=forced_scheme)


async def project_character_relic(comlink, stat_calc, plate_name: str, base_id: str, target_relic: int, guild_id: int = 1, forced_scheme: int | None = None):
    """Обёртка над cogs.stat_requirements._project_character_relic — (char_name, block)
    либо None, если для этого персонажа нет сохранённых требований в плейте."""
    bot_stand_in = _bot_stand_in(comlink, stat_calc)
    return await _project_character_relic(bot_stand_in, plate_name, base_id, target_relic, guild_id=guild_id, forced_scheme=forced_scheme)


async def build_guild_report(comlink, stat_calc, plate_name: str, char_keys: list, guild_id: int = 1, scenario: str = SCENARIO_RAW, forced_scheme: int | None = None) -> dict:
    """Обёртка над cogs.stat_requirements._build_guild_report — см. её докстринг для формата
    результата ({"error", "total_players", "compliant", "problem", "no_data"}) и параметров
    scenario/forced_scheme."""
    bot_stand_in = _bot_stand_in(comlink, stat_calc)
    return await _build_guild_report(bot_stand_in, plate_name, char_keys, guild_id=guild_id, scenario=scenario, forced_scheme=forced_scheme)
