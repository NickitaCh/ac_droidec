# Мини-язык правил фильтрации для автозаполнения взводов ТБ (см. план "Автозаполнение
# взводов ТБ + фильтры" от 2026-08-29). Прецедента такого парсера в проекте не было
# (cogs/datacron_requirements.py — единственный похожий "конструктор правил" — целиком на
# структурированных Discord-параметрах/autocomplete, без свободного текста) — этот модуль
# написан с нуля. Хранится сырой текст (database.py::get_/set_tb_platoon_filter_rules,
# один ряд на гильдию), разбирается заново при каждом использовании — здесь никакого
# состояния между вызовами, чистые функции.
#
# Грамматика, одна строка — одно правило (пустые строки и строки с '#' — комментарии):
#   exclude player [Имя игрока]
#   exclude unit [Имя юнита]
#   bundle [Юнит-триггер] -> [Юнит 1], [Юнит 2], ...
#
# "bundle" читается так: если игроку уже назначен юнит-триггер где-либо в текущем
# автозаполнении, при выборе донора для юнита из пула автозаполнение отдаёт предпочтение
# ИМЕННО ЭТОМУ игроку (если он подходит по релику и не занят/не превысил лимит) — чтобы не
# дробить пачку между разными донорами. Это ПРЕДПОЧТЕНИЕ, не жёсткое требование: если игрок
# не владеет юнитом из пула или не проходит по другим правилам, слот всё равно достаётся
# следующему подходящему кандидату.
import re
from dataclasses import dataclass, field

import database

_EXCLUDE_PLAYER_RE = re.compile(r"^exclude\s+player\s+\[([^\]]+)\]$", re.IGNORECASE)
_EXCLUDE_UNIT_RE = re.compile(r"^exclude\s+unit\s+\[([^\]]+)\]$", re.IGNORECASE)
_BUNDLE_RE = re.compile(r"^bundle\s+\[([^\]]+)\]\s*->\s*(.+)$", re.IGNORECASE)
_BRACKET_ITEM_RE = re.compile(r"\[([^\]]+)\]")


@dataclass
class ParsedRules:
    exclude_player_codes: set = field(default_factory=set)
    exclude_player_names: dict = field(default_factory=dict)  # ally_code -> отображаемое имя
    exclude_unit_ids: set = field(default_factory=set)
    exclude_unit_names: dict = field(default_factory=dict)  # base_id -> отображаемое имя
    bundles: dict = field(default_factory=dict)  # trigger_base_id -> [pool_base_id, ...]
    unit_display_names: dict = field(default_factory=dict)  # base_id -> отображаемое имя (для describe_rules, включает и триггеры, и пул bundle)

    def is_player_excluded(self, ally_code: str) -> bool:
        return ally_code in self.exclude_player_codes

    def is_unit_excluded(self, base_id: str) -> bool:
        return base_id in self.exclude_unit_ids

    def bundle_pool_for(self, trigger_base_id: str) -> list:
        return self.bundles.get(trigger_base_id, [])


def parse_rules(rules_text: str, guild_id: int) -> tuple:
    """Разбирает текст правил построчно. Возвращает (ParsedRules, errors), errors —
    список (номер_строки, текст_ошибки); при непустых errors вызывающая сторона (веб-форма
    /tb/platoons/filters) должна отказаться сохранять текст и показать ошибки как есть."""
    mappings = database.get_all_user_mappings(guild_id)
    ally_code_by_name = {name.strip().lower(): (ally_code, name) for _discord_id, ally_code, name in mappings if name}

    errors = []
    parsed_lines = []  # (line_num, kind, payload)
    all_unit_names = set()

    for line_num, raw_line in enumerate(rules_text.splitlines(), start=1):
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("#"):
            continue

        m = _EXCLUDE_PLAYER_RE.match(stripped)
        if m:
            parsed_lines.append((line_num, "exclude_player", m.group(1).strip()))
            continue

        m = _EXCLUDE_UNIT_RE.match(stripped)
        if m:
            name = m.group(1).strip()
            all_unit_names.add(name)
            parsed_lines.append((line_num, "exclude_unit", name))
            continue

        m = _BUNDLE_RE.match(stripped)
        if m:
            trigger = m.group(1).strip()
            pool_names = [n.strip() for n in _BRACKET_ITEM_RE.findall(m.group(2))]
            if not pool_names:
                errors.append((line_num, "bundle: не нашлось юнитов в квадратных скобках после '->'"))
                continue
            all_unit_names.add(trigger)
            all_unit_names.update(pool_names)
            parsed_lines.append((line_num, "bundle", (trigger, pool_names)))
            continue

        errors.append((line_num, f"не распознана строка правила: {stripped!r}"))

    # Один batch-запрос на все имена юнитов сразу, а не один на строку — тот же приём, что
    # и в web/routes/guild_dashboard.py::tb_platoons для донат-листов.
    name_to_base_id = database.resolve_unit_display_names(list(all_unit_names)) if all_unit_names else {}

    result = ParsedRules()
    for line_num, kind, payload in parsed_lines:
        if kind == "exclude_player":
            name = payload
            hit = ally_code_by_name.get(name.lower())
            if not hit:
                errors.append((line_num, f"игрок не найден в гильдии: {name!r}"))
                continue
            ally_code, real_name = hit
            result.exclude_player_codes.add(ally_code)
            result.exclude_player_names[ally_code] = real_name

        elif kind == "exclude_unit":
            name = payload
            base_id = name_to_base_id.get(name)
            if not base_id:
                errors.append((line_num, f"юнит не найден: {name!r}"))
                continue
            result.exclude_unit_ids.add(base_id)
            result.exclude_unit_names[base_id] = name
            result.unit_display_names[base_id] = name

        elif kind == "bundle":
            trigger, pool_names = payload
            trigger_base_id = name_to_base_id.get(trigger)
            if not trigger_base_id:
                errors.append((line_num, f"юнит не найден: {trigger!r}"))
                continue
            pool_base_ids = []
            line_ok = True
            for n in pool_names:
                bid = name_to_base_id.get(n)
                if not bid:
                    errors.append((line_num, f"юнит не найден: {n!r}"))
                    line_ok = False
                    continue
                pool_base_ids.append(bid)
                result.unit_display_names[bid] = n
            if not line_ok:
                continue
            result.unit_display_names[trigger_base_id] = trigger
            result.bundles.setdefault(trigger_base_id, []).extend(pool_base_ids)

    return result, errors


def describe_rules(parsed: ParsedRules) -> list:
    """Список строк на русском для отображения на /tb/platoons/filters — что реально
    применится, без сырых base_id/ally_code."""
    lines = []
    for ally_code in sorted(parsed.exclude_player_codes):
        lines.append(f"Исключён игрок: {parsed.exclude_player_names.get(ally_code, ally_code)}")
    for base_id in sorted(parsed.exclude_unit_ids):
        lines.append(f"Юнит не предлагается: {parsed.exclude_unit_names.get(base_id, base_id)}")
    for trigger_base_id, pool in parsed.bundles.items():
        trigger_name = parsed.unit_display_names.get(trigger_base_id, trigger_base_id)
        pool_names = [parsed.unit_display_names.get(b, b) for b in pool]
        lines.append(f"При донате «{trigger_name}» — приоритет тому же игроку на: {', '.join(pool_names)}")
    return lines
