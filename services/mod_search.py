"""Гибкий поиск по НАДЕТЫМ модам гильдии — по запросу NicolozZ (тред "Гайд по АС Боту",
#ас-задачи, 2026-09-10): в игре нет инструмента анализа модулей по своим критериям (сет +
слот + вторичка), только примитивная "модка". Аналог `/mods search` HotUtils, но не по
одному персонажу, а сразу по всей гильдии — и с несколькими условиями по вторичкам разом
(у HotUtils только одно).

Данные — уже закэшированные в player_unit_cache (database.get_player_units/
get_player_units_bulk) сырые rosterUnit-объекты игроков (тот же player_units_sync_loop,
что питает /статы) — БЕЗ дополнительных обращений к Comlink, поиск по гильдии не должен
делать 50-90 живых запросов на каждый вызов команды.

Формат сырого мода — Comlink equippedStatMod (см. swgoh_comlink.StatCalc.calculator.
_calculate_mod_stats, установленный пакет): definitionId — строка из 3 цифр, [0]=id сета,
[1]=редкость (пипсы) — ОБА уже используются существующим кодом бота для расчёта статов
через тот же definitionId, так что достоверно верны. Цифра слота — [2] — ИЗНАЧАЛЬНО (до
2026-09-11) была взята "общеизвестная" конвенция 2-7 (Square=2..Cross=7, как у
swgoh_comlink.helpers.MOD_SLOTS) БЕЗ проверки на живых данных — и она оказалась НЕВЕРНОЙ:
после жалобы пользователя "показывает 0 модов на всё подряд" (при фильтре по слоту)
прогнали реальные 91349 модов гильдии через эмпирическую сверку (см. чат-сессию
2026-09-11) — для каждого мода с ОДНОЗНАЧНО-слото-специфичным primary-статом (Speed/
Accuracy%/CritAvoidance% бывают только на Arrow, CritDamage%/CritChance% только на
Triangle, Potency%/Tenacity% только на Cross — см. stat_engine.MOD_PRIMARY_OPTIONS)
проверили, какое значение definitionId[2] у него ФАКТИЧЕСКИ стоит: оказалось 100%-но
детерминированно 1=Square, 2=Arrow, 3=Diamond, 4=Triangle, 5=Circle, 6=Cross (Square/
Diamond/Circle довыведены через исключение — единственный оставшийся возможный слот для
Offense%/Defense%/Health%+Protection% соответственно). Простая последовательная нумерация
1-6 в порядке MOD_SLOT_DEFS — никакой связи с swgoh_comlink.helpers.MOD_SLOTS (та таблица,
судя по всему, про другое поле/контекст, не про equippedStatMod.definitionId). Самопроверка
decode_mod ниже (сверка декодированного слота со списком допустимых primary для него)
осталась как страховка на случай, если Comlink когда-нибудь поменяет формат.

primaryStat/secondaryStat.stat.unscaledDecimalValue — то же кодирование, что уже
задокументировано в stat_engine.MOD_PRIMARY_OPTIONS: разделить на 1e8, а для %-статов
результат домножить на 100 (т.е. итоговое деление на 1e6) — выведено из тех же реальных
примеров (Offense+8.5% <-> "8500000", Speed+32 <-> "3200000000")."""

import logging
import operator as _operator

import stat_engine
from services.mod_optimizer import STAT_NAME_TO_IDS

logger = logging.getLogger(__name__)

# id слота в definitionId[2] -> ключ формы; те же ключи/подписи и тот же порядок, что
# MOD_SLOT_DEFS в web/routes/stat_builder.py (не импортируем оттуда — web-слой зависит от
# services, не наоборот, — но это ровно та же классификация форм модов).
SLOT_CHOICES = [
    ("square", "Квадрат"), ("arrow", "Стрела"), ("diamond", "Ромб"),
    ("triangle", "Треугольник"), ("circle", "Круг"), ("cross", "Крест"),
]
SLOT_ID_TO_KEY = {1: "square", 2: "arrow", 3: "diamond", 4: "triangle", 5: "circle", 6: "cross"}
SLOT_KEY_TO_LABEL = dict(SLOT_CHOICES)

# unit_stat_id (первичный или вторичный стат мода) -> (отображаемое имя, это %-вариант или нет).
# STAT_NAME_TO_IDS — {имя: (flat_id, percent_id)}, сверено на реальном HotUtils-экспорте
# 2026-09-01 (см. services/mod_optimizer.py) — тут просто разворачиваем в обратную сторону,
# не передираем константы заново.
STAT_ID_TO_INFO: dict[int, tuple[str, bool]] = {}
for _name, (_flat_id, _pct_id) in STAT_NAME_TO_IDS.items():
    if _flat_id is not None:
        STAT_ID_TO_INFO[_flat_id] = (_name, False)
    if _pct_id is not None:
        STAT_ID_TO_INFO[_pct_id] = (f"{_name} %", True)

# Для выпадашек — стабильно отсортировано по имени.
STAT_CHOICES = sorted(((sid, name, is_pct) for sid, (name, is_pct) in STAT_ID_TO_INFO.items()), key=lambda t: t[1])

# Только >=/<=/= — то же множество операторов, что в cogs/stat_requirements.py
# (OPERATOR_CHOICES), чтобы не плодить второй словарь с другим набором знаков.
OPERATORS = {">=": _operator.ge, "<=": _operator.le, "=": _operator.eq}

