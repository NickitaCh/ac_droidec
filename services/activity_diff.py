"""Сравнение снимков ростера игрока (сырые rosterUnit из comlink.get_player, см.
cogs/stat_requirements.py::_fetch_player_units) между двумя циклами player_units_sync_loop —
источник событий гильдийской активности вместо сдохшего из-за Cloudflare скрапинга
swgoh.gg (был cogs/gohgg_activity.py). Пишет в ту же guild_activity_events, тем же
набором action_type ('gear'/'relic'/'star'/'zeta'/'omicron'), плюс новый 'unlock'.

old_value="" (не None) для zeta/omicron/unlock — как и в исходном скрапере: в UNIQUE-индексе
guild_activity_events каждый NULL уникален сам по себе (поведение SQLite), из-за чего
INSERT OR IGNORE не смог бы задедуплицировать повторную запись одного и того же события."""

import stat_engine

ZETA_TIER = 8
OMICRON_MIN_TIER = 9


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
