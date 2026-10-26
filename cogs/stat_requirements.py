import asyncio
import math
import re
from datetime import datetime
from zoneinfo import ZoneInfo

import disnake
from disnake.ext import commands, tasks

import database
import guild_resolver
import stat_engine
from services import activity_diff, feature_flags, mod_search, omicron_priority, unit_aliases
from tg_bot import outbox

MSK = ZoneInfo("Europe/Moscow")
from cogs.datacron_requirements import (
    PRIORITY_REQUIRED,
    PRIORITY_LABELS,
    PRIORITY_CHOICES,
    PRIORITY_EMOJI,
    DATACRON_LIST_COLOR,
    DATACRON_CHECK_COLOR_FULL,
    DATACRON_CHECK_COLOR_PARTIAL,
    DATACRON_CHECK_COLOR_NONE,
    _lines_to_embeds,
    _squad_label,
    squad_missing_reason_in_roster,
)

# Полный список названий статов у StatCalc (swgoh_comlink/helpers/_stat_data.py, таблица
# STATS) шире — здесь только те, что реально фигурируют в гильдийских билд-требованиях.
# "Relic" — не настоящий стат StatCalc, спец-значение для требования к уровню реликвии.
# ВАЖНО: "обобщённые" статы без Physical/Special (Critical Chance, Critical Avoidance,
# Accuracy, Offense, Defense) в финальном расчёте StatCalc НЕ существуют — final-словарь
# отдаёт только расщеплённые Physical.../Special... варианты, генерик-ключи всегда None
# (проверено эмпирически на реальном игроке). Такие пункты сюда не добавлять — требование
# с ними навсегда останется "нет данных".
STAT_CHOICES = [  # подписи — официальная русская локализация игры (как STAT_LABEL_SHORT)
    disnake.OptionChoice(name="Реликвия (уровень)", value="Relic"),
    disnake.OptionChoice(name="Здоровье + Защита", value="Total Life"),
    disnake.OptionChoice(name="Здоровье", value="Health"),
    disnake.OptionChoice(name="Защита", value="Protection"),
    disnake.OptionChoice(name="Скорость", value="Speed"),
    disnake.OptionChoice(name="Физический урон", value="Physical Damage"),
    disnake.OptionChoice(name="Особый урон", value="Special Damage"),
    disnake.OptionChoice(name="Броня", value="Armor"),
    disnake.OptionChoice(name="Сопротивление", value="Resistance"),
    disnake.OptionChoice(name="Эффективность", value="Potency"),
    disnake.OptionChoice(name="Стойкость", value="Tenacity"),
    disnake.OptionChoice(name="Критический урон", value="Critical Damage"),
    disnake.OptionChoice(name="Шанс физ. крит. атаки", value="Physical Critical Chance"),
    disnake.OptionChoice(name="Шанс особ. крит. атаки", value="Special Critical Chance"),
    disnake.OptionChoice(name="Избег. физ. крит. удара", value="Physical Critical Avoidance"),
    disnake.OptionChoice(name="Избег. особ. крит. удара", value="Special Critical Avoidance"),
    disnake.OptionChoice(name="Физическая точность", value="Physical Accuracy"),
    disnake.OptionChoice(name="Особая точность", value="Special Accuracy"),
    disnake.OptionChoice(name="Кража здоровья", value="Health Steal"),
]

# Плоская (не растущая с реликвией) часть порога для /статы_релик — значения и сама модель
# взяты из гильдийской Google-таблицы (лист TEST, формула BASESTAT(...)*MODMULT(...)+flat):
# порог считается как flat_offset (роллы с модов, не масштабируются) + остаток порога,
# который масштабируется пропорционально росту голой базы стата между релик-уровнями.
# Статов без записи здесь (Speed, Potency, Tenacity, крит-статы и т.п.) в таблице тоже нет —
# для них норма при пересчёте остаётся как есть, без масштабирования.
RELIC_PROJECTION_FLAT_OFFSET = {
    "Health": 1500,
    "Protection": 3000,
    "Total Life": 4500,  # Health flat + Protection flat — см. _with_total_life
    "Physical Damage": 100,
    "Special Damage": 100,
    "Armor": 20,
}

# Спец-значение stat_name (как "Relic") для требования "у персонажа разблокирован омикрон X" —
# см. /статы_требования добавить_омикрон. operator/threshold_value для таких строк всегда
# захардкожены как ">="/1.0 (сам факт разблокировки как булево 1/0, см. _evaluate_character_player),
# а КАКОЙ именно омикрон — в отдельной колонке skill_id (персонаж может иметь их несколько).
STAT_OMICRON = "Omicron"

# Спец-значение stat_name для требования "на слоте мода X стоит основа Y" — заметка Коли
# в Discord-треде "Гайд по АС Боту" 2026-09-20: "проверяем, что на стрелке скорость, на
# треугольнике крит.урон". operator захардкожен как "=" (на слоте либо стоит ровно та
# основа, либо нет — не "больше/меньше"), threshold_value — unit_stat_id основы (см.
# stat_engine.MOD_PRIMARY_OPTIONS), а КАКОЙ именно слот — в колонке mod_slot (square/arrow/
# diamond/triangle/circle/cross). Отдельная колонка, а не skill_id — семантика другая
# (skill_id — идентификатор способности, mod_slot — форма слота мода).
STAT_MOD_PRIMARY = "ModPrimary"

# Спец-значение stat_name для требования "надет сет X (нужным числом деталей)" — запрошено
# пользователем 2026-09-21: "мы так делали в конструкторе" (см. web/routes/stat_builder.py —
# тот же справочник сетов, что там). operator захардкожен как ">=" (то же булево "есть/нет
# бонуса сета", что и у Omicron), threshold_value — числовой set_id (1-8, см.
# stat_engine.MOD_SET_IDS/swgoh_comlink.helpers.MOD_SET_IDS). Нужное число деталей на сет —
# НЕ параметр требования (сколько штук нужно для бонуса — фиксированное игровое правило,
# stat_engine.MOD_SET_PIECE_COUNT, 2 либо 4 — офицер выбирает только САМ сет, не количество).
STAT_SET = "ModSet"

# Сеты с условием на СПИСОК (перенесены из HotUtils 2026-09-28, семантика проверена прогоном
# templates/execute на живых игроках): ModSetAny = «Mod Set Of» — надет хотя бы один полный
# комплект любого сета из списка; ModSetOnly = «Mod Set Only» — все надетые полные комплекты
# только из списка (и хотя бы один есть). Список set_id — в skill_id через запятую,
# operator ">=", threshold_value 1.0 (булева цель, как у Omicron).
STAT_SET_ANY = "ModSetAny"
STAT_SET_ONLY = "ModSetOnly"
MOD_SET_STATS = (STAT_SET, STAT_SET_ANY, STAT_SET_ONLY)
# Все «модовые» типы требований — оператор/значение у них не редактируются.
MOD_REQ_STATS = (STAT_MOD_PRIMARY,) + MOD_SET_STATS


def parse_id_list(raw) -> list[int]:
    """"55,56" → [55, 56] — список id в skill_id у ModPrimary (варианты основы «или»)
    и ModSetAny/ModSetOnly (список сетов)."""
    return [int(p) for p in str(raw or "").split(",") if p.strip().isdigit()]


def mod_primary_allowed(threshold, skill_id) -> list[int]:
    """Допустимые основы ModPrimary-строки: список из skill_id (если основ несколько, «или»),
    иначе единственная — threshold_value."""
    return parse_id_list(skill_id) or [int(threshold)]

# {int(set_id): название} — официальный русский перевод игры (просьба гильдии 2026-10-19:
# «чего мы на пендосском»). Импорт HotBot сверяет английские имена по своим словарям.
_SET_NAME_BY_ID = stat_engine.MOD_SET_NAMES_RU  # RUS_RU STATMODSETBONUS_*_NAME
SET_CHOICES = [
    disnake.OptionChoice(name=name, value=set_id)
    for set_id, name in sorted(_SET_NAME_BY_ID.items(), key=lambda kv: kv[1])
]

# Локализованные подписи форм слотов — независимая копия web/routes/stat_builder.py::
# MOD_SLOT_DEFS (cogs/ не может подтягивать код из web/, см. _omicron_options_for_base),
# сам список форм и легальных primary на форму — общий stat_engine.MOD_PRIMARY_OPTIONS.
MOD_SLOT_LABELS = {
    "square": "Квадрат",
    "arrow": "Стрела",
    "diamond": "Ромб",
    "triangle": "Треугольник",
    "circle": "Круг",
    "cross": "Крест",
}
OPERATOR_CHOICES = [
    disnake.OptionChoice(name=">=", value=">="),
    disnake.OptionChoice(name="<=", value="<="),
    disnake.OptionChoice(name="=", value="="),
]
# Для сравнения с другим персонажем — плюс строгие > и < ("офицер быстрее Хакса на +51":
# стат > стат_сравниваемого + 51). Для обычных числовых порогов строгие операторы не
# заводили — там порог и так задаётся числом, ">= N+1" покрывает то же самое.
COMPARE_OPERATOR_CHOICES = [
    disnake.OptionChoice(name=">", value=">"),
    disnake.OptionChoice(name=">=", value=">="),
    disnake.OptionChoice(name="<", value="<"),
    disnake.OptionChoice(name="<=", value="<="),
    disnake.OptionChoice(name="=", value="="),
]
COMPARE_OPERATORS = {c.value for c in COMPARE_OPERATOR_CHOICES}

# Требование можно привязать к одной из двух "схем" мод-билда персонажа (обсуждение с Колей,
# 2026-09-21: "2 карточки перса - одна в одном билде, другая в другом"; матчинг по совпавшим
# основам, при равенстве или отсутствии данных — всегда схема 1). value="" — требование общее
# для обеих схем (тот же смысл, что и NULL в stat_requirements.scheme_num, единственный режим
# до этой фичи). Человекочитаемые названия схем ("Скорость"/"КД" и т.п.) — отдельно, per
# персонаж, в stat_plate_character_schemes/схема_переименовать; тут только номер слота.
SCHEME_CHOICES = [
    disnake.OptionChoice(name="Оба варианта", value=""),
    disnake.OptionChoice(name="Вариант 1", value="1"),
    disnake.OptionChoice(name="Вариант 2", value="2"),
]
# Для /статы/статы_релик — форсирует, под какую схему проверять/пересчитывать, вместо
# авто-определения (значение "" = авто).
FORCE_SCHEME_CHOICES = [
    disnake.OptionChoice(name="Авто (по фактическим модулям игрока)", value=""),
    disnake.OptionChoice(name="Вариант 1", value="1"),
    disnake.OptionChoice(name="Вариант 2", value="2"),
]


def _parse_scheme_param(value: str | None) -> int | None:
    return int(value) if value else None

# Три сценария сравнения реального билда/модов игрока с нормой плейта — влияют и на
# одиночный "Итог" (failed_required), и на гильдийский compliant/problem (см.
# _evaluate_character_player/_build_guild_report). Раньше был один булев параметр
# "учитывать_релик", который (а) не читался вообще на одиночном игроке и (б) даже в
# гильдии двигал только показанную дробь matched/total, а не сам факт прохождения —
# сам "Итог" всегда фактически считал SCENARIO_FULL, что не совпадало ни с ХБ (сырое
# сравнение), ни с "поднять релик только тем, кто ниже цели" — оба варианта теперь
# отдельные явные сценарии.
SCENARIO_RAW = "raw"
SCENARIO_UP = "up"
SCENARIO_FULL = "full"

SCENARIO_CHOICES = [
    disnake.OptionChoice(name="Как сейчас, без проекции (как в ХБ)", value=SCENARIO_RAW),
    disnake.OptionChoice(name="Релик вверх тем, кто ниже цели плейта", value=SCENARIO_UP),
    disnake.OptionChoice(name="Полный подгон релика (вверх и вниз)", value=SCENARIO_FULL),
]

# Короткие подписи для колонки "Стат" в таблице /статы — полные названия (особенно
# Physical/Special Damage, Critical Chance/Avoidance) не влезают в ширину на мобильных.
# Health/Speed/Armor/Potency и т.п. уже достаточно короткие — оставлены как есть (fallback).
STAT_LABEL_SHORT = {
    "Relic": "Реликвия",
    "Total Life": "Здоровье+Защита",
    "Speed": "Скорость",
    "Health": "Здоровье",
    "Protection": "Защита",
    "Armor": "Броня",
    "Resistance": "Сопротивление",
    "Potency": "Эффективность",
    "Tenacity": "Стойкость",
    "Physical Damage": "Физический урон",
    "Special Damage": "Особый урон",
    "Critical Damage": "Критический урон",
    "Critical Chance": "Шанс на крит. удар",
    "Physical Critical Chance": "Шанс физ. крит. атаки",
    "Special Critical Chance": "Шанс особ. крит. атаки",
    "Critical Avoidance": "Избегание крит. удара",
    "Physical Critical Avoidance": "Избег. физ. крит. удара",
    "Special Critical Avoidance": "Избег. особ. крит. удара",
    "Accuracy": "Точность",
    "Physical Accuracy": "Физическая точность",
    "Special Accuracy": "Особая точность",
    "Health Steal": "Кража здоровья",
    "Offense": "Атака",
    "Defense": "Оборона",
}


# =====================================================================
# Мелкие хелперы (парсинг "Имя [BASE_ID]", форматирование, сравнение) —
# та же схема, что unit_match в cogs/tasks.py и _parse_leading_hash_int в datacron_requirements.py
# =====================================================================
def _parse_bracket_id(text: str) -> str:
    m = re.search(r"\[(.*?)\]", text or "")
    return m.group(1) if m else (text or "").strip()


def _unit_display_name(base_id: str) -> str:
    return database.get_game_unit_name(base_id) or base_id


_EMOJI_SHORTCODE_RE = re.compile(r":([a-zA-Z0-9_~]+):")


def _resolve_emoji_shortcodes(text: str, guild) -> str:
    """Заменяет `:name:` в тексте фразы на реальный кастомный эмодзи гильдии
    (`<:name:id>`), если такой эмодзи там есть. Discord API не делает это
    автоматически (в отличие от клиента, где автоподстановка при наборе
    происходит на стороне пользователя ДО отправки) — офицеры вводят фразы
    как текст в /омикрон_текст или на веб-странице /omicron/phrases и без
    этой замены `:priyatno:` уходит в канал буквами, а не самим эмодзи."""
    if not text or guild is None or not guild.emojis:
        return text
    by_name = {e.name: e for e in guild.emojis if e.available}
    if not by_name:
        return text
    return _EMOJI_SHORTCODE_RE.sub(lambda m: str(by_name[m.group(1)]) if m.group(1) in by_name else m.group(0), text)


