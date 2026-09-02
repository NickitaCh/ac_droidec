"""Подбор омикронов, которые стоит поставить игроку под приоритет гильдии для ВГ — план
"Приоритеты омикронов для ВГ" (~/.claude/plans/lively-noodling-moler.md), для Димы (гильд-
админ), заменяет ручную гугл-таблицу. Все данные уже кэшированы (player_unit_cache,
skill_tier_thresholds, unit_omicron_skills) — без похода в Comlink, чистые функции над БД.

Алгоритм "готов, но ещё не поставлен" — тот же, что уже проверен вживую в
services/activity_diff.py::diff_unit (skill.tier сравнивается с per-skill omicron_tier, а не с
универсальной константой)."""

import database
import guild_omicron_rules
import stat_engine


def _load_catalog(guild_id: int) -> dict:
    """Всё, что нужно для подбора по всей гильдии сразу — вызывается один раз, а не по разу
    на игрока (та же экономия, что get_all_skill_tier_thresholds грузится один раз за цикл
    синка, см. cogs/stat_requirements.py::player_units_sync_loop)."""
    priority = database.get_guild_omicron_priority(guild_id)  # [(skill_id, priority), ...]
    base_id_to_skills = database.get_all_unit_omicron_skills()  # {base_id: [skill_id, ...]}
    skill_owner = {sid: base_id for base_id, sids in base_id_to_skills.items() for sid in sids}
    thresholds = database.get_all_skill_tier_thresholds()  # {skill_id: (zeta_tier, omicron_tier)}
    skill_ids = [sid for sid, _priority in priority]
    display = database.get_skill_display_info(skill_ids)  # {skill_id: (name, ability_id, ability_type, omicron_mode)}
    unit_names = {}
    for skill_id, _priority in priority:
        base_id = skill_owner.get(skill_id)
        if base_id and base_id not in unit_names:
            unit_names[base_id] = database.get_game_unit_name(base_id) or base_id
    return {
        "priority": priority,
        "skill_owner": skill_owner,
        "thresholds": thresholds,
        "display": display,
        "unit_names": unit_names,
    }


def priority_list_display(guild_id: int) -> list:
    """[{skill_id, base_id, unit_name, skill_name, omicron_mode}, ...] в порядке приоритета —
    для рендера самого списка на /omicrons/priority (не подбор по игроку, просто отображение
    того, что сохранено в guild_omicron_priority)."""
    catalog = _load_catalog(guild_id)
    rows = []
    for skill_id, _priority in catalog["priority"]:
        base_id = catalog["skill_owner"].get(skill_id)
        name, _ability_id, _ability_type, omicron_mode = catalog["display"].get(skill_id, ("", "", "", ""))
        rows.append({
            "skill_id": skill_id,
            "base_id": base_id,
            "unit_name": catalog["unit_names"].get(base_id, base_id) if base_id else skill_id,
            "skill_name": name,
            "omicron_mode": omicron_mode,
        })
    return rows


def _requirements_met(reqs: list, units: dict) -> bool:
    for r in reqs:
        unit = units.get(r["base_id"])
        if unit is None:
            return False
        if r["relic"] is not None and stat_engine.get_current_relic_level(unit) < r["relic"]:
            return False
    return True


def _missing_for_units(units: dict, catalog: dict, parsed_rules: guild_omicron_rules.ParsedRules) -> list:
    result = []
    for skill_id, _priority in catalog["priority"]:
        base_id = catalog["skill_owner"].get(skill_id)
        if not base_id:
            continue
        unit = units.get(base_id)
        if unit is None:
            continue  # юнита нет в ростере вообще

        _zeta_tier, omicron_tier = catalog["thresholds"].get(skill_id, (None, None))
        if omicron_tier is None:
            continue
        current_tier = next((s.get("tier") for s in (unit.get("skill") or []) if s.get("id") == skill_id), None)
        if current_tier is not None and current_tier >= omicron_tier:
            continue  # омикрон уже поставлен

        reqs = parsed_rules.requirements_for(skill_id)
        if reqs and not _requirements_met(reqs, units):
            continue

        name, _ability_id, _ability_type, omicron_mode = catalog["display"].get(skill_id, ("", "", "", ""))
        result.append({
            "skill_id": skill_id,
            "base_id": base_id,
            "unit_name": catalog["unit_names"].get(base_id, base_id),
            "skill_name": name,
            "omicron_mode": omicron_mode,
        })
    return result


def missing_omicrons_for_player(ally_code: str, guild_id: int) -> list:
    """[{skill_id, base_id, unit_name, skill_name, omicron_mode}, ...] в порядке приоритета
    гильдии — омикроны, которые игрок ещё не поставил, но уже готов поставить (владеет всеми
    юнитами-требованиями на нужном релике)."""
    catalog = _load_catalog(guild_id)
    if not catalog["priority"]:
        return []
    rules_text = database.get_guild_omicron_requirement_rules(guild_id)
    parsed_rules, _errors = guild_omicron_rules.parse_rules(rules_text, guild_id)
    units = database.get_player_units(ally_code)
    return _missing_for_units(units, catalog, parsed_rules)


def missing_omicrons_report(guild_id: int) -> list:
    """[{ally_code, name, missing: [...]}, ...] по всем игрокам гильдии, только у кого есть
    хотя бы один отсутствующий готовый омикрон, отсортировано по убыванию количества —
    гильд-wide вид на /omicrons/report."""
    catalog = _load_catalog(guild_id)
    if not catalog["priority"]:
        return []
    rules_text = database.get_guild_omicron_requirement_rules(guild_id)
    parsed_rules, _errors = guild_omicron_rules.parse_rules(rules_text, guild_id)

    result = []
    for _discord_id, ally_code, ingame_name in database.get_all_user_mappings(guild_id):
        if not ally_code:
            continue
        units = database.get_player_units(ally_code)
        if not units:
            continue
        missing = _missing_for_units(units, catalog, parsed_rules)
        if missing:
            result.append({"ally_code": ally_code, "name": ingame_name, "missing": missing})
    result.sort(key=lambda r: len(r["missing"]), reverse=True)
    return result
