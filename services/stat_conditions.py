"""Player-specific requirement priorities, independent of mods and relic forecasts."""
import database


def apply_priorities(rows, conditions, units):
    """Promote to required when the chosen omicron is absent; keep base priority otherwise.

    Never mutate shared/cached rows. Missing units/skills mean no unlocked omicron.
    Unknown game thresholds fail explicitly rather than silently waiving a requirement.
    """
    tiers = database.get_skill_omicron_tiers(list({s for _, s in conditions.values()}))
    result = []
    for row in rows:
        condition = conditions.get(row[0])
        if condition:
            base_id, skill_id = condition
            threshold = tiers.get(skill_id)
            if threshold is None:
                raise ValueError("Не загружен уровень омикрона для условия плейта. Обновите справочник способностей.")
            skills = {s['id']: s.get('tier', 0) for s in (units.get(base_id) or {}).get('skill', [])}
            if skills.get(skill_id, 0) < threshold:
                row = (*row[:6], 'required', *row[7:])
        result.append(row)
    return result