def _fmt_value(value: float) -> str:
    return f"{value:g}"


def _fmt_compact(value: float) -> str:
    """Сокращённая запись для ширины таблицы: 8472 -> '8,4к' (отбрасывание, не округление),
    8000 -> '8к'. Значения меньше 1000 (релик, скорость, статы-проценты...) не сокращаются
    буквой "к", но тоже режутся до 1 знака после запятой без округления — иначе проценты
    вроде Potency/Armor вылезают как "106.509"/"72.2766" (StatCalc считает их с большой
    точностью, см. PERCENT_STATS в stat_engine.py)."""
    if abs(value) >= 1000:
        truncated = math.trunc(value / 100) / 10
        text = f"{truncated:.1f}".replace(".", ",")
        if text.endswith(",0"):
            text = text[:-2]
        return text + "к"
    truncated = math.trunc(value * 10) / 10
    if truncated == math.trunc(truncated):
        return str(int(truncated))
    return f"{truncated:.1f}".replace(".", ",")


def _compare(current: float, operator: str, threshold: float) -> bool:
    if operator == ">=":
        return current >= threshold
    if operator == "<=":
        return current <= threshold
    if operator == ">":
        return current > threshold
    if operator == "<":
        return current < threshold
    if operator == "=":
        return current == threshold
    return False


def _with_total_life(values: dict) -> dict:
    """Добавляет синтетический стат "Total Life" = Health+Protection в результат
    calc_final_stats — сам StatCalc такого стата не считает (заметка Коли в Discord-треде
    "Гайд по АС Боту", 2026-09-20: "хочу total life, если ещё нет — на своё усмотрение",
    сумма факт. HP+защита показалась самым простым и понятным вариантом)."""
    health = values.get("Health")
    protection = values.get("Protection")
    if health is not None and protection is not None:
        values["Total Life"] = health + protection
    return values


def _build_synthetic_unit(base_id: str, relic_level: int) -> dict:
    """Без модов/шмота конкретного игрока: макс. редкость/уровень/шестерня + бонус реликвии.
    Используется только когда команда вызвана без игрока (абстрактный лукап)."""
    return {
        "defId": base_id,
        "rarity": 7,
        "level": 85,
        "gear": 13,
        "equipped": [],
        "skills": [],
        "relic": {"currentTier": relic_level + 2},
    }


async def _get_unit_for_player(bot, ally_code: str, base_id: str, force_refresh: bool):
    """Возвращает (unit_dict, updated_at) либо (None, None)."""
    if force_refresh:
        # Один запрос на весь плейт: следующие персонажи попадают в кулдаун и читают кэш.
        await activity_diff.refresh_player_units(bot.comlink, ally_code)
    cached = database.get_player_unit(ally_code, base_id)
    if cached:
        return cached
    return None, None


def _build_table(headers: list, table_rows: list) -> str:
    """Моноширинная таблица в code-block'е (Discord embed'ы не умеют настоящие таблицы)."""
    widths = [len(h) for h in headers]
    for row in table_rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(cell))

    def fmt_row(cells):
        return "  ".join(cell.ljust(widths[i]) for i, cell in enumerate(cells))

    lines = [fmt_row(headers), fmt_row(["-" * w for w in widths])]
    lines.extend(fmt_row(row) for row in table_rows)
    return "```\n" + "\n".join(lines) + "\n```"


_CODE_PAIR_NUMBERS_RE = re.compile(r" \+\d+(?:[.,]\d+)?%?| \(\d+ модуль\.\)")


def _code_pairs(pairs: list) -> str:
    """Основы/сеты — «название  состояние» в code-block'е, как таблица статов, но без
    заголовков; левая колонка выравнивается, если не слишком длинная (мобильный Discord).
    Без цифр основ («Health +16%» → «Health») и «(4 мод.)» (RicardoOneS, 2026-10-19);
    «×2» у сетов остаётся — это число комплектов."""
    pairs = [(_CODE_PAIR_NUMBERS_RE.sub("", left), _CODE_PAIR_NUMBERS_RE.sub("", right)) for left, right in pairs]
    width = max((len(left) for left, _ in pairs if len(left) <= 16), default=0)
    lines = [f"{left.ljust(width)}  {right}" for left, right in pairs]
    return "```\n" + "\n".join(lines) + "\n```"


def _stat_label(stat_name: str, priority: str) -> str:
    label = STAT_LABEL_SHORT.get(stat_name, stat_name)
    return f"{label}*" if priority in ("optional", "useful") else label


def _omicron_options_for_base(base_id: str) -> list:
    """[(skill_id, label), ...] — омикроны конкретного персонажа/корабля, для автодополнения
    и валидации в /статы_требования добавить_омикрон. Та же логика, что
    web/routes/tasks.py::_omicron_options — независимая локальная копия (cogs/ не может
    подтягивать код из web/), справочник (unit_omicron_skills/skill_tier_thresholds) общий."""
    skill_ids = database.get_all_unit_omicron_skills().get(base_id, [])
    if not skill_ids:
        return []
    info = database.get_skill_display_info(skill_ids)
    options = []
    for skill_id in skill_ids:
        name, _ability_id, ability_type, omicron_mode = info.get(skill_id, (skill_id, None, None, None))
        label = name or skill_id
        extra = " / ".join(p for p in (ability_type, omicron_mode) if p)
        if extra:
            label += f" ({extra})"
        options.append((skill_id, label))
    return options


def _omicron_ability_label(skill_id: str) -> str:
    info = database.get_skill_display_info([skill_id])
    name, _ability_id, _ability_type, _omicron_mode = info.get(skill_id, (None, None, None, None))
    return name or skill_id


def _omicron_label(skill_id: str, priority: str) -> str:
    label = f"Омикрон: {_omicron_ability_label(skill_id)}"
    return f"{label}*" if priority in ("optional", "useful") else label


def _mod_primary_option(slot_key: str, unit_stat_id) -> dict | None:
    for opt in stat_engine.MOD_PRIMARY_OPTIONS.get(slot_key, []):
        if opt["unit_stat"] == unit_stat_id:
            return opt
    return None


_PRIMARY_STAT_RU = stat_engine.UNIT_STAT_RU  # официальные RUS_RU UnitStat_*; значения основ (+16% и т.п.) в /статы не пишем


def _mod_primary_stat_label(mod_slot: str, unit_stat_id) -> str:
    opt = _mod_primary_option(mod_slot, unit_stat_id)
    if not opt:
        return f"#{unit_stat_id}"
    return _PRIMARY_STAT_RU.get(opt["unit_stat"], opt["label"])


def _mod_primary_req_text(mod_slot: str, unit_stat_id, skill_id=None) -> str:
    allowed = mod_primary_allowed(unit_stat_id, skill_id)
    stat_label = " или ".join(_mod_primary_stat_label(mod_slot, s) for s in allowed)
    return f"{MOD_SLOT_LABELS.get(mod_slot, mod_slot)}: {stat_label}"


def _mod_primary_label(mod_slot: str, priority: str) -> str:
    label = f"Основа: {MOD_SLOT_LABELS.get(mod_slot, mod_slot)}"
    return f"{label}*" if priority in ("optional", "useful") else label


def _compare_label(stat_name: str, compare_character_key: str, priority: str) -> str:
    label = f"{_stat_label(stat_name, '')} vs {_unit_display_name(compare_character_key)}"
    return f"{label}*" if priority in ("optional", "useful") else label


def _compare_offset_sign(operator: str) -> int:
    """Отрыв ("на сколько") у сравнения с персонажем хранится в threshold_value как
    неотрицательное число, направление берётся из оператора: для >/>=/= — стат сравниваемого
    ПЛЮС отрыв ("быстрее на 51"), для </<= — МИНУС ("медленнее на 20")."""
    return -1 if operator in ("<", "<=") else 1


def _compare_threshold(other_value: float, operator: str, offset: float) -> float:
    return other_value + _compare_offset_sign(operator) * (offset or 0.0)


def _compare_offset_text(operator: str, offset: float) -> str:
    if not offset:
        return ""
    return f" {'−' if _compare_offset_sign(operator) < 0 else '+'} {_fmt_value(offset)}"


def _compare_req_text(stat_name: str, operator: str, compare_character_key: str, offset: float = 0.0) -> str:
    return f"{_stat_label(stat_name, '')} {operator} {_unit_display_name(compare_character_key)}{_compare_offset_text(operator, offset)}"


def _player_mod_primaries(unit: dict) -> dict:
    """{slot_key: unit_stat_id} — primary-стат, реально стоящий сейчас на каждом слоте
    мода у этого юнита (services.mod_search.decode_mod — тот же decode, что и
    /моды_поиск). Слот без экипированного мода просто не попадает в словарь."""
    primaries = {}
    for mod in unit.get("equippedStatMod") or []:
        decoded = mod_search.decode_mod(mod)
        if decoded and decoded.get("slot_key") and decoded.get("primary"):
            primaries[decoded["slot_key"]] = decoded["primary"]["stat_id"]
    return primaries


def _player_mod_set_counts(unit: dict) -> dict:
    """{set_id: количество надетых модов этого сета} — по всем 6 слотам разом (в отличие от
    _player_mod_primaries, сет не привязан к конкретному слоту). Тот же decode_mod, что и
    /моды_поиск/_player_mod_primaries."""
    counts: dict[int, int] = {}
    for mod in unit.get("equippedStatMod") or []:
        decoded = mod_search.decode_mod(mod)
        if decoded and decoded.get("set_id"):
            counts[decoded["set_id"]] = counts.get(decoded["set_id"], 0) + 1
    return counts


def _mod_set_label(set_id, priority: str) -> str:
    label = f"Сет: {_SET_NAME_BY_ID.get(int(set_id), set_id)}"
    return f"{label}*" if priority in ("optional", "useful") else label


def _mod_set_req_text(set_id, sets_count=1) -> str:
    """"Health ×2 (4 мода)" — sets_count комплектов (threshold_value у ModSet-строки,
    исторически всегда 1.0), в скобках — сколько модулей из 6 это занимает."""
    set_id = int(set_id)
    count = max(int(sets_count or 1), 1)
    pieces = stat_engine.MOD_SET_PIECE_COUNT.get(set_id, 2)
    name = _SET_NAME_BY_ID.get(set_id, set_id)
    times = f" ×{count}" if count > 1 else ""
    return f"{name}{times} ({pieces * count} мод.)"


def _complete_sets(mod_set_counts: dict, set_id: int) -> int:
    """Сколько полных комплектов сета set_id надето (2 шт. Health ×4 модуля = 2)."""
    return mod_set_counts.get(set_id, 0) // stat_engine.MOD_SET_PIECE_COUNT.get(set_id, 2)


def _worn_complete_set_ids(mod_set_counts: dict) -> list[int]:
    return [sid for sid in sorted(mod_set_counts) if _complete_sets(mod_set_counts, sid) > 0]


def _set_list_ok(stat_name: str, skill_id, mod_set_counts: dict) -> bool:
    """ModSetAny — хотя бы один полный комплект из списка; ModSetOnly — все надетые полные
    комплекты из списка и хотя бы один надет (семантика HotUtils «Mod Set Of»/«Mod Set Only»)."""
    listed = set(parse_id_list(skill_id))
    worn = set(_worn_complete_set_ids(mod_set_counts))
    if stat_name == STAT_SET_ANY:
        return bool(worn & listed)
    return bool(worn) and worn <= listed


def _set_list_names(skill_id) -> str:
    return " / ".join(str(_SET_NAME_BY_ID.get(sid, sid)) for sid in parse_id_list(skill_id))


def _mod_set_list_req_text(stat_name: str, skill_id) -> str:
    prefix = "один из" if stat_name == STAT_SET_ANY else "только"
    return f"{prefix}: {_set_list_names(skill_id)}"


def _mod_set_list_label(stat_name: str, priority: str) -> str:
    label = "Сет: один из" if stat_name == STAT_SET_ANY else "Сеты: только"
    return f"{label}*" if priority in ("optional", "useful") else label


def _worn_sets_text(mod_set_counts: dict) -> str:
    parts = []
    for sid in _worn_complete_set_ids(mod_set_counts):
        n = _complete_sets(mod_set_counts, sid)
        parts.append(f"{_SET_NAME_BY_ID.get(sid, sid)}{f' ×{n}' if n > 1 else ''}")
    return ", ".join(parts) or "без сетов"


def _resolve_scheme(rows: list, mod_primaries: dict, mod_set_counts: dict, forced_scheme: int | None,
                     plate_name: str, base_id: str, guild_id: int):
    """Возвращает (active_scheme, scheme_label, is_forced) для персонажа, у которого могут
    быть строки с scheme_num (см. SCHEME_CHOICES выше). active_scheme=None, если у персонажа
    схем нет вовсе — тогда фильтрация по схеме в _evaluate_character_player полностью
    отключена (обычное поведение, как до этой фичи).

    Авто-детект (forced_scheme не задан), по явному решению пользователя 2026-09-21 —
    ДВУХСТУПЕНЧАТЫЙ, сеты приоритетнее основ (проверено первым, решает, если различает схемы):
    1. Сеты: считаем, у скольких ModSet-строк каждой схемы реально надето достаточно деталей
       нужного сета (mod_set_counts >= stat_engine.MOD_SET_PIECE_COUNT — см.
       _player_mod_set_counts). Если счёт схем РАЗНЫЙ — активна схема с большим счётом,
       основы вообще не смотрим.
    2. Основы: только если счёт по сетам совпал (в т.ч. 0:0 — например, у персонажа вообще
       нет ModSet-строк, либо надетые сеты одинаково подходят обеим схемам) — тогда, как и
       раньше, считаем совпадения ModPrimary-строк с фактическими основами на слотах.
    Итоговая ничья (после обоих шагов) или полное отсутствие сигнала — по явному решению
    пользователя всегда схема 1."""
    schemes_present = any(r[14] for r in rows)
    if not schemes_present:
        return None, None, False

    scheme_leaf_plate = next((r[1] for r in rows if r[14]), plate_name)
    scheme_labels = database.get_character_scheme_labels(scheme_leaf_plate, base_id, guild_id)

    if forced_scheme in (1, 2):
        active_scheme = forced_scheme
        is_forced = True
    else:
        set_scores = {1: 0, 2: 0}
        for r in rows:
            if r[3] == STAT_SET and r[14] in (1, 2):
                if _complete_sets(mod_set_counts, int(r[11])) >= max(int(r[5] or 1), 1):
                    set_scores[r[14]] += 1
            elif r[3] in (STAT_SET_ANY, STAT_SET_ONLY) and r[14] in (1, 2):
                if _set_list_ok(r[3], r[11], mod_set_counts):
                    set_scores[r[14]] += 1

        if set_scores[1] != set_scores[2]:
            active_scheme = 1 if set_scores[1] > set_scores[2] else 2
        else:
            primary_scores = {1: 0, 2: 0}
            for r in rows:
                if r[3] == STAT_MOD_PRIMARY and r[14] in (1, 2):
                    actual = mod_primaries.get(r[12])
                    if actual is not None and actual in mod_primary_allowed(r[5], r[11]):
                        primary_scores[r[14]] += 1
            active_scheme = 1 if primary_scores[1] >= primary_scores[2] else 2
        is_forced = False

    scheme_label = scheme_labels.get(active_scheme, f"Вариант {active_scheme}")
    return active_scheme, scheme_label, is_forced