_slot_mismatch_warned: set[str] = set()


def _decode_stat(stat_block) -> dict | None:
    if not stat_block:
        return None
    stat_id = stat_block.get("unitStatId")
    raw = stat_block.get("unscaledDecimalValue")
    if stat_id is None or raw is None:
        return None
    try:
        raw = float(raw)
    except (TypeError, ValueError):
        return None
    name, is_percent = STAT_ID_TO_INFO.get(stat_id, (f"#{stat_id}", False))
    value = raw / 1e8
    if is_percent:
        value *= 100
    return {"stat_id": stat_id, "name": name, "is_percent": is_percent, "value": value}


def decode_mod(mod: dict) -> dict | None:
    """Сырой equippedStatMod -> {set_id, rarity, level, slot_key, slot_label,
    primary: decoded_stat|None, secondaries: [decoded_stat, ...]}. None — мод без
    definitionId/нечитаемый (повреждённые данные, не должно встречаться, но не роняем поиск)."""
    definition_id = mod.get("definitionId")
    if not definition_id or len(definition_id) < 3:
        return None
    try:
        set_id = int(definition_id[0])
        rarity = int(definition_id[1])
        slot_id = int(definition_id[2])
    except ValueError:
        return None
    slot_key = SLOT_ID_TO_KEY.get(slot_id)

    primary = _decode_stat((mod.get("primaryStat") or {}).get("stat"))
    secondaries = [
        d for d in (_decode_stat((s or {}).get("stat")) for s in (mod.get("secondaryStat") or []))
        if d is not None
    ]

    if slot_key and primary and slot_key not in _slot_mismatch_warned:
        allowed = {opt["unit_stat"] for opt in stat_engine.MOD_PRIMARY_OPTIONS.get(slot_key, [])}
        if allowed and primary["stat_id"] not in allowed:
            _slot_mismatch_warned.add(slot_key)
            logger.warning(
                "mod_search.decode_mod: primary-стат %r не входит в известный список для слота %r "
                "(definitionId=%r) — конвенция decode слота из definitionId[2] может быть неверна, "
                "см. docstring services/mod_search.py",
                primary["name"], slot_key, definition_id,
            )

    return {
        "set_id": set_id, "rarity": rarity, "level": mod.get("level"),
        "slot_key": slot_key, "slot_label": SLOT_KEY_TO_LABEL.get(slot_key, f"#{slot_id}"),
        "primary": primary, "secondaries": secondaries,
    }


def mod_matches(decoded: dict, set_ids=None, slot_keys=None, primary_stat_ids=None, conditions=None) -> bool:
    """set_ids/slot_keys/primary_stat_ids — списки (по фидбеку 2026-09-11: несколько
    значений в каждой категории, ИЛИ внутри категории — "сет 2 или сет 3"), пустой/None
    список категории значит "любой". Категории между собой и с conditions — через И.
    conditions — [(stat_id, operator_str, value), ...]; каждое условие ищет СВОЮ вторичку
    среди до 4 на моде (одно и то же условие не может быть закрыто одной и той же
    вторичкой дважды, но одна и та же вторичка может закрыть только одно условие за раз —
    на практике с ≤4 вторичек и обычно 1-3 условиями это не проблема)."""
    if set_ids and decoded["set_id"] not in set_ids:
        return False
    if slot_keys and decoded["slot_key"] not in slot_keys:
        return False
    if primary_stat_ids and (not decoded["primary"] or decoded["primary"]["stat_id"] not in primary_stat_ids):
        return False
    for stat_id, op, value in (conditions or []):
        op_fn = OPERATORS.get(op)
        if op_fn is None:
            continue
        if not any(s["stat_id"] == stat_id and op_fn(s["value"], value) for s in decoded["secondaries"]):
            return False
    return True


def search_units(units: dict, **filt) -> list:
    """units — {base_id: unit_dict} (database.get_player_units(ally_code)). Возвращает
    [{base_id, mod: decoded}, ...] по всем модам этого игрока, прошедшим фильтр."""
    matches = []
    for base_id, unit in units.items():
        for mod in unit.get("equippedStatMod") or []:
            decoded = decode_mod(mod)
            if decoded and mod_matches(decoded, **filt):
                matches.append({"base_id": base_id, "mod": decoded})
    return matches


def fmt_value(value: float) -> str:
    if value == int(value):
        return str(int(value))
    return f"{value:.2f}".rstrip("0").rstrip(".")


def describe_mod(base_id: str, decoded: dict, char_label) -> str:
    """Одна строка отчёта: персонаж — слот, сет, primary, вторички. char_label — либо
    готовая строка, либо callable(base_id) (см. database.get_game_unit_name) — вызывающая
    сторона решает, резолвить имя заранее (пачкой) или лениво."""
    name = char_label(base_id) if callable(char_label) else char_label
    set_name = stat_engine.MOD_SET_IDS.get(str(decoded["set_id"]), f"набор #{decoded['set_id']}")
    primary = decoded["primary"]
    primary_txt = f"{primary['name']} {fmt_value(primary['value'])}{'%' if primary['is_percent'] else ''}" if primary else "?"
    secondaries_txt = ", ".join(
        f"{s['name']} {fmt_value(s['value'])}{'%' if s['is_percent'] else ''}" for s in decoded["secondaries"]
    ) or "—"
    return f"**{name}** — {decoded['slot_label']} · {set_name} · primary: {primary_txt} · вторички: {secondaries_txt}"
