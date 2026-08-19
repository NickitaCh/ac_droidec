"""Сравнение снимков ростера игрока (сырые rosterUnit из comlink.get_player) между двумя
циклами синка — источник событий гильдийской активности вместо сдохшего из-за Cloudflare
скрапинга swgoh.gg (был cogs/gohgg_activity.py). Пишет в ту же guild_activity_events, тем же
набором action_type ('gear'/'relic'/'star'/'zeta'/'omicron'), плюс новый 'unlock'.

old_value="" (не None) для zeta/omicron/unlock — как и в исходном скрапере: в UNIQUE-индексе
guild_activity_events каждый NULL уникален сам по себе (поведение SQLite), из-за чего
INSERT OR IGNORE не смог бы задедуплицировать повторную запись одного и того же события.

sync_player() — общая точка входа, используется и автоматическим циклом (cogs/stat_requirements.py
::player_units_sync_loop, раз в PLAYER_STATS_SYNC_HOURS), и ручной кнопкой "Обновить сейчас" на
веб-странице /activity (web/routes/guild_dashboard.py) — оба процесса (бот и веб) держат свой
собственный SwgohComlink на один и тот же comlink-сайдкар, но диффят/пишут события одинаково."""

import asyncio

import database
import stat_engine

ZETA_TIER = 8
OMICRON_MIN_TIER = 9


async def fetch_player_units(comlink, ally_code: str) -> dict:
    """base_id -> сырой rosterUnit из comlink.get_player. Блокирующий сетевой вызов — через to_thread."""
    player_data = await asyncio.to_thread(comlink.get_player, allycode=str(ally_code))
    roster = player_data.get("rosterUnit") or player_data.get("roster") or []
    units = {}
    for u in roster:
        base_id = u.get("baseId") or (u.get("definitionId", "") or "").split(":")[0]
        if base_id:
            units[base_id] = u
    return units


def diff_unit(old: dict, new: dict) -> list[tuple[str, str, str]]:
    """(action_type, old_value, new_value) для одного юнита между двумя снимками."""
    events = []

    old_gear = old.get("currentTier")
    new_gear = new.get("currentTier")
    if old_gear is not None and new_gear is not None and new_gear != old_gear:
        events.append(("gear", str(old_gear), str(new_gear)))

    old_relic = stat_engine.get_current_relic_level(old)
    new_relic = stat_engine.get_current_relic_level(new)
    if new_relic != old_relic:
        events.append(("relic", str(old_relic), str(new_relic)))

    old_rarity = old.get("currentRarity")
    new_rarity = new.get("currentRarity")
    if old_rarity is not None and new_rarity is not None and new_rarity != old_rarity:
        events.append(("star", str(old_rarity), str(new_rarity)))

    old_skills = {s.get("id"): s.get("tier") for s in (old.get("skill") or [])}
    for s in (new.get("skill") or []):
        skill_id = s.get("id")
        new_tier = s.get("tier")
        if skill_id is None or new_tier is None:
            continue
        old_tier = old_skills.get(skill_id)
        if new_tier == old_tier:
            continue
        if new_tier >= OMICRON_MIN_TIER and (old_tier is None or old_tier < OMICRON_MIN_TIER):
            events.append(("omicron", "", skill_id))
        elif new_tier == ZETA_TIER and (old_tier is None or old_tier < ZETA_TIER):
            events.append(("zeta", "", skill_id))

    return events


def diff_roster(old_units: dict, new_units: dict, is_first_sync: bool) -> list[tuple[str, str, str, str]]:
    """(base_id, action_type, old_value, new_value) по всем юнитам игрока.
    is_first_sync=True (для игрока ещё нет ни одной записи в player_unit_cache) —
    только сидируем кэш, без unlock-события на весь стартовый ростер сразу."""
    events = []
    if is_first_sync:
        return events
    for base_id, new_unit in new_units.items():
        old_unit = old_units.get(base_id)
        if old_unit is None:
            rarity = new_unit.get("currentRarity", 1)
            events.append((base_id, "unlock", "", f"{rarity}★"))
            continue
        for action_type, old_value, new_value in diff_unit(old_unit, new_unit):
            events.append((base_id, action_type, old_value, new_value))
    return events


async def sync_player(comlink, ally_code: str, guild_ids, event_date: str, timeout: float = 15.0) -> tuple[bool, int, list]:
    """Тянет живой ростер игрока (Comlink-вызов ограничен timeout секунд — тот же паттерн,
    что в web/routes/datacrons.py::_build_guild_report, а не голый to_thread без дедлайна),
    диффит против player_unit_cache, обновляет кэш и пишет новые события активности в
    guild_activity_events для каждой из guild_ids (обычно одна — но игрок теоретически может
    состоять в нескольких зарегистрированных гильдиях сразу).

    Возвращает (fetched, added_events, omicron_hits): fetched=False при таймауте или пустом
    ростере — вызывающий код (player_units_sync_loop, /activity/sync) использует это для
    честного счётчика "сколько игроков реально обновилось", а не считает таймаут за успех.
    omicron_hits — [(base_id, guild_id), ...] только для реально новых (не дублей) omicron-событий;
    используется cogs/stat_requirements.py::_announce_omicrons для объявлений в Discord — веб-вызов
    (/activity/sync) это поле игнорирует, т.к. веб-процесс не держит Discord-клиента и постить не может."""
    try:
        new_units = await asyncio.wait_for(fetch_player_units(comlink, ally_code), timeout=timeout)
    except asyncio.TimeoutError:
        return False, 0, []
    if not new_units:
        return False, 0, []
    old_units = database.get_player_units(ally_code)
    events = diff_roster(old_units, new_units, is_first_sync=not old_units)
    database.upsert_player_units(ally_code, new_units)
    added = 0
    omicron_hits = []
    for base_id, action_type, old_value, new_value in events:
        for guild_id in guild_ids:
            if database.add_guild_activity_event(
                guild_id=guild_id, ally_code=ally_code, base_id=base_id,
                action_type=action_type, old_value=old_value, new_value=new_value,
                event_date=event_date,
            ):
                added += 1
                if action_type == "omicron":
                    omicron_hits.append((base_id, guild_id))
    return True, added, omicron_hits