def progress_row_key(row) -> str:
    """Стабильный ключ строки требования для «Прогресса модинга» (services/mod_progress.py):
    не id строки (при переимпорте плейта он меняется), а что именно проверяется — плейт-
    источник, персонаж, стат, омикрон/сет/слот, с кем сравнение, схема, приоритет. Порог в
    ключ не входит: изменённая норма — та же строка с новой нормой. Приоритет входит, иначе
    required- и optional-строка по одному и тому же стату (например, "скорость >= 530
    обязательно" + "скорость >= 550 опционально") схлопывались бы в одну строку, и при снимке
    (services/mod_progress.py::take_snapshot, INSERT OR REPLACE по этому ключу) результат
    опциональной строки тихо перетирал бы результат обязательной — баг, отчего игрок с
    реально пройденным обязательным порогом показывался как не выполнивший его."""
    return "|".join(str(x or "") for x in (row[1], row[2], row[3], row[6], row[11], row[12], row[13], row[14]))


def progress_row_text(row) -> tuple[str, str]:
    """(подпись, норма) строки требования без привязки к игроку — для версий плейта в
    «Прогрессе модинга». Те же подписи, что в /статы."""
    _, _, _, stat_name, operator, threshold, priority, _, _, _, _, skill_id, mod_slot, compare_key, _ = row
    if stat_name in (STAT_SET_ANY, STAT_SET_ONLY):
        return _mod_set_list_label(stat_name, priority), _mod_set_list_req_text(stat_name, skill_id)
    if compare_key:
        return (_compare_label(stat_name, compare_key, priority),
                f"{operator} {_unit_display_name(compare_key)}{_compare_offset_text(operator, threshold or 0.0)}")
    if stat_name == STAT_MOD_PRIMARY:
        return _mod_primary_label(mod_slot, priority), _mod_primary_req_text(mod_slot, threshold, skill_id).split(": ", 1)[-1]
    if stat_name == STAT_OMICRON:
        return _omicron_label(skill_id, priority), "разблокирован"
    if stat_name == STAT_SET:
        return _mod_set_label(skill_id, priority), _mod_set_req_text(skill_id, threshold)
    return _stat_label(stat_name, priority), f"{operator} {_fmt_compact(threshold)}"


def _load_char_rows(plate_name: str, base_id: str, guild_id: int = 1, skip_plates=None):
    """Общий префикс для обоих режимов расчёта: сохранённые требования персонажа в плейте,
    его отображаемое имя, требуемый по плейту релик, комментарии и лёгенда "опционально".
    skip_plates — плейты-источники, чьих отрядов у игрока нет (их строки отбрасываются).
    Возвращает None, если для этого персонажа нет сохранённых требований."""
    rows = database.get_stat_requirements(plate_name, base_id, guild_id=guild_id)
    if skip_plates:
        rows = [r for r in rows if r[1] not in skip_plates]
    if not rows:
        return None
    char_name = _unit_display_name(base_id)
    relic_reqs = [r for r in rows if r[3] == "Relic"]
    required_relic = int(relic_reqs[0][5]) if relic_reqs else None
    comments = [r[8] for r in rows if r[8]]
    legend = " · * опционально" if any(r[6] in ("optional", "useful") for r in rows) else ""
    return rows, char_name, required_relic, comments, legend


def _missing_plate_squads(ally_code, guild_id: int, squads: dict | None = None) -> dict:
    """{плейт: причина} — отряды плейтов (database.get_stat_plate_squads), которых у игрока
    нет по кэшу ростера. Пусто, если отрядов не задано."""
    squads = database.get_stat_plate_squads(guild_id) if squads is None else squads
    if not squads:
        return {}
    roster = database.get_player_units(ally_code)
    missing = {}
    for plate, units in squads.items():
        reason = squad_missing_reason_in_roster(units, roster)
        if reason:
            missing[plate] = reason
    return missing


def _char_squad_skip(plate_name: str, base_id: str, guild_id: int, missing: dict):
    """(skip_plates, reason): плейты-источники строк персонажа без отряда у игрока; reason —
    если отброшены ВСЕ строки (персонаж игроку не требуется), иначе None."""
    if not missing:
        return set(), None
    leaves = {r[1] for r in database.get_stat_requirements(plate_name, base_id, guild_id=guild_id)}
    skip = leaves & set(missing)
    if leaves and skip == leaves:
        return skip, missing[sorted(skip)[0]]
    return skip, None


