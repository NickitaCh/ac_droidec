# Мини-язык требований для приоритета омикронов ВГ (план "Приоритеты омикронов для ВГ",
# ~/.claude/plans/lively-noodling-moler.md) — по образцу tb_platoon_filters.py (сырой текст,
# один ряд на гильдию через database.get_/set_guild_omicron_requirement_rules, разбирается
# заново при каждом использовании, здесь никакого состояния между вызовами).
#
# Грамматика, одна строка — одно правило (пустые строки и строки с '#' — комментарии):
#   omicron [Юнит] require unit [Юнит]
#   omicron [Юнит] require unit [Юнит] relic N
#   omicron [Юнит: Название способности] require unit [Юнит] relic N   — только если у
#       юнита несколько омикрон-способностей (редкий кейс реворка), иначе достаточно имени.
#
# "omicron [X]" определяет, к какому омикрону относится правило — юнит резолвится в base_id
# (database.resolve_unit_display_names, тот же резолвер, что и в tb_platoon_filters), а
# base_id — в skill_id через database.get_all_unit_omicron_skills. Если у юнита ровно один
# омикрон-скилл — однозначно, без уточнения. Несколько строк с одинаковым "omicron [X]"
# работают по AND — все require должны выполниться, чтобы омикрон попал в рекомендованный
# список (см. services/omicron_priority.py). Омикрон без единой строки правил в тексте —
# требований нет, попадания в приоритетный список достаточно.
#
# Зета (суффикс "zeta" у require) — на будущее, в v1 не реализуем (пользователь подтвердил,
# что это не k v1) — грамматика (regex ниже) НЕ ловит его сейчас, добавится позже без слома
# уже сохранённых конфигов.
import re
from dataclasses import dataclass, field

import database

_RULE_RE = re.compile(
    r"^omicron\s+\[([^\]]+)\]\s+require\s+unit\s+\[([^\]]+)\](?:\s+relic\s+(\d+))?$",
    re.IGNORECASE,
)


@dataclass
class ParsedRules:
    # skill_id -> [{"base_id", "unit_name", "relic": int|None}, ...] — все требования по AND.
    requirements: dict = field(default_factory=dict)
    omicron_display_names: dict = field(default_factory=dict)  # skill_id -> "Юнит — Способность"
    unit_display_names: dict = field(default_factory=dict)  # base_id -> имя, как введено в тексте

    def requirements_for(self, skill_id: str) -> list:
        return self.requirements.get(skill_id, [])


def _resolve_omicron_target(raw: str, name_to_base_id: dict, base_id_to_skills: dict, skill_names: dict):
    """raw — содержимое скобок 'omicron [...]': 'Юнит' или 'Юнит: Способность'.
    Возвращает (skill_id, unit_name, error_message)."""
    if ":" in raw:
        unit_name, skill_name = (p.strip() for p in raw.split(":", 1))
    else:
        unit_name, skill_name = raw.strip(), None

    base_id = name_to_base_id.get(unit_name)
    if not base_id:
        return None, unit_name, f"юнит не найден: {unit_name!r}"

    skill_ids = base_id_to_skills.get(base_id) or []
    if not skill_ids:
        return None, unit_name, f"у юнита {unit_name!r} нет омикрон-способности"

    if len(skill_ids) == 1:
        return skill_ids[0], unit_name, None

    if not skill_name:
        options = ", ".join(skill_names.get(sid, sid) for sid in skill_ids)
        return None, unit_name, (
            f"у юнита {unit_name!r} несколько омикрон-способностей ({options}) — уточните: "
            f"omicron [{unit_name}: Название способности]"
        )

    for sid in skill_ids:
        if (skill_names.get(sid) or "").strip().lower() == skill_name.lower():
            return sid, unit_name, None
    options = ", ".join(skill_names.get(sid, sid) for sid in skill_ids)
    return None, unit_name, f"способность {skill_name!r} не найдена у {unit_name!r} (доступны: {options})"


def parse_rules(rules_text: str, guild_id: int) -> tuple:
    """Разбирает текст правил построчно. Возвращает (ParsedRules, errors), errors —
    список (номер_строки, текст_ошибки); при непустых errors вызывающая сторона
    (/omicrons/priority/rules) должна отказаться сохранять текст и показать ошибки как есть."""
    base_id_to_skills = database.get_all_unit_omicron_skills()
    all_skill_ids = [sid for sids in base_id_to_skills.values() for sid in sids]
    skill_display = database.get_skill_display_info(all_skill_ids)  # {skill_id: (name, ability_id, ability_type, omicron_mode)}
    skill_names = {sid: info[0] for sid, info in skill_display.items()}

    errors = []
    parsed_lines = []
    all_unit_names = set()

    for line_num, raw_line in enumerate(rules_text.splitlines(), start=1):
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        m = _RULE_RE.match(stripped)
        if not m:
            errors.append((line_num, f"не распознана строка правила: {stripped!r}"))
            continue
        omicron_raw, unit_raw, relic_raw = m.group(1).strip(), m.group(2).strip(), m.group(3)
        all_unit_names.add(omicron_raw.split(":", 1)[0].strip())
        all_unit_names.add(unit_raw)
        relic = int(relic_raw) if relic_raw is not None else None
        parsed_lines.append((line_num, omicron_raw, unit_raw, relic))

    # Один batch-запрос на все имена юнитов сразу, а не один на строку — тот же приём, что и
    # в tb_platoon_filters.parse_rules.
    name_to_base_id = database.resolve_unit_display_names(list(all_unit_names)) if all_unit_names else {}

    result = ParsedRules()
    for line_num, omicron_raw, unit_raw, relic in parsed_lines:
        skill_id, omicron_unit_name, error = _resolve_omicron_target(
            omicron_raw, name_to_base_id, base_id_to_skills, skill_names,
        )
        if error:
            errors.append((line_num, error))
            continue

        req_base_id = name_to_base_id.get(unit_raw)
        if not req_base_id:
            errors.append((line_num, f"юнит не найден: {unit_raw!r}"))
            continue

        result.unit_display_names[req_base_id] = unit_raw
        skill_name = skill_names.get(skill_id, "")
        result.omicron_display_names[skill_id] = f"{omicron_unit_name} — {skill_name}" if skill_name else omicron_unit_name

        result.requirements.setdefault(skill_id, []).append({
            "base_id": req_base_id,
            "unit_name": unit_raw,
            "relic": relic,
        })

    return result, errors


def describe_rules(parsed: ParsedRules) -> list:
    """Список строк на русском для отображения на /omicrons/priority/rules — что реально
    применится, без сырых skill_id/base_id."""
    lines = []
    for skill_id, reqs in parsed.requirements.items():
        omicron_name = parsed.omicron_display_names.get(skill_id, skill_id)
        parts = []
        for r in reqs:
            if r["relic"] is not None:
                parts.append(f"{r['unit_name']} (релик ≥{r['relic']})")
            else:
                parts.append(r["unit_name"])
        lines.append(f"Омикрон «{omicron_name}» требует: {', '.join(parts)}")
    return lines