async def _evaluate_character_player(bot, plate_name: str, base_id: str, ally_code, force_refresh: bool, player_label, guild_id: int = 1, scenario: str = SCENARIO_RAW, forced_scheme: int | None = None, skip_plates=None, only_failed: bool = False, rows_out: list | None = None):
    """Возвращает (char_name, block, matched, total, updated_at, failed_required, required_total,
    active_scheme, scheme_label) для одного персонажа плейта у конкретного игрока — статы берутся
    из его реальных модов/шмота, прогноз на релик плейта. active_scheme/scheme_label — None,
    если у персонажа нет строк с scheme_num вовсе (обычный случай); иначе номер (1/2) и подпись
    схемы, которая реально использовалась для фильтрации строк — либо forced_scheme (если
    передан), либо авто-детект по фактическим основам модов игрока (см. _resolve_scheme выше).
    Строки чужой (неактивной) схемы полностью пропускаются — не попадают ни в таблицу, ни в
    matched/total/failed_required.
    прогноз на релик плейта. matched/total (ВСЕ приоритеты — required+optional+useful) и
    failed_required/required_total (только priority=="required") считаются по ОДНОМУ И ТОМУ ЖЕ
    критерию — какой именно, задаёт scenario (см. SCENARIO_RAW/UP/FULL выше), чтобы гильдийская
    дробь "X/Y" и решение compliant/problem/итоговый блок никогда не расходились между собой.
    required_total − len(failed_required) = сколько обязательных строк реально пройдено —
    используется гильдийским отчётом, чтобы показывать дробь именно по обязательным, а не по
    всем строкам плейта разом (иначе, если в плейте вперемешку 83 required+optional+useful
    строки, дробь "27/83" не даёт понять, сколько из непройденного — реально обязательное):
    - SCENARIO_RAW — сырое "как сейчас" сравнение (реальный текущий стат против порога плейта
      в лоб, без всякой проекции на релик) — то же самое, что делает ХБ.
    - SCENARIO_UP — билд/моды игрока проецируются на релик плейта, только если релик игрока
      НИЖЕ требуемого (тем, кто уже выше, релик вниз не опускается — их реальный стат просто
      берётся как есть); отвечает на вопрос "кто уже замодился и пройдёт, когда докачает релик".
    - SCENARIO_FULL — билд/моды проецируются на релик плейта в обе стороны (и вверх, и вниз) —
      то же самое, что показано в колонке "Релик N" детальной таблицы; отвечает на вопрос
      "кто хочет в целевом релике замодить персонажа как следует".
    Строка Relic сценарием не затрагивается вовсе (см. is_relic_row ниже) — её наличие/уровень
    либо есть у игрока прямо сейчас, либо нет, "спроецировать" его на другой релик бессмысленно.
    Omicron аналогично не проецируется, но в SCENARIO_UP строка с ним целиком исключается из
    "Итог"/matched-total (не считается ни пройденной, ни непройденной) — этот сценарий
    специально используется, чтобы смотреть билд/модуль в отрыве от факта разблокировки.
    Возвращает None, если для этого персонажа нет сохранённых требований (пропускается в отчёте).
    skip_plates — см. _load_char_rows (отбор по отрядам, _char_squad_skip).
    only_failed — в блок попадают только строки с ❌ (режим «только кресты» в /статы);
    подсчёты matched/total/failed_required от него не зависят.
    rows_out — если передан, сюда дописывается по строке на каждое проверенное требование:
    {"key", "value", "margin", "ok"} (снимки «Прогресса модинга», services/mod_progress.py)."""
    loaded = _load_char_rows(plate_name, base_id, guild_id, skip_plates=skip_plates)
    if loaded is None:
        return None
    rows, char_name, required_relic, comments, legend = loaded
    matched = 0
    total = 0
    required_total = 0
    failed_required = []

    unit, updated_at = await _get_unit_for_player(bot, ally_code, base_id, force_refresh)
    if not unit:
        block = f"⚠️ нет юнита у игрока «{player_label}» (не открыт либо ещё не синхронизирован)"
        return char_name, block, 0, 0, None, [], 0, None, None

    current_relic = stat_engine.get_current_relic_level(unit)
    current_values = _with_total_life(dict(stat_engine.calc_final_stats(bot.stat_calc, unit)))
    current_values["Relic"] = current_relic

    # Основы и сеты модов игрока читаются один раз тут (а не только внутри своих блоков
    # ModPrimary/ModSet ниже) — нужны заодно и для авто-детекта активной схемы
    # (_resolve_scheme), если у персонажа она есть.
    mod_primaries = _player_mod_primaries(unit)
    mod_set_counts = _player_mod_set_counts(unit)
    active_scheme, scheme_label, scheme_is_forced = _resolve_scheme(
        rows, mod_primaries, mod_set_counts, forced_scheme, plate_name, base_id, guild_id
    )

    # Омикрон-требования (stat_name==STAT_OMICRON) не входят в calc_final_stats — статус
    # "разблокирован" считается отдельно per skill_id (персонаж может иметь требования на
    # несколько разных омикронов сразу) и кладётся в тот же current_values под ключом
    # "Omicron:<skill_id>", чтобы дальше их можно было читать той же общей логикой _compare.
    omicron_skill_ids = [row[11] for row in rows if row[3] == STAT_OMICRON]
    if omicron_skill_ids:
        skill_tiers = {s.get("id"): s.get("tier") for s in (unit.get("skill") or [])}
        omicron_tiers = database.get_skill_omicron_tiers(omicron_skill_ids)
        for skill_id in omicron_skill_ids:
            omicron_tier = omicron_tiers.get(skill_id)
            current_tier = skill_tiers.get(skill_id)
            has_omicron = omicron_tier is not None and current_tier is not None and current_tier >= omicron_tier
            current_values[f"Omicron:{skill_id}"] = 1.0 if has_omicron else 0.0

    # ModPrimary-требования (stat_name==STAT_MOD_PRIMARY) — как и Omicron, не входят в
    # calc_final_stats: реальный primary-стат на слоте мода читается прямо из equippedStatMod
    # (mod_search.decode_mod, та же логика, что и /моды_поиск) и кладётся под ключом
    # "ModPrimary:<slot_key>" как unit_stat_id — сравнение оператором "=" с требуемым
    # unit_stat_id той же общей _compare-логикой.
    mod_slots_needed = {row[12] for row in rows if row[3] == STAT_MOD_PRIMARY}
    if mod_slots_needed:
        for slot_key in mod_slots_needed:
            actual_stat_id = mod_primaries.get(slot_key)
            if actual_stat_id is not None:
                current_values[f"ModPrimary:{slot_key}"] = float(actual_stat_id)

    # ModSet-требования (stat_name==STAT_SET) — как и Omicron, не входят в calc_final_stats:
    # "надет ли сет" считается булевым фактом (достаточно ли деталей сета реально
    # экипировано, stat_engine.MOD_SET_PIECE_COUNT) и кладётся под ключом "ModSet:<set_id>",
    # сравнение оператором ">=" с захардкоженным threshold_value=1.0 (тот же паттерн, что у
    # Omicron) — САМ set_id хранится в колонке skill_id (не в threshold_value, который тут
    # всегда 1.0 — булева цель, а не "какой сет"), см. stat_req_add_mod_set.
    # С 2026-09-28 значение — ЧИСЛО полных комплектов (а не 0/1), threshold_value — сколько
    # комплектов нужно (Health ×2 = 4 модуля); старые строки с 1.0 сравниваются как раньше.
    set_ids_needed = {int(row[11]) for row in rows if row[3] == STAT_SET}
    if set_ids_needed:
        for set_id in set_ids_needed:
            current_values[f"ModSet:{set_id}"] = float(_complete_sets(mod_set_counts, set_id))
    # ModSetAny/ModSetOnly — булев факт по списку сетов (1.0/0.0) под ключом "<stat>:<skill_id>".
    for row in rows:
        if row[3] in (STAT_SET_ANY, STAT_SET_ONLY):
            current_values[f"{row[3]}:{row[11]}"] = 1.0 if _set_list_ok(row[3], row[11], mod_set_counts) else 0.0

    # Показываем прогноз не только вверх (у игрока релик ниже требуемого), но и вниз
    # (у игрока уже выше — интересно, каким был бы стат ровно на уровне плейта).
    # Формула "Нужно" (порог минус дельта) не зависит от направления: дельта от релика
    # к релику при тех же модах/шмоте фиксирована в обе стороны.
    show_projection = required_relic is not None and required_relic != current_relic
    target_relic = required_relic if show_projection else current_relic
    projected_values = None
    if show_projection:
        projected_unit = stat_engine.project_unit_relic(unit, target_relic)
        projected_values = _with_total_life(dict(stat_engine.calc_final_stats(bot.stat_calc, projected_unit)))
        projected_values["Relic"] = target_relic

    # SCENARIO_UP: проекция только вверх — target_relic выше current_relic ровно тогда,
    # когда игрок ниже требуемого релика (см. show_projection/target_relic выше), так что
    # projected_values в этом случае УЖЕ является нужной "up"-проекцией, пересчитывать не
    # нужно; если же игрок на нужном релике или выше, up-значения — это просто его текущие
    # статы (релик вниз не опускаем).
    up_direction = show_projection and target_relic > current_relic
    up_values = projected_values if up_direction else current_values

    caption = f"Релик игрока: {current_relic}"
    if show_projection:
        if target_relic > current_relic:
            caption += f" → цель по плейту: {target_relic}"
        else:
            caption += f" → плейт требует {target_relic} (у игрока выше)"
    if active_scheme is not None:
        source = "вручную" if scheme_is_forced else "авто"
        caption += f" · Вариант: {scheme_label} ({source})"
    caption += legend
    if show_projection:
        headers = ["Стат", "Сейчас", "Нужно", f"Релик {target_relic}", "Норма"]
    else:
        headers = ["Стат", "Сейчас", "Норма"]

    # Омикрон/основа мода/сравнение с персонажем раньше шли теми же 3-5 колонками, что и обычные
    # статы — но их текст (название способности, слот+основа, "vs ИмяПерсонажа") сильно длиннее
    # обычных чисел, из-за чего моноширинная таблица (_build_table подгоняет ширину колонки под
    # САМУЮ длинную ячейку) распухала и разъезжалась на мобильном Discord (жалоба пользователя,
    # реальный скриншот, тред "Гайд по АС Боту" 2026-09-21: "тут чет куда-то все убежало"). Эти
    # три типа требований теперь копятся в отдельные списки строк и выводятся под таблицей
    # обычным текстом (без моноширинной колончатости) — таблица остаётся узкой и читаемой на
    # телефоне, а длинные названия просто переносятся как обычный текст.
    table_rows = []
    omicron_lines = []
    mod_primary_lines = []
    mod_set_lines = []
    compare_lines = []
    for row in rows:
        req_id, _, _, stat_name, operator, threshold, priority, raw_text, comment, _, _, skill_id, mod_slot, compare_character_key, row_scheme = row
        if active_scheme is not None and row_scheme and row_scheme != active_scheme:
            # Строка другой (неактивной) схемы — полностью пропускается: не в таблице,
            # не в matched/total, не в "Итог" (см. _resolve_scheme выше).
            continue
        is_omicron = stat_name == STAT_OMICRON
        is_mod_primary = stat_name == STAT_MOD_PRIMARY
        is_mod_set = stat_name == STAT_SET
        is_set_list = stat_name in (STAT_SET_ANY, STAT_SET_ONLY)
        is_compare = bool(compare_character_key)
        is_relic_row = stat_name == "Relic"
        row_key = progress_row_key(row)

        primary_req_text = None
        if is_mod_primary:
            # Основа «или»: сравнение "=" идёт с той из допустимых, что реально стоит на слоте
            # (если стоит допустимая), иначе с первой — дальше общая логика _compare/сценариев.
            primary_req_text = _mod_primary_req_text(mod_slot, threshold, skill_id)
            allowed = mod_primary_allowed(threshold, skill_id)
            actual = mod_primaries.get(mod_slot)
            threshold = float(actual) if actual in allowed else float(allowed[0])

        if is_set_list:
            key = f"{stat_name}:{skill_id}"
            label = _mod_set_list_label(stat_name, priority)
            req_cell = _mod_set_list_req_text(stat_name, skill_id)
            suffix = "*" if priority in ("optional", "useful") else ""
            ok = current_values.get(key, 0.0) >= 1.0
            mod_set_lines.append((f"{req_cell}{suffix}", f"{_worn_sets_text(mod_set_counts)} {'✅' if ok else '❌'}"))
            if rows_out is not None:
                rows_out.append({"key": row_key, "value": _worn_sets_text(mod_set_counts), "margin": None, "ok": ok})
            total += 1
            matched += ok
            if priority == PRIORITY_REQUIRED:
                required_total += 1
                if not ok:
                    failed_required.append({"stat": label, "current": _worn_sets_text(mod_set_counts), "requirement": req_cell})
            continue

        compare_char_name = None
        if is_compare:
            # Сравнение с другим персонажем ТОГО ЖЕ игрока — заметка Коли 2026-09-20:
            # "офицер быстрее Хакса в ТП на Лкайло". threshold_value в строке — отрыв
            # ("на сколько", 0 = просто сравнение; знак берётся из оператора, см.
            # _compare_offset_sign), реальный порог — живой текущий стат compare-персонажа
            # плюс/минус отрыв, подставляется сюда и дальше течёт по той же общей логике _compare/сценариев,
            # что и обычный числовой порог.
            compare_char_name = _unit_display_name(compare_character_key)
            compare_unit, _ = await _get_unit_for_player(bot, ally_code, compare_character_key, force_refresh)
            if compare_unit is None:
                suffix = "*" if priority in ("optional", "useful") else ""
                compare_lines.append(f"⚠️ {_stat_label(stat_name, '')} vs {compare_char_name}{suffix}: нет данных у сравниваемого")
                if rows_out is not None:
                    rows_out.append({"key": row_key, "value": None, "margin": None, "ok": None})
                continue
            compare_stats = _with_total_life(dict(stat_engine.calc_final_stats(bot.stat_calc, compare_unit)))
            compare_stats["Relic"] = stat_engine.get_current_relic_level(compare_unit)
            compare_offset = threshold or 0.0
            compare_value = compare_stats.get(stat_name)
            threshold = None if compare_value is None else _compare_threshold(compare_value, operator, compare_offset)

        if is_mod_primary:
            label = _mod_primary_label(mod_slot, priority)
        elif is_omicron:
            label = _omicron_label(skill_id, priority)
        elif is_mod_set:
            label = _mod_set_label(skill_id, priority)
        elif is_compare:
            label = _compare_label(stat_name, compare_character_key, priority)
        else:
            label = _stat_label(stat_name, priority)
        lookup_key = (
            f"ModPrimary:{mod_slot}" if is_mod_primary
            else f"Omicron:{skill_id}" if is_omicron
            else f"ModSet:{skill_id}" if is_mod_set
            else stat_name
        )
        if is_compare:
            req_cell = f"{operator} {compare_char_name}{_compare_offset_text(operator, compare_offset)}"
        elif is_mod_primary:
            req_cell = primary_req_text
        elif is_omicron:
            req_cell = "разблокирован"
        elif is_mod_set:
            req_cell = "надет" if (threshold or 1) <= 1 else f"×{int(threshold)}"
        else:
            req_cell = f"{operator} {_fmt_compact(threshold)}"
        cur_val = current_values.get(lookup_key)
        suffix = "*" if priority in ("optional", "useful") else ""
        if cur_val is None or threshold is None:
            if rows_out is not None:
                rows_out.append({"key": row_key, "value": None, "margin": None, "ok": None})
            if is_omicron:
                omicron_lines.append(f"⚠️ {_omicron_ability_label(skill_id)}{suffix}: нет данных")
            elif is_mod_primary:
                mod_primary_lines.append((f"{MOD_SLOT_LABELS.get(mod_slot, mod_slot)}{suffix}", "нет данных ⚠️"))
            elif is_mod_set:
                mod_set_lines.append((f"{_SET_NAME_BY_ID.get(int(skill_id), skill_id)}{suffix}", "нет данных ⚠️"))
            elif is_compare:
                compare_lines.append(f"⚠️ {_stat_label(stat_name, '')} vs {compare_char_name}{suffix}: нет данных")
            else:
                table_rows.append([label, "нет данных", "—", "—", req_cell] if show_projection else [label, "нет данных", req_cell])
            continue
        cur_ok = _compare(cur_val, operator, threshold)
        if is_mod_primary:
            actual_opt = _mod_primary_option(mod_slot, cur_val)
            cur_cell = f"{_mod_primary_stat_label(mod_slot, cur_val) if actual_opt else f'#{int(cur_val)}'} {'✅' if cur_ok else '❌'}"
        elif is_omicron:
            cur_cell = "Есть ✅" if cur_ok else "Нет ❌"
        elif is_mod_set and (threshold or 1) > 1:
            cur_cell = f"{int(cur_val)} из {int(threshold)} {'✅' if cur_ok else '❌'}"
        elif is_mod_set:
            cur_cell = "Надет ✅" if cur_ok else "Не надет ❌"
        elif is_compare:
            cur_cell = f"{_fmt_compact(cur_val)} vs {_fmt_compact(compare_value)}{_compare_offset_text(operator, compare_offset)} {'✅' if cur_ok else '❌'}"
        else:
            cur_cell = f"{_fmt_compact(cur_val)} {'✅' if cur_ok else '❌'}"

        if is_omicron:
            omicron_lines.append(f"{cur_cell} — {_omicron_ability_label(skill_id)}{suffix}")
        elif is_mod_primary:
            slot_label = MOD_SLOT_LABELS.get(mod_slot, mod_slot)
            if cur_ok:
                mod_primary_lines.append((f"{slot_label}{suffix}", cur_cell))
            else:
                req_label = primary_req_text.split(": ", 1)[1]
                mod_primary_lines.append((f"{slot_label}{suffix}", f"{cur_cell} → {req_label}"))
        elif is_mod_set:
            mod_set_lines.append((f"{_mod_set_req_text(skill_id, threshold)}{suffix}", cur_cell))
        elif is_compare:
            compare_lines.append(f"{cur_cell} — {_stat_label(stat_name, '')} {operator} {compare_char_name}{_compare_offset_text(operator, compare_offset)}{suffix}")

        proj_val = (projected_values.get(stat_name) if projected_values else None) if (show_projection and not is_omicron and not is_mod_primary and not is_mod_set and not is_compare and not is_relic_row) else None

        if is_omicron or is_mod_primary or is_mod_set or is_compare:
            pass  # уже добавлено в свой список строк выше, в table_rows не идёт
        elif not show_projection:
            table_rows.append([label, cur_cell, req_cell])
        else:
            needed_cell = "—"
            proj_cell = "—"
            if not is_relic_row and proj_val is not None:
                proj_ok = _compare(proj_val, operator, threshold)
                proj_cell = f"{_fmt_compact(proj_val)} {'✅' if proj_ok else '❌'}"
                # На сколько бы вырос стат к целевому релику (delta) — фиксированная величина
                # при тех же модах/шмоте, не зависит от текущего значения (см. план фичи).
                # needed = порог минус этот рост = сколько нужно ИМЕННО СЕЙЧАС, чтобы после
                # апа реликвии стат дотянул до нормы плейта.
                delta = proj_val - cur_val
                needed_now = threshold - delta
                needed_ok = _compare(cur_val, operator, needed_now)
                needed_cell = f"{_fmt_compact(needed_now)} {'✅' if needed_ok else '❌'}"
            table_rows.append([label, cur_cell, needed_cell, proj_cell, req_cell])

        # scenario_val/scenario_ok — критерий, который РЕАЛЬНО решает и подсчёт matched/total,
        # и "Итог" (failed_required) — единый источник правды для обоих, в отличие от старой
        # версии, где отображаемая дробь и итоговый список могли расходиться.
        # Omicron: сырое сравнение в RAW/FULL (омикрон либо есть, либо нет, релик его не
        # меняет) — но в UP строка целиком исключается из подсчёта (решено в Discord-треде
        # "Гайд по АС Боту" 2026-09-20: вариант "релик поднят только тем, кто ниже цели"
        # используется именно чтобы смотреть чисто модули, без завязки на факт разблокировки).
        # ModPrimary — наоборот, ВСЕГДА сырое сравнение в любом сценарии (в т.ч. UP): это и
        # есть "чисто модули", ради которых сценарий UP существует, исключать их из него было
        # бы бессмысленно.
        # StatCompare — тоже ВСЕГДА сырое сравнение в любом сценарии (явное решение 2026-09-20:
        # "в любом сценарии нужно допустить такие проверки" — они идут на равных с обычными
        # требованиями по статам/сетам/основам, а не привязаны к конкретному сценарию релика).
        # Relic — аналогично: ровно то, что каждый непустой сценарий хочет "отвязать" от
        # остального билда: в RAW считается как есть (это и есть факт нехватки реликвии,
        # который сценарий должен показать), а в UP/FULL строка целиком исключается из
        # подсчёта — иначе сама реликвия срывала бы "Итог" даже когда сценарий специально
        # проверяет билд/моды в отрыве от текущего уровня реликвии.
        if is_mod_primary or is_mod_set or is_compare:
            scenario_val = cur_val
        elif is_omicron:
            scenario_val = None if scenario == SCENARIO_UP else cur_val
        elif is_relic_row:
            scenario_val = cur_val if scenario == SCENARIO_RAW else None
        elif scenario == SCENARIO_RAW:
            scenario_val = cur_val
        elif scenario == SCENARIO_UP:
            scenario_val = up_values.get(stat_name, cur_val)
        else:  # SCENARIO_FULL
            scenario_val = proj_val if proj_val is not None else cur_val

        if scenario_val is None:
            continue

        scenario_ok = _compare(scenario_val, operator, threshold)
        if rows_out is not None:
            if is_mod_primary:
                shown = _mod_primary_stat_label(mod_slot, cur_val) if _mod_primary_option(mod_slot, cur_val) else "другая основа"
            elif is_omicron:
                shown = "есть" if scenario_ok else "нет"
            elif is_mod_set:
                shown = f"{int(cur_val)} из {int(threshold)}" if (threshold or 1) > 1 else ("надет" if scenario_ok else "не надет")
            elif is_compare:
                shown = f"{_fmt_compact(cur_val)} vs {_fmt_compact(compare_value)}"
            else:
                shown = _fmt_compact(scenario_val)
            # margin — запас до нормы (>0 — с запасом, <0 — не хватает): по нему «Прогресс
            # модинга» видит, сдвинулся ли игрок к норме между снимками. Для основ — нет.
            margin = None if is_mod_primary else _compare_offset_sign(operator) * (scenario_val - threshold)
            rows_out.append({"key": row_key, "value": shown, "margin": margin, "ok": scenario_ok})
        total += 1
        if scenario_ok:
            matched += 1
        if priority == PRIORITY_REQUIRED:
            required_total += 1

        if priority == PRIORITY_REQUIRED and not scenario_ok:
            if is_omicron:
                requirement_text = "нужна разблокировка"
            elif is_mod_primary or is_mod_set:
                requirement_text = req_cell
            elif scenario_val == cur_val:
                requirement_text = req_cell
            else:
                # Такая же "нужно СЕЙЧАС" величина, что и в колонке "Нужно" — но пересчитанная
                # под delta выбранного сценария, а не всегда под полную проекцию.
                delta = scenario_val - cur_val
                requirement_text = f"{operator} {_fmt_compact(threshold - delta)}"
            actual_opt = _mod_primary_option(mod_slot, cur_val) if is_mod_primary else None
            current_text = (
                "не разблокирован" if is_omicron
                else (f"надето {int(cur_val or 0)} из {int(threshold)}" if (threshold or 1) > 1 else "не надет") if is_mod_set
                else (_mod_primary_stat_label(mod_slot, cur_val) if actual_opt else "другая основа") if is_mod_primary
                else _fmt_compact(cur_val)
            )
            failed_required.append({
                "stat": label,
                "current": current_text,
                "requirement": requirement_text,
            })

    if only_failed:
        table_rows = [r for r in table_rows if any("❌" in str(c) for c in r)]
        omicron_lines = [l for l in omicron_lines if "❌" in l]
        mod_primary_lines = [l for l in mod_primary_lines if "❌" in l[1]]
        mod_set_lines = [l for l in mod_set_lines if "❌" in l[1]]
        compare_lines = [l for l in compare_lines if "❌" in l]
        comments = []
        if not (table_rows or omicron_lines or mod_primary_lines or mod_set_lines or compare_lines):
            caption += "\n✅ Крестов нет"

    block = caption
    if table_rows:
        block += "\n" + _build_table(headers, table_rows)
    if omicron_lines:
        block += "\n**🧬 Омикроны:**\n" + "\n".join(omicron_lines)
    if mod_primary_lines:
        block += "\n**⚙️ Основы модулей:**\n" + _code_pairs(mod_primary_lines)
    if mod_set_lines:
        block += "\n**🔷 Сеты:**\n" + _code_pairs(mod_set_lines)
    if compare_lines:
        block += "\n**⚔️ Сравнение с персонажами:**\n" + "\n".join(compare_lines)
    if comments:
        block += "\n" + "\n".join(f"💠 _{c}_" for c in comments)

    return char_name, block, matched, total, updated_at, failed_required, required_total, active_scheme, scheme_label


async def _build_guild_report(bot, plate_name: str, char_keys: list, guild_id: int = 1, scenario: str = SCENARIO_RAW, forced_scheme: int | None = None, squad_only: bool = False) -> dict:
    """Гильдийский вариант _evaluate_character_player — прогоняет весь зарегистрированный
    ростер по каждому персонажу плейта (char_keys сужается снаружи, если проверяем один
    персонаж), используя уже закэшированные в player_unit_cache данные (player_units_sync_loop,
    без обращений к Comlink — то же самое, что видит /статы без "обновить"). Раскладывает
    игроков на три бакета для рендера и в Discord, и в вебе.

    scenario — тот же критерий, что и в _evaluate_character_player (SCENARIO_RAW/UP/FULL),
    прокидывается туда как есть: и показанная дробь matched/total, и compliant/problem
    (has_failed_required) считаются по нему консистентно — раньше это были два независимых,
    несогласованных друг с другом переключателя (одно двигало только дробь, другое — вообще
    ничего, всегда молча считало как SCENARIO_FULL).

    Каждая запись problem[i]["chars"][j] несёт "failed_required" — тот же список
    {"stat", "current", "requirement"}, что возвращает _evaluate_character_player, — чтобы
    вызывающий код (веб-попап на /stats-check) мог показать конкретные невыполненные статы
    персонажа, а не только дробь matched/total.

    squad_only — игроки без отряда плейта (stat_plate_squads) уходят в "skipped" с причиной,
    а строки источников модульного плейта без отряда не проверяются; по умолчанию — все."""
    roster = database.get_all_user_mappings(guild_id)
    if not roster:
        return {
            "error": "Никто из гильдии не зарегистрирован (/регистрация) — проверять некого.",
            "total_players": 0, "compliant": [], "problem": [], "no_data": [], "skipped": [],
        }
    squads = database.get_stat_plate_squads(guild_id) if squad_only else {}

    # required_relic на персонажа — не меняется от игрока к игроку, считаем один раз
    # (не в цикле по ростеру) и приклеиваем к каждой "проблемной" строке ниже: пригодится
    # для кнопки "поставить задачу" в вебе (см. web/routes/stat_forecast.py) — цель по
    # реликвии берётся прямо из требования плейта, без пересчёта.
    required_relic_by_char = {}
    for base_id in char_keys:
        loaded = _load_char_rows(plate_name, base_id, guild_id)
        if loaded is not None:
            required_relic_by_char[base_id] = loaded[2]

    compliant, problem, no_data, skipped = [], [], [], []
    for _discord_id, ally_code, name in roster:
        missing = _missing_plate_squads(ally_code, guild_id, squads) if squads else {}
        if plate_name in missing:
            skipped.append({"name": name, "ally_code": ally_code, "reason": missing[plate_name]})
            continue
        rows_total = 0  # ВСЕ приоритеты — только чтобы понять, есть ли вообще данные (no_data)
        required_total_all = 0
        required_matched_all = 0
        char_problems = []
        has_failed_required = False
        skip_reasons = []
        evaluated = 0
        for base_id in char_keys:
            skip_plates, skip_reason = _char_squad_skip(plate_name, base_id, guild_id, missing)
            if skip_reason:
                skip_reasons.append(skip_reason)
                continue
            result = await _evaluate_character_player(bot, plate_name, base_id, ally_code, False, name, guild_id=guild_id, scenario=scenario, forced_scheme=forced_scheme, skip_plates=skip_plates)
            if result is None:
                continue
            evaluated += 1
            char_name, _block, _matched, total, _updated_at, failed_required, required_total, _active_scheme, scheme_label = result
            rows_total += total
            required_matched = required_total - len(failed_required)
            required_total_all += required_total
            required_matched_all += required_matched
            if failed_required:
                has_failed_required = True
            if required_total > 0 and required_matched < required_total:
                char_problems.append({
                    "char_name": char_name, "base_id": base_id, "matched": required_matched, "total": required_total,
                    "required_relic": required_relic_by_char.get(base_id),
                    "failed_required": failed_required,
                    "scheme_label": scheme_label,
                })

        # "Полностью соответствуют" — только по ОБЯЗАТЕЛЬНЫМ статам (failed_required,
        # см. _evaluate_character_player), а не по всем строкам плейта разом: та сумма
        # включает и "по желанию"/"полезные" строки, из-за чего один невыполненный
        # опциональный стат топил игрока целиком (реальный баг, найденный пользователем
        # на тестовом плейте, где ВСЕ строки были опциональными — гильдия показывала
        # 0/49 соответствуют, хотя обязательных требований не было вовсе). Тот же
        # принцип, что уже используется guild-репортом по датакронам (_match_counts —
        # там тоже "missing_required" считается только по priority=="required").
        # Показанная дробь "matched/total" ниже — тоже ТОЛЬКО по обязательным строкам
        # (required_total_all/required_matched_all), а не по всем строкам плейта разом:
        # иначе на плейте с большим числом опциональных требований дробь вида "27/83"
        # не даёт понять, сколько из непройденного реально обязательно (реальная путаница
        # у пользователя на AC_TW, 2026-09-16).
        entry = {
            "name": name, "ally_code": ally_code,
            "matched": required_matched_all, "total": required_total_all,
            "chars": char_problems,
        }
        if not evaluated and skip_reasons:
            skipped.append({"name": name, "ally_code": ally_code, "reason": skip_reasons[0]})
        elif rows_total == 0:
            no_data.append(entry)
        elif not has_failed_required:
            compliant.append(entry)
        else:
            problem.append(entry)

        # _evaluate_character_player — синхронный по факту (sqlite + StatCalc-расчёты без
        # единого реального await), при force_refresh=False (как тут). Без явной отдачи
        # управления цикл по всему ростеру × персонажам плейта блокирует event loop бота
        # сплошным CPU-куском на несколько секунд — за это время ЛЮБОЕ другое взаимодействие
        # (включая повторный клик того же игрока) не успевает получить defer() в 3-секундное
        # окно Discord и падает с "Приложение не отвечает" (жалоба NicolozZ, ас-тестовая,
        # 2026-09-21) — тот же класс бага, что CLAUDE.md уже описывает для необёрнутых Comlink-
        # вызовов, только тут виновник — синхронный StatCalc/sqlite, а не сеть. Тот же паттерн
        # периодической отдачи, что уже в player_units_sync_loop (await asyncio.sleep(0.1)).
        await asyncio.sleep(0)

    problem.sort(key=lambda r: (r["matched"] - r["total"], r["name"].lower()))
    compliant.sort(key=lambda r: r["name"].lower())
    no_data.sort(key=lambda r: r["name"].lower())
    skipped.sort(key=lambda r: r["name"].lower())
    return {
        "error": None, "total_players": len(roster) - len(skipped),
        "compliant": compliant, "problem": problem, "no_data": no_data, "skipped": skipped,
    }


async def _project_character_relic(bot, plate_name: str, base_id: str, target_relic: int, guild_id: int = 1, forced_scheme: int | None = None):
    """Возвращает (char_name, block) — пересчёт уже заданных в плейте норм на другой релик.
    Модель — та же, что в гильдийской Google-таблице (BASESTAT*MODMULT+flat): порог на
    исходном релике раскладывается на плоскую часть (RELIC_PROJECTION_FLAT_OFFSET — роллы
    с модов, не растут с базой) и оставшуюся часть, которая масштабируется пропорционально
    голой безмодовой базе стата. Множитель находится из уже заданного порога:
        multiplier = (threshold − flat) / base(исходный релик)
        projected  = base(целевой релик) × multiplier + flat
    Для статов без записи в RELIC_PROJECTION_FLAT_OFFSET (Speed, Potency, крит-статы и т.п.)
    норма не пересчитывается — как и в самой таблице, она просто переносится как есть.
    Тут нет живого игрока, поэтому авто-детект схемы (как в _evaluate_character_player)
    невозможен — если у персонажа есть схемы, forced_scheme выбирает какую показывать
    (по умолчанию схема 1, чтобы не мешать строки разных билдов в одной таблице).
    Возвращает None, если для этого персонажа нет сохранённых требований в плейте."""
    loaded = _load_char_rows(plate_name, base_id, guild_id)
    if loaded is None:
        return None
    rows, char_name, required_relic, comments, legend = loaded
    schemes_present = any(r[14] for r in rows)
    active_scheme = (forced_scheme or 1) if schemes_present else None
    scheme_label = None
    if active_scheme is not None:
        scheme_leaf_plate = next((r[1] for r in rows if r[14]), plate_name)
        scheme_label = database.get_character_scheme_labels(scheme_leaf_plate, base_id, guild_id).get(active_scheme, f"Вариант {active_scheme}")

    if required_relic is None:
        block = "⚠️ В требованиях плейта нет строки «Реликвия» — не от чего считать прибавку."
        return char_name, block

    ref_unit = _build_synthetic_unit(base_id, required_relic)
    target_unit = _build_synthetic_unit(base_id, target_relic)
    ref_values = _with_total_life(dict(stat_engine.calc_final_stats(bot.stat_calc, ref_unit)))
    target_values = _with_total_life(dict(stat_engine.calc_final_stats(bot.stat_calc, target_unit)))

    headers = ["Стат", f"Норма Р{required_relic}", f"Норма Р{target_relic}"]
    table_rows = []
    for row in rows:
        _, _, _, stat_name, operator, threshold, priority, raw_text, comment, _, _, skill_id, mod_slot, compare_character_key, row_scheme = row
        if active_scheme is not None and row_scheme and row_scheme != active_scheme:
            continue

        if stat_name == STAT_OMICRON:
            # Разблокировка омикрона от релика не зависит — норма не меняется между релик-колонками.
            label = _omicron_label(skill_id, priority)
            cell = "разблокирован"
            table_rows.append([label, cell, cell])
            continue

        if stat_name == STAT_MOD_PRIMARY:
            # Основа мода от релика не зависит — норма не меняется между релик-колонками.
            label = _mod_primary_label(mod_slot, priority)
            cell = _mod_primary_req_text(mod_slot, threshold, skill_id)
            table_rows.append([label, cell, cell])
            continue

        if stat_name == STAT_SET:
            # Надетый сет от релика не зависит — норма не меняется между релик-колонками.
            label = _mod_set_label(skill_id, priority)
            cell = _mod_set_req_text(skill_id, threshold)
            table_rows.append([label, cell, cell])
            continue

        if stat_name in (STAT_SET_ANY, STAT_SET_ONLY):
            label = _mod_set_list_label(stat_name, priority)
            cell = _mod_set_list_req_text(stat_name, skill_id)
            table_rows.append([label, cell, cell])
            continue

        if compare_character_key:
            # Сравнение с другим персонажем не проецируется на релик — оба живых значения
            # берутся как есть в момент проверки, норма не меняется между релик-колонками.
            label = _compare_label(stat_name, compare_character_key, priority)
            cell = _compare_req_text(stat_name, operator, compare_character_key, threshold)
            table_rows.append([label, cell, cell])
            continue

        label = _stat_label(stat_name, priority)
        orig_cell = f"{operator} {_fmt_compact(threshold)}"

        if stat_name == "Relic":
            table_rows.append([label, orig_cell, f"{operator} {_fmt_compact(target_relic)}"])
            continue

        flat = RELIC_PROJECTION_FLAT_OFFSET.get(stat_name)
        if flat is None:
            table_rows.append([label, orig_cell, orig_cell])
            continue

        ref_val = ref_values.get(stat_name)
        tgt_val = target_values.get(stat_name)
        if not ref_val or tgt_val is None:
            table_rows.append([label, orig_cell, "нет данных"])
            continue

        multiplier = (threshold - flat) / ref_val
        projected = tgt_val * multiplier + flat
        table_rows.append([label, orig_cell, f"{operator} {_fmt_compact(projected)}"])

    scheme_part = f" · Вариант: {scheme_label}" if active_scheme is not None else ""
    block = f"Норма пересчитана с релика {required_relic} на {target_relic} (плоская часть + пропорциональный рост базы){scheme_part}" + legend + "\n" + _build_table(headers, table_rows)
    if comments:
        block += "\n" + "\n".join(f"💠 _{c}_" for c in comments)

    return char_name, block


# =====================================================================
# Автокомплиты (модульные функции — как autocomplete_players/autocomplete_datacron_*)
# =====================================================================
async def autocomplete_stat_plate(inter: disnake.ApplicationCommandInteraction, string: str):
    guild_id = guild_resolver.autocomplete_guild_id(inter)
    if guild_id is None:
        return []
    plates = database.get_all_stat_requirement_plates(guild_id=guild_id)
    if not plates:
        return ["❌ Список плейтов пуст."]
    search = string.lower().strip()
    return [p for p in plates if not search or search in p.lower()][:25]


async def autocomplete_stat_character(inter: disnake.ApplicationCommandInteraction, string: str):
    guild_id = guild_resolver.autocomplete_guild_id(inter)
    if guild_id is None:
        return []
    plate = inter.filled_options.get("плейт")
    if not plate:
        return ["⚠️ СНАЧАЛА выберите плейт!"]
    char_keys = database.get_stat_requirement_characters(plate, guild_id=guild_id)
    if not char_keys:
        return ["❌ У этого плейта нет сохранённых требований."]
    search = string.lower().strip()
    by_alias = unit_aliases.matching(string)
    options = []
    for base_id in char_keys:
        label = f"{_unit_display_name(base_id)} [{base_id}]"
        if not search or search in label.lower() or base_id in by_alias:
            options.append(disnake.OptionChoice(name=label[:100], value=label))
    return options[:25]


# Кнопка «Показать всем» на скрытых (ephemeral) отчётах этого модуля — по образцу
# cogs/datacron_requirements.py::DatacronCheckRevealView (тот же паттерн, что и там:
# небольшое локальное дублирование класса вместо общего импорта — уже устоявшаяся
# конвенция репозитория, см. CLAUDE.md). Публикует те же embed'ы отчёта в канал
# открытым сообщением по нажатию — запрошено пользователем 2026-09-21 для
# /омикроны отчёт "как и в схожих командах".
class StatsRevealView(disnake.ui.View):
    def __init__(self, embeds):
        super().__init__(timeout=1800)
        self.embeds = embeds
        self.revealed = False

    @disnake.ui.button(label="Показать всем", emoji="🔓", style=disnake.ButtonStyle.secondary)
    async def reveal(self, button: disnake.ui.Button, interaction: disnake.MessageInteraction):
        if self.revealed:
            await interaction.response.defer()
            return
        self.revealed = True
        button.disabled = True
        button.label = "Показано всем"
        await interaction.response.edit_message(view=self)
        for e in self.embeds:
            await interaction.channel.send(embed=e)


# Листалка /статы (просьба Егора 2026-10-15: «чтобы на одной странице был один ТП»;
# 2026-10-16 NicolozZ: «быстро открыть нужного перса», «весь список на одной странице»):
# одно сообщение на Components V2. Итог — сначала невыполненные обязательные статы (кресты),
# затем список персонажей (сначала с ❌, дальше в порядке плейта) обычным текстом, а открыть
# персонажа — выпадающим списком (по 25 в списке). Кнопка у каждой строки (Section) съедала
# 3 компонента из 40 и давала лишь 9 строк на страницу. Страница персонажа — текст + тот же
# выпадающий список + «📋 К списку»; снизу ⏮ ◀ ▶ ⏭ ✖. V2 не живёт во View, поэтому компоненты
# несут custom_id "stp|<token>|<действие>", обработчики — on_stats_pager_click/on_stats_pager_pick,
# состояние в памяти (STATS_PAGER_TTL, после рестарта — «вызовите заново»). Текст страницы
# держим под лимитом 4000 символов V2-сообщения. Листать могут вызвавший и проверяемый игрок
# (его Discord по /регистрация, Ricardo 2026-10-16).
STATS_PAGE_TEXT_BUDGET = 3300
STATS_PICKS_PER_SELECT = 25
# Второй лимит — размер компонентов сообщения в байтах UTF-8 (~10 КБ; замерено 2026-10-26:
# 413 «message.components=10167»), а кириллица — 2 байта на символ. Текст страницы делит
# этот объём со списками персонажей (75 подписей ≈ 3,3 КБ), поэтому бюджет в байтах — то,
# что осталось после списков. Без этого длинный итог Юсина по AC_ALL падал с 500 от Discord.
STATS_MESSAGE_BYTES_BUDGET = 9000
STATS_PAGE_BYTES_MIN = 2500
STATS_PAGER_TTL = 1800
_stats_pagers: dict[str, dict] = {}


def _chunk_lines(lines: list, budget: int, byte_budget: int | None = None) -> list[str]:
    byte_budget = byte_budget or budget * 4
    chunks, current, size, size_bytes = [], [], 0, 0
    for line in lines:
        line = line[:budget]
        while len(line.encode()) > byte_budget:
            line = line[:len(line) * 3 // 4]
        n_bytes = len(line.encode()) + 1
        if current and (size + len(line) + 1 > budget or size_bytes + n_bytes > byte_budget):
            chunks.append("\n".join(current))
            current, size, size_bytes = [], 0, 0
        current.append(line)
        size += len(line) + 1
        size_bytes += n_bytes
    if current:
        chunks.append("\n".join(current))
    return chunks


def _picks_bytes(picks: list) -> int:
    """Байты, которые займут выпадающие списки (_render_stats_page показывает первые 75)."""
    shown = picks[:3 * STATS_PICKS_PER_SELECT]
    return sum(len(label[:100].encode()) + len(emoji.encode()) + 12 for label, emoji, _ in shown) + 300


def _build_stats_pages(title: str, summary_lines: list, picks: list, char_pages: list) -> tuple[list[dict], list]:
    """summary_lines — текст итога (кресты, список персонажей, «не требуется»), picks —
    [(подпись, эмодзи, char_name)] для выпадающего списка, char_pages — [(char_name, block)].
    Страницы: итог (обычно одна, при длинном тексте — продолжение), затем персонажи.
    Возвращает (pages, [(подпись, эмодзи, индекс страницы)]) — без персонажей без страницы."""
    byte_budget = max(STATS_PAGE_BYTES_MIN, STATS_MESSAGE_BYTES_BUDGET - _picks_bytes(picks) - len(title.encode()) - 600)
    summary_chunks = _chunk_lines(summary_lines, STATS_PAGE_TEXT_BUDGET, byte_budget) or [""]
    pages = []
    for k, chunk in enumerate(summary_chunks):
        suffix = f" ({k + 1}/{len(summary_chunks)})" if len(summary_chunks) > 1 else ""
        pages.append({"head": f"### {title} — итог{suffix}", "text": chunk})
    char_start = {}
    for name, block in char_pages:
        for k, chunk in enumerate(_chunk_lines(block.split("\n"), STATS_PAGE_TEXT_BUDGET, byte_budget)):
            char_start.setdefault(name, len(pages))
            pages.append({"head": f"### {title} — {name}" + (" (продолжение)" if k else ""), "text": chunk,
                          "back": 0, "char": char_start[name]})
    targets = [(label, emoji, char_start[name]) for label, emoji, name in picks if name in char_start]
    return pages, targets


def _render_stats_page(state: dict, closed: bool = False) -> list:
    ui = disnake.ui
    pages, i, token = state["pages"], state["index"], state["token"]
    page = pages[i]
    items = [ui.TextDisplay(page["head"])]
    if page.get("text"):
        items.append(ui.TextDisplay(page["text"]))
    footer = f"Страница {i + 1} из {len(pages)}" + (f" · {state['footer']}" if state["footer"] else "")
    items.append(ui.TextDisplay(f"-# {footer}"))
    components = [ui.Container(*items, accent_colour=disnake.Colour(state["color"]))]
    if closed:
        return components
    last = len(pages) - 1

    def nav(emoji, action, disabled, style=disnake.ButtonStyle.secondary):
        return ui.Button(emoji=emoji, custom_id=f"stp|{token}|{action}", disabled=disabled, style=style)

    components.append(ui.ActionRow(
        nav("⏮", "first", i == 0), nav("◀", "prev", i == 0), nav("▶", "next", i == last),
        nav("⏭", "last", i == last), nav("✖", "close", False, disnake.ButtonStyle.danger),
    ))
    picks = state["picks"]
    # Строк компонентов максимум 5: навигация + до 3 списков (75 персонажей) + «К списку».
    n_selects = min(3, math.ceil(len(picks) / STATS_PICKS_PER_SELECT))
    for n in range(n_selects):
        part = picks[n * STATS_PICKS_PER_SELECT:(n + 1) * STATS_PICKS_PER_SELECT]
        placeholder = "🔎 Открыть персонажа…"
        if n_selects > 1:
            placeholder = f"🔎 Открыть персонажа ({n * STATS_PICKS_PER_SELECT + 1}–{n * STATS_PICKS_PER_SELECT + len(part)})…"
        options = [disnake.SelectOption(label=label[:100], value=str(target), emoji=emoji,
                                        default=target == page.get("char"))
                   for label, emoji, target in part]
        components.append(ui.ActionRow(ui.StringSelect(custom_id=f"stp|{token}|pick{n}", placeholder=placeholder, options=options)))
    if "back" in page:
        components.append(ui.ActionRow(ui.Button(label="К списку", emoji="📋", custom_id=f"stp|{token}|back")))
    return components


def _stats_pager_gone_text(token: str) -> str:
    # Токен — id interaction (snowflake), по нему видно, истёк ли срок или бот перезапускался.
    try:
        age = (disnake.utils.utcnow() - disnake.utils.snowflake_time(int(token))).total_seconds()
    except ValueError:
        age = STATS_PAGER_TTL
    reason = "прошло 30 минут" if age >= STATS_PAGER_TTL else "бот перезапускался"
    return f"Листалка устарела ({reason}) — вызовите /статы заново."


async def _check_stats_pager_access(inter: disnake.MessageInteraction, token: str):
    state = _stats_pagers.get(token)
    if state is None:
        await inter.response.send_message(_stats_pager_gone_text(token), ephemeral=True)
        return None
    if inter.author.id not in state["allowed_ids"]:
        await inter.response.send_message("Листать могут только вызвавший команду и проверяемый игрок — вызовите /статы сами.", ephemeral=True)
        return None
    return state


async def _expire_stats_pager(token: str):
    await asyncio.sleep(STATS_PAGER_TTL)
    state = _stats_pagers.pop(token, None)
    if state and state.get("message") is not None:
        try:
            await state["message"].edit(components=_render_stats_page(state, closed=True), flags=disnake.MessageFlags(is_components_v2=True))
        except disnake.HTTPException:
            pass


# =====================================================================
#                       ОСНОВНОЙ МОДУЛЬ /статы
# =====================================================================
class StatRequirementsCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        if not hasattr(bot, "stat_calc"):
            bot.stat_calc = None
        self.stat_calc_loop.start()
        self.player_units_sync_loop.change_interval(hours=bot.PLAYER_STATS_SYNC_HOURS)
        self.player_units_sync_loop.start()

    def cog_unload(self):
        self.stat_calc_loop.cancel()
        self.player_units_sync_loop.cancel()

    @tasks.loop(hours=12)
    async def stat_calc_loop(self):
        try:
            self.bot.stat_calc = await stat_engine.build_stat_calc(self.bot.comlink)
            print("✅ [Статы] Калькулятор статов обновлён")
        except Exception as e:
            print(f"❌ [Статы] Ошибка обновления калькулятора статов: {e}")

    @stat_calc_loop.before_loop
    async def _before_stat_calc_loop(self):
        await self.bot.wait_until_ready()

    @tasks.loop(hours=1)
    async def player_units_sync_loop(self):
        # player_unit_cache — не per-guild (ключ ally_code глобально уникален), поэтому
        # синхронизируем объединённый ростер по всем зарегистрированным гильдиям сразу.
        # Заодно, до перезаписи кэша, диффим новый снимок против старого и пишем события
        # гильдийской активности (guild_activity_events) — замена сдохшему из-за Cloudflare
        # скрапингу swgoh.gg, см. services/activity_diff.py.
        # Подбираем "зависшие" omicron-события — записанные в БД (уже не всплывут повторным
        # диффом), но так и не объявленные: либо рестарт бота случился между записью и
        # объявлением с прошлого цикла (было живым инцидентом 2026-09-02), либо отправка тогда
        # упала на временной ошибке Discord. Не завязано на guild_roster_caches (в отличие от
        # остального цикла) — независимая проверка, чтобы отставание с ростером не откладывало
        # и без того просроченное объявление.
        stale_hits = [(event_id, ally_code, base_id, skill_id, guild_id)
                      for event_id, guild_id, ally_code, base_id, skill_id in database.get_unannounced_omicron_events()]
        if stale_hits:
            print(f"🔁 [Омикрон] Хвост необъявленных событий с прошлых циклов: {len(stale_hits)}")
            await self._announce_omicrons(stale_hits)
        if not self.bot.guild_roster_caches:
            return
        ally_to_guilds = {}
        for guild_id, cache in self.bot.guild_roster_caches.items():
            for ally_code in cache.values():
                ally_to_guilds.setdefault(ally_code, set()).add(guild_id)
        ally_codes = list(ally_to_guilds)
        if not ally_codes:
            return
        # Метка "последний автоцикл стартовал в X" — веб-дашборд (/activity) оценивает время
        # до следующего автообновления как last_auto_run + PLAYER_STATS_SYNC_HOURS. Пишем
        # только тут (не из ручного веб-синка), иначе кнопка "Обновить сейчас" на веб-странице
        # сбивала бы эту оценку, не переставляя реальный внутренний таймер disnake tasks.loop.
        database.set_bot_state("player_units_sync_loop_last_auto_run", datetime.now(MSK).isoformat())
        print(f"🔄 [Статы] Синхронизация ростеров игроков ({len(ally_codes)})...")
        # Грузим один раз на весь цикл (не на каждого из ~50 игроков) — таблица общая,
        # см. database.get_all_skill_tier_thresholds.
        skill_tier_map = database.get_all_skill_tier_thresholds()
        synced = 0
        total_events = 0
        today = datetime.now(MSK).date().isoformat()
        for ally_code in ally_codes:
            try:
                fetched, added, omicron_hits = await activity_diff.sync_player(
                    self.bot.comlink, ally_code, ally_to_guilds[ally_code], today, skill_tier_map
                )
                if fetched:
                    synced += 1
                total_events += added
                if omicron_hits:
                    # Объявляем сразу за игрока, а не копим на весь ~50-игроков цикл до конца —
                    # раньше рестарт бота посреди цикла терял объявления для ВСЕХ уже
                    # обработанных игроков (их omicron уже в БД, диффом больше не всплывёт),
                    # хотя объявить успевали единицы. Теперь окно потери — один игрок, а не весь
                    # цикл, и его подберёт stale_hits на следующем запуске в любом случае.
                    await self._announce_omicrons(
                        (event_id, ally_code, base_id, skill_id, guild_id)
                        for event_id, base_id, skill_id, guild_id in omicron_hits
                    )
            except Exception as e:
                print(f"⚠️ [Статы] Не удалось обновить ростер {ally_code}: {e}")
            await asyncio.sleep(0.1)
        print(f"✅ [Статы] Синхронизировано ростеров: {synced}/{len(ally_codes)}, событий активности: {total_events}")
        # Срез гильдии за сегодня для виджета на главной (services/guild_snapshot.py) — из
        # только что обновлённых кэшей, без запросов в игру.
        from services import guild_snapshot
        for guild_id in list(self.bot.guild_roster_caches):
            try:
                await asyncio.to_thread(guild_snapshot.take, guild_id)
            except Exception as e:
                print(f"⚠️ [Срез] Гильдия {guild_id}: {e!r}")

    @player_units_sync_loop.before_loop
    async def _before_player_units_sync_loop(self):
        await self.bot.wait_until_ready()

    # Если в канал за один вызов набралось столько отдельных объявлений (обычно это
    # database.get_unannounced_omicron_events после очень долгого простоя бота — см.
    # player_units_sync_loop) — не заваливаем канал вереницей сообщений подряд, а склеиваем
    # в одно (или несколько под лимит Discord в 2000 символов).
    OMICRON_COMBINE_THRESHOLD = 10

    @staticmethod
    def _chunk_omicron_lines(header: str, lines: list[tuple[int, str]], limit: int = 2000):
        """lines: [(event_id, строка), ...] → [(event_ids_чанка, текст_сообщения), ...],
        каждое сообщение — header + маркированный список, порезанный по лимиту символов."""
        chunks = []
        chunk_ids: list[int] = []
        chunk_text = header
        continuation_header = "🔁 Омикроны (продолжение)…"
        for event_id, line in lines:
            bullet = f"\n• {line}"
            if chunk_ids and len(chunk_text) + len(bullet) > limit:
                chunks.append((chunk_ids, chunk_text))
                chunk_ids = []
                chunk_text = continuation_header
            chunk_ids.append(event_id)
            chunk_text += bullet
        if chunk_ids:
            chunks.append((chunk_ids, chunk_text))
        return chunks

    async def _announce_omicrons(self, hits):
        """hits: [(event_id, ally_code, base_id, skill_id, guild_id), ...] — новые омикроны,
        найденные за этот цикл синка, плюс необъявленный хвост с прошлых циклов (см.
        database.get_unannounced_omicron_events). Постит в guilds.omicron_channel_id гильдии
        (если он настроен через /омикрон_текст канал); без настроенного канала для конкретной
        гильдии молча пропускает — это НЕ ошибка, просто фича ещё не включена для этой гильдии.
        На каждый успешно отправленный hit сразу ставит database.mark_activity_event_announced,
        поэтому рестарт бота посреди обработки списка не приводит к повторной отправке уже
        объявленных — упавшие/недошедшие останутся announced=0 и подберутся заново.
        Формат одиночного объявления ("**{игрок}** выдал омикрон **{способность}** ({тип})
        для {режим} на **{персонаж}**.") — тип/режим резолвятся из skill_tier_thresholds (см.
        services/units_sync.py::_skill_tier_thresholds); если справочник ещё не успел
        досинкать конкретный skill_id (гонка с hourly sync_units), молча опускаем скобки/
        "для ..." вместо кривого текста с пустышками. Если хитов на один канал набралось
        OMICRON_COMBINE_THRESHOLD и больше (типичный случай — бот был недоступен долго,
        накопился хвост в get_unannounced_omicron_events), склеиваются в одно/несколько
        сообщений через _chunk_omicron_lines вместо отдельного сообщения на каждый."""
        hits = list(hits)
        if not hits:
            return
        skill_info = database.get_skill_display_info([skill_id for _, _, _, skill_id, _ in hits])
        names_by_guild = {}
        channels_by_id: dict[int, object] = {}
        items_by_channel: dict[int, list[tuple[int, str]]] = {}
        guild_by_channel: dict[int, int] = {}
        for event_id, ally_code, base_id, skill_id, guild_id in hits:
            if not feature_flags.is_enabled(guild_id, "omicron"):
                continue
            guild_cfg = database.get_guild_config(guild_id)
            channel_id = guild_cfg.get("omicron_channel_id") if guild_cfg else None
            if not channel_id:
                # Гильдия без Discord-канала, но с Telegram-чатом (/notify «омикроны») —
                # объявляем только туда и сразу помечаем, иначе событие висело бы вечно.
                if outbox.guild_wants(guild_id, "omicrons"):
                    if guild_id not in names_by_guild:
                        names_by_guild[guild_id] = {code: name for _, code, name in database.get_all_user_mappings(guild_id)}
                    line = self._omicron_line(names_by_guild[guild_id].get(ally_code, ally_code), base_id, skill_id, skill_info)
                    outbox.enqueue_guild(guild_id, "omicrons", line)
                    database.mark_activity_event_announced(event_id)
                continue
            channel_id = int(channel_id)
            guild_by_channel[channel_id] = guild_id
            if channel_id not in channels_by_id:
                channel = self.bot.get_channel(channel_id)
                if channel is None:
                    # get_channel — только кеш шлюза, иногда промахивается даже при достаточных
                    # правах (см. тот же фикс и подробности в cogs/antispam.py).
                    try:
                        channel = await self.bot.fetch_channel(channel_id)
                    except (disnake.NotFound, disnake.Forbidden, disnake.HTTPException):
                        channel = None
                channels_by_id[channel_id] = channel
            if channels_by_id[channel_id] is None:
                continue
            if guild_id not in names_by_guild:
                names_by_guild[guild_id] = {code: name for _, code, name in database.get_all_user_mappings(guild_id)}
            player_name = names_by_guild[guild_id].get(ally_code, ally_code)
            channel = channels_by_id[channel_id]
            line = self._omicron_line(player_name, base_id, skill_id, skill_info, getattr(channel, "guild", None))
            items_by_channel.setdefault(channel_id, []).append((event_id, line))

        for channel_id, items in items_by_channel.items():
            channel = channels_by_id[channel_id]
            guild_id = guild_by_channel[channel_id]
            if len(items) >= self.OMICRON_COMBINE_THRESHOLD:
                header = f"🔁 Накопилось объявлений об омикронах: {len(items)} (бот был недоступен)"
                for chunk_event_ids, chunk_text in self._chunk_omicron_lines(header, items):
                    try:
                        await channel.send(chunk_text)
                    except Exception as e:
                        print(f"⚠️ [Омикрон] Не удалось отправить объединённое объявление в канал {channel_id}: {e}")
                        continue
                    outbox.enqueue_guild(guild_id, "omicrons", chunk_text)
                    for event_id in chunk_event_ids:
                        database.mark_activity_event_announced(event_id)
            else:
                for event_id, line in items:
                    try:
                        await channel.send(line)
                        database.mark_activity_event_announced(event_id)
                    except Exception as e:
                        print(f"⚠️ [Омикрон] Не удалось отправить объявление в канал {channel_id}: {e}")
                        continue
                    # В Telegram — вслед за Discord (только после успешной отправки, чтобы
                    # повторная попытка неотправленного не дублировала в Telegram).
                    outbox.enqueue_guild(guild_id, "omicrons", line)

    @staticmethod
    def _omicron_line(player_name: str, base_id: str, skill_id: str, skill_info: dict, discord_guild=None) -> str:
        ability_name, _ability_id, ability_type, omicron_mode = skill_info.get(skill_id, (None, None, None, None))
        line = f"**{player_name}** выдал омикрон"
        if ability_name:
            line += f" **{ability_name}**"
            if ability_type:
                line += f" ({ability_type})"
        if omicron_mode:
            line += f" для {omicron_mode}"
        line += f" на **{_unit_display_name(base_id)}**."
        phrase = database.get_omicron_phrase(base_id, skill_id)
        if phrase:
            line += f" {_resolve_emoji_shortcodes(phrase, discord_guild)}"
        return line

    # ------------------ /омикроны отчёт (запрос из офицерского чата гильдии GR,
    # 2026-09-20: рекомендации "что поставить" были видны только в веб-дашборде
    # /omicrons/report, тут та же логика services/omicron_priority.py выведена в Discord). ------------------
    @commands.slash_command(name="омикроны", description="Отчёты по приоритетным для ВГ омикронам гильдии")
    async def omicrons_group(self, inter: disnake.ApplicationCommandInteraction):
        pass

    @omicrons_group.sub_command(name="отчёт", description="Какие приоритетные для ВГ омикроны вы готовы поставить, но не поставили")
    async def omicrons_report_player(
        self,
        inter: disnake.ApplicationCommandInteraction,
        участник: disnake.User = commands.Param(default=None, description="🔒 Офицеры: " + guild_resolver.TARGET_USER_DESCRIPTION),
        аликод: str = commands.Param(default=None, description="🔒 Офицеры: " + guild_resolver.TARGET_CODE_DESCRIPTION),
        альт: bool = commands.Param(default=False, description=guild_resolver.ALT_PARAM_DESCRIPTION),
    ):
        await inter.response.defer(ephemeral=True)

        if альт:
            alt = await guild_resolver.require_alt(inter, "omicron", other_target=bool(участник or аликод))
            if alt is None:
                return
            guild_id, ally_code, игрок = alt["guild_id"], alt["ally_code"], alt["name"]
        else:
            guild_id = await guild_resolver.require_feature(inter, "omicron")
            if guild_id is None:
                return

        if not альт:
            ally_code, игрок, error = guild_resolver.target_player(участник, аликод, guild_id)
            if error:
                await inter.edit_original_response(error)
                return
            if ally_code is None:
                registration = database.get_user_registration(str(inter.author.id), guild_id=guild_id)
                if not registration:
                    await inter.edit_original_response("❌ Игрок не указан, а вы не зарегистрированы — используйте `/регистрация` или укажите участника.")
                    return
                ally_code, игрок = registration
            игрок = игрок or database.get_player_names(guild_id).get(ally_code) or ally_code

        self_error = guild_resolver.self_only_error(inter.author, guild_id, ally_code)
        if self_error:
            await guild_resolver.send_denied(inter, self_error)
            return

        missing = omicron_priority.missing_omicrons_for_player(ally_code, guild_id)
        if not missing:
            await inter.edit_original_response(f"✅ У **{игрок}** нет готовых, но не поставленных приоритетных омикронов (либо приоритеты ВГ ещё не настроены — /omicrons/priority).")
            return

        lines = []
        for item in missing:
            mode_part = f" ({item['omicron_mode']})" if item.get("omicron_mode") else ""
            lines.append(f"**{item['unit_name']}** — {item['skill_name']}{mode_part}")

        embeds = _lines_to_embeds(f"🧬 {игрок} — что можно поставить", DATACRON_LIST_COLOR, lines)
        view = StatsRevealView(embeds)
        await inter.edit_original_response(embed=embeds[0], view=view)
        for e in embeds[1:]:
            await inter.followup.send(embed=e, ephemeral=True)

    # ------------------ /статы_требования ------------------
    # Проверка прав больше не висит на группе целиком: "список"/"плейты" открыты
    # уровню member (main.py::MEMBER_ACCESSIBLE_COMMANDS), а остальные сабкоманды
    # (создать/добавить/редактировать/переименовать/удалить) и так требуют
    # officer через глобальный гейт main.py::_check_access по умолчанию.
    @commands.slash_command(name="статы_требования", description="Просмотр плейтов и требований к статам (правка — на сайте)")
    async def stat_req(self, inter: disnake.ApplicationCommandInteraction):
        pass

    @stat_req.sub_command(name="список", description="Показать сохранённые требования по плейту (и опционально персонажу)")
    async def stat_req_list(
        self,
        inter: disnake.ApplicationCommandInteraction,
        плейт: str = commands.Param(description="Плейт", autocomplete=autocomplete_stat_plate),
        персонаж: str = commands.Param(default=None, description="Персонаж (если не указан — весь плейт)", autocomplete=autocomplete_stat_character),
    ):
        await inter.response.defer(ephemeral=True)
        guild_id = await guild_resolver.require_feature(inter, "stat_requirements")
        if guild_id is None:
            return

        char_keys = [_parse_bracket_id(персонаж)] if персонаж else database.get_stat_requirement_characters(плейт, guild_id=guild_id)
        if not char_keys:
            await inter.edit_original_response("❌ Нет требований для этого плейта.")
            return

        lines = []
        for base_id in char_keys:
            rows = database.get_stat_requirements(плейт, base_id, guild_id=guild_id)
            if not rows:
                continue
            scheme_labels = database.get_character_scheme_labels(плейт, base_id, guild_id=guild_id)
            lines.append(f"## {_unit_display_name(base_id)}")
            for row in rows:
                req_id, _, _, _, _, _, priority, raw_text, comment, _, _, _, _, _, scheme_num = row
                comment_part = f" · _{comment}_" if comment else ""
                scheme_part = f" · [{scheme_labels.get(scheme_num, f'Вариант {scheme_num}')}]" if scheme_num else ""
                lines.append(f"`#{req_id}` {PRIORITY_EMOJI.get(priority, '')} {raw_text}{comment_part}{scheme_part}")

        embeds = _lines_to_embeds(f"📋 {плейт}", DATACRON_LIST_COLOR, lines)
        if not embeds:
            await inter.edit_original_response("❌ Нет требований для этого плейта.")
            return
        await inter.edit_original_response(embed=embeds[0])
        for e in embeds[1:]:
            await inter.followup.send(embed=e, ephemeral=True)

    @stat_req.sub_command(name="плейты", description="Показать список всех плейтов")
    async def stat_req_list_plates(self, inter: disnake.ApplicationCommandInteraction):
        await inter.response.defer(ephemeral=True)
        guild_id = await guild_resolver.require_feature(inter, "stat_requirements")
        if guild_id is None:
            return

        rows = database.get_all_stat_plates_detailed(guild_id=guild_id)
        if not rows:
            await inter.edit_original_response("❌ Плейтов пока нет — создайте на сайте, в разделе «Плейты».")
            return

        lines = []
        for name, description, is_modular, char_count, req_count in rows:
            desc_part = f" — _{description}_" if description else ""
            modular_part = " 🧩" if is_modular else ""
            lines.append(f"`{name}`{modular_part}{desc_part} · персонажей: {char_count}, требований: {req_count}")

        embeds = _lines_to_embeds("📋 Плейты", DATACRON_LIST_COLOR, lines)
        await inter.edit_original_response(embed=embeds[0])
        for e in embeds[1:]:
            await inter.followup.send(embed=e, ephemeral=True)

    @commands.slash_command(name="статы", description="Прогноз своих статов на релик плейта (чужие и вся гильдия — 🔒 офицеры)")
    async def stats_check(
        self,
        inter: disnake.ApplicationCommandInteraction,
        плейт: str = commands.Param(description="Плейт (набор требований)", autocomplete=autocomplete_stat_plate),
        участник: disnake.User = commands.Param(default=None, description="🔒 Офицеры: " + guild_resolver.TARGET_USER_DESCRIPTION),
        аликод: str = commands.Param(default=None, description="🔒 Офицеры: " + guild_resolver.TARGET_CODE_DESCRIPTION),
        персонаж: str = commands.Param(default=None, description="Персонаж из плейта (если не указан — весь плейт)", autocomplete=autocomplete_stat_character),
        обновить: bool = commands.Param(default=False, description="Обновить данные игрока из игры перед расчётом"),
        гильдия: bool = commands.Param(default=False, description="🔒 Офицеры: проверить всю гильдию вместо одного игрока"),
        сценарий: str = commands.Param(default=SCENARIO_RAW, description="Как сравнивать билд/модули с нормой плейта по релику", choices=SCENARIO_CHOICES),
        вариант: str = commands.Param(default=None, description="Форсировать вариант мод-билда вместо авто-детекта (не действует на персонажей без вариантов)", choices=FORCE_SCHEME_CHOICES),
        с_отрядом: bool = commands.Param(default=False, description="Только у кого есть отряд плейта (состав задаётся на сайте). По умолчанию — все"),
        только_кресты: bool = commands.Param(default=False, description="Показать только невыполненное (❌) — без строк в норме"),
        альт: bool = commands.Param(default=False, description=guild_resolver.ALT_PARAM_DESCRIPTION),
    ):
        await inter.response.defer()

        if альт:
            alt = await guild_resolver.require_alt(inter, "stat_requirements", other_target=bool(участник or аликод or гильдия))
            if alt is None:
                return
            guild_id, аликод = alt["guild_id"], alt["ally_code"]
        else:
            guild_id = await guild_resolver.require_feature(inter, "stat_requirements")
            if guild_id is None:
                return

        if not await stat_engine.wait_stat_calc(self.bot, inter):
            await inter.edit_original_response("⏳ Калькулятор статов не загрузился — попробуйте через пару минут.")
            return

        forced_scheme = _parse_scheme_param(вариант)

        char_keys = [_parse_bracket_id(персонаж)] if персонаж is not None else database.get_stat_requirement_characters(плейт, guild_id=guild_id)
        if not char_keys:
            await inter.edit_original_response("❌ Нет сохранённых требований для этого плейта.")
            return

        if гильдия:
            if not guild_resolver.is_officer_for_resolved_guild(inter.author):
                await guild_resolver.send_denied(inter, guild_resolver.GUILD_WIDE_OFFICER_ONLY)
                return

            report = await _build_guild_report(self.bot, плейт, char_keys, guild_id=guild_id, scenario=сценарий, forced_scheme=forced_scheme, squad_only=с_отрядом)
            if report["error"]:
                await inter.edit_original_response(f"❌ {report['error']}")
                return

            lines = [f"✅ Полностью соответствуют: {len(report['compliant'])}/{report['total_players']}"]
            if сценарий == SCENARIO_RAW:
                lines.append("_(как есть, без проекции на релик — как в ХБ)_")
            elif сценарий == SCENARIO_UP:
                lines.append("_(релик поднят только тем, кто ниже цели плейта)_")
            else:
                lines.append("_(полный подгон релика к цели плейта — вверх и вниз)_")
            if report["no_data"]:
                lines.append(f"⚠️ Нет данных: {len(report['no_data'])}")
            if report["skipped"]:
                lines.append(f"⚪ Не требуется — нет отряда: {len(report['skipped'])}")
            lines.append("")
            if report["compliant"]:
                lines.append("✅ " + ", ".join(r["name"] for r in report["compliant"]))
                lines.append("")
            if report["problem"]:
                lines.append("_(дробь ниже — только по ОБЯЗАТЕЛЬНЫМ требованиям, без опциональных/полезных)_")
                for r in report["problem"]:
                    lines.append(f"❌ {r['name']} — обязательных {r['matched']}/{r['total']} (не выполнено: {r['total'] - r['matched']})")
            else:
                lines.append("Все закрыли все требования! 🎉")
            if report["skipped"]:
                lines.append("")
                lines.append("⚪ Нет отряда: " + ", ".join(f"{r['name']} ({r['reason']})" for r in report["skipped"]))

            if not report["problem"]:
                color = DATACRON_CHECK_COLOR_FULL
            elif not report["compliant"]:
                color = DATACRON_CHECK_COLOR_NONE
            else:
                color = DATACRON_CHECK_COLOR_PARTIAL

            title = f"📋 {плейт} — гильдия"
            embeds = _lines_to_embeds(title, color, lines)
            await inter.edit_original_response(embed=embeds[0])
            for e in embeds[1:]:
                await inter.followup.send(embed=e)
            return

        ally_code, игрок, error = guild_resolver.target_player(участник, аликод, guild_id)
        if error:
            await inter.edit_original_response(error)
            return
        if ally_code is None:
            registration = database.get_user_registration(str(inter.author.id), guild_id=guild_id)
            if not registration:
                await inter.edit_original_response(
                    "❌ Игрок не указан, а вы не зарегистрированы — используйте `/регистрация`, укажите участника "
                    "или код союзника."
                )
                return
            ally_code, игрок = registration
        игрок = игрок or database.get_player_names(guild_id).get(ally_code) or ally_code

        self_error = guild_resolver.self_only_error(inter.author, guild_id, ally_code)
        if self_error:
            await guild_resolver.send_denied(inter, self_error)
            return

        missing = {}
        if с_отрядом:
            if обновить:
                await activity_diff.refresh_player_units(self.bot.comlink, ally_code)
            missing = _missing_plate_squads(ally_code, guild_id)
            if плейт in missing:
                await inter.edit_original_response(f"⚪ У игрока «{игрок}» нет отряда плейта {плейт} ({missing[плейт]}) — плейт ему не требуется.")
                return

        char_pages = []  # (char_name, block)
        summary_rows = []  # (char_name, matched, total, has_failed_required)
        matched_total = 0
        rows_total = 0
        updated_ats = []
        failed_required_by_char = []
        skipped_chars = []
        for base_id in char_keys:
            skip_plates, skip_reason = _char_squad_skip(плейт, base_id, guild_id, missing)
            if skip_reason:
                skipped_chars.append((_unit_display_name(base_id), skip_reason))
                continue
            result = await _evaluate_character_player(self.bot, плейт, base_id, ally_code, обновить, игрок, guild_id=guild_id, scenario=сценарий, forced_scheme=forced_scheme, skip_plates=skip_plates, only_failed=только_кресты)
            if result is None:
                continue
            char_name, block, matched, total, updated_at, failed_required, _required_total, _active_scheme, _scheme_label = result
            summary_rows.append((char_name, matched, total, bool(failed_required)))
            # «Только кресты»: персонаж без единого ❌ отдельной страницы не получает.
            if not (только_кресты and matched == total and "❌" not in block):
                char_pages.append((char_name, block))
            matched_total += matched
            rows_total += total
            if updated_at:
                updated_ats.append(updated_at)
            if failed_required:
                failed_required_by_char.append((char_name, failed_required))

        if not summary_rows:
            if skipped_chars:
                await inter.edit_original_response(f"⚪ У игрока «{игрок}» нет отрядов этого плейта — проверять нечего.")
            else:
                await inter.edit_original_response("❌ Нет сохранённых требований для этого плейта.")
            return

        # Первая страница — итог: персонажи плейта с дробью + обязательные статы, не прошедшие
        # норму по выбранному сценарию (failed_required в _evaluate_character_player).
        _scenario_titles = {
            SCENARIO_RAW: "как есть, без проекции — как в ХБ",
            SCENARIO_UP: "релик поднят только тем, кто ниже цели",
            SCENARIO_FULL: "с полным подгоном релика на цель плейта",
        }
        # Порядок итога (NicolozZ 2026-10-16): сначала кресты, затем список персонажей —
        # сперва с ❌, дальше в порядке плейта (сортировка стабильная). Страницы персонажей —
        # в том же порядке, чтобы ▶ шла по списку.
        summary_rows.sort(key=lambda row: not row[3])
        order = {row[0]: pos for pos, row in enumerate(summary_rows)}
        char_pages.sort(key=lambda page: order.get(page[0], len(order)))
        failed_required_by_char.sort(key=lambda item: order.get(item[0], len(order)))

        summary = [f"**Обязательные статы** ({_scenario_titles[сценарий]})"]
        if failed_required_by_char:
            for char_name, failed_items in failed_required_by_char:
                parts = "; ".join(f"{item['stat']}: {item['current']} (нужно {item['requirement']})" for item in failed_items)
                summary.append(f"❌ **{char_name}** — {parts}")
        else:
            summary.append("✅ Все обязательные статы в норме.")
        summary.append("")
        summary.append(f"**Персонажи** ({len(summary_rows)})")
        picks = []
        for char_name, matched, total, has_failed in summary_rows:
            # Иконка — только по обязательным: недобор по опциональным не делает персонажа «⚠️».
            icon = "❌" if has_failed else "✅"
            summary.append(f"{icon} {char_name} — {matched}/{total}")
            picks.append((f"{char_name} — {matched}/{total}", icon, char_name))
        if skipped_chars:
            summary.append("")
            summary.append(f"**⚪ Не требуется — нет отряда** ({len(skipped_chars)})")
            for char_name, reason in skipped_chars:
                summary.append(f"{char_name} — {reason}")
        if только_кресты:
            summary.append("")
            summary.append("_Режим «только кресты»: на страницах персонажей — только невыполненное._")

        # Цвет — по обязательным статам (как и текст итога), а не matched_total == rows_total.
        if not failed_required_by_char:
            color = DATACRON_CHECK_COLOR_FULL
        elif matched_total == 0:
            color = DATACRON_CHECK_COLOR_NONE
        else:
            color = DATACRON_CHECK_COLOR_PARTIAL

        title = f"📋 {плейт} (игрок: {игрок})"
        footer = f"Данные игрока: {updated_ats[0]}" if updated_ats else "⚠️ Нет сохранённых данных ни по одному из этих юнитов у игрока"
        if not char_pages:
            # Листать нечего (всё в норме при «только кресты») — обычный embed, как раньше.
            pages = _lines_to_embeds(title + " — итог", color, summary)
            for e in pages:
                e.set_footer(text=footer)
            await inter.edit_original_response(embed=pages[0])
            for e in pages[1:]:
                await inter.followup.send(embed=e)
            return
        token = inter.id
        allowed_ids = {inter.author.id}
        target_discord_id = database.get_discord_id_for_ally(ally_code, guild_id=guild_id)
        if target_discord_id and str(target_discord_id).isdigit():
            allowed_ids.add(int(target_discord_id))
        pages, picks = _build_stats_pages(title, summary, picks, char_pages)
        state = {
            "token": token, "allowed_ids": allowed_ids, "index": 0, "color": color, "footer": footer,
            "pages": pages, "picks": picks, "message": None,
        }
        _stats_pagers[str(token)] = state
        await inter.edit_original_response(components=_render_stats_page(state), flags=disnake.MessageFlags(is_components_v2=True))
        state["message"] = await inter.original_response()
        asyncio.create_task(_expire_stats_pager(str(token)))

    @commands.Cog.listener("on_button_click")
    async def on_stats_pager_click(self, inter: disnake.MessageInteraction):
        """Кнопки листалки /статы (см. _render_stats_page)."""
        parts = (inter.component.custom_id or "").split("|")
        if len(parts) != 3 or parts[0] != "stp":
            return
        state = await _check_stats_pager_access(inter, parts[1])
        if state is None:
            return
        action, last = parts[2], len(state["pages"]) - 1
        flags = disnake.MessageFlags(is_components_v2=True)
        if action == "close":
            _stats_pagers.pop(parts[1], None)
            await inter.response.edit_message(components=_render_stats_page(state, closed=True), flags=flags)
            return
        targets = {"first": 0, "prev": state["index"] - 1, "next": state["index"] + 1, "last": last,
                   "back": state["pages"][state["index"]].get("back", 0)}
        if action in targets:
            index = targets[action]
        else:
            await inter.response.defer()
            return
        state["index"] = max(0, min(index, last))
        await inter.response.edit_message(components=_render_stats_page(state), flags=flags)

    @commands.Cog.listener("on_dropdown")
    async def on_stats_pager_pick(self, inter: disnake.MessageInteraction):
        """Выпадающий список «Открыть персонажа» листалки /статы."""
        parts = (inter.component.custom_id or "").split("|")
        if len(parts) != 3 or parts[0] != "stp" or not parts[2].startswith("pick"):
            return
        state = await _check_stats_pager_access(inter, parts[1])
        if state is None:
            return
        value = (inter.values or [""])[0]
        if not value.isdigit():
            await inter.response.defer()
            return
        state["index"] = max(0, min(int(value), len(state["pages"]) - 1))
        await inter.response.edit_message(components=_render_stats_page(state), flags=disnake.MessageFlags(is_components_v2=True))

    # ------------------ /статы_релик (пересчёт нормы плейта на другой релик) ------------------
    @commands.slash_command(name="статы_релик", description="Пересчитать уже заданную в плейте норму персонажа на другой релик")
    async def stats_relic_check(
        self,
        inter: disnake.ApplicationCommandInteraction,
        плейт: str = commands.Param(description="Плейт (набор требований)", autocomplete=autocomplete_stat_plate),
        персонаж: str = commands.Param(description="Персонаж из плейта", autocomplete=autocomplete_stat_character),
        релик: int = commands.Param(description="Целевой уровень реликвии", ge=0, le=10),
        вариант: str = commands.Param(default=None, description="Какой вариант мод-билда показывать, если у персонажа их два (по умолчанию вариант 1)", choices=SCHEME_CHOICES),
    ):
        await inter.response.defer()

        guild_id = await guild_resolver.require_feature(inter, "stat_requirements")
        if guild_id is None:
            return

        if not await stat_engine.wait_stat_calc(self.bot, inter):
            await inter.edit_original_response("⏳ Калькулятор статов не загрузился — попробуйте через пару минут.")
            return

        base_id = _parse_bracket_id(персонаж)
        result = await _project_character_relic(self.bot, плейт, base_id, релик, guild_id=guild_id, forced_scheme=_parse_scheme_param(вариант))
        if result is None:
            await inter.edit_original_response("❌ Нет сохранённых требований для этого персонажа в плейте.")
            return

        char_name, block = result
        title = f"📋 {плейт}: {char_name} → релик {релик}"
        embeds = _lines_to_embeds(title, DATACRON_LIST_COLOR, [block])
        if not embeds:
            await inter.edit_original_response("❌ Нечего показать.")
            return

        embeds[-1].set_footer(text="Модель: плоская часть порога (роллы модулей) не растёт, остальное масштабируется вместе с базой персонажа")

        await inter.edit_original_response(embed=embeds[0])
        for e in embeds[1:]:
            await inter.followup.send(embed=e)


def setup(bot):
    bot.add_cog(StatRequirementsCog(bot))
