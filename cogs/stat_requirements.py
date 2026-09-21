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
from services import activity_diff, feature_flags, mod_search, omicron_priority, resource_spend
from services.config_status import config_warning_text
from cogs.violations import autocomplete_players
from cogs.tasks import units_autocomplete

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
)

# Полный список названий статов у StatCalc (swgoh_comlink/helpers/_stat_data.py, таблица
# STATS) шире — здесь только те, что реально фигурируют в гильдийских билд-требованиях.
# "Relic" — не настоящий стат StatCalc, спец-значение для требования к уровню реликвии.
# ВАЖНО: "обобщённые" статы без Physical/Special (Critical Chance, Critical Avoidance,
# Accuracy, Offense, Defense) в финальном расчёте StatCalc НЕ существуют — final-словарь
# отдаёт только расщеплённые Physical.../Special... варианты, генерик-ключи всегда None
# (проверено эмпирически на реальном игроке). Такие пункты сюда не добавлять — требование
# с ними навсегда останется "нет данных".
STAT_CHOICES = [
    disnake.OptionChoice(name="Relic (уровень реликвии)", value="Relic"),
    disnake.OptionChoice(name="Total Life (Health+Protection)", value="Total Life"),
    disnake.OptionChoice(name="Health", value="Health"),
    disnake.OptionChoice(name="Protection", value="Protection"),
    disnake.OptionChoice(name="Speed", value="Speed"),
    disnake.OptionChoice(name="Physical Damage", value="Physical Damage"),
    disnake.OptionChoice(name="Special Damage", value="Special Damage"),
    disnake.OptionChoice(name="Armor", value="Armor"),
    disnake.OptionChoice(name="Resistance", value="Resistance"),
    disnake.OptionChoice(name="Potency", value="Potency"),
    disnake.OptionChoice(name="Tenacity", value="Tenacity"),
    disnake.OptionChoice(name="Critical Damage", value="Critical Damage"),
    disnake.OptionChoice(name="Physical Critical Chance", value="Physical Critical Chance"),
    disnake.OptionChoice(name="Special Critical Chance", value="Special Critical Chance"),
    disnake.OptionChoice(name="Physical Critical Avoidance", value="Physical Critical Avoidance"),
    disnake.OptionChoice(name="Special Critical Avoidance", value="Special Critical Avoidance"),
    disnake.OptionChoice(name="Physical Accuracy", value="Physical Accuracy"),
    disnake.OptionChoice(name="Special Accuracy", value="Special Accuracy"),
    disnake.OptionChoice(name="Health Steal", value="Health Steal"),
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

# {int(set_id): "Health"/...} — те же английские имена, что уже использует /моды_поиск
# (cogs/mod_search.py::SET_CHOICES), не переизобретаем русский перевод, которого в проекте
# нигде больше нет для сетов.
_SET_NAME_BY_ID = {int(k): v for k, v in stat_engine.MOD_SET_IDS.items()}
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
MOD_SLOT_CHOICES = [disnake.OptionChoice(name=label, value=key) for key, label in MOD_SLOT_LABELS.items()]

OPERATOR_CHOICES = [
    disnake.OptionChoice(name=">=", value=">="),
    disnake.OptionChoice(name="<=", value="<="),
    disnake.OptionChoice(name="=", value="="),
]

# Требование можно привязать к одной из двух "схем" мод-билда персонажа (обсуждение с Колей,
# 2026-09-21: "2 карточки перса - одна в одном билде, другая в другом"; матчинг по совпавшим
# основам, при равенстве или отсутствии данных — всегда схема 1). value="" — требование общее
# для обеих схем (тот же смысл, что и NULL в stat_requirements.scheme_num, единственный режим
# до этой фичи). Человекочитаемые названия схем ("Скорость"/"КД" и т.п.) — отдельно, per
# персонаж, в stat_plate_character_schemes/схема_переименовать; тут только номер слота.
SCHEME_CHOICES = [
    disnake.OptionChoice(name="Обе схемы", value=""),
    disnake.OptionChoice(name="Схема 1", value="1"),
    disnake.OptionChoice(name="Схема 2", value="2"),
]
# Для /статы/статы_релик — форсирует, под какую схему проверять/пересчитывать, вместо
# авто-определения (значение "" = авто).
FORCE_SCHEME_CHOICES = [
    disnake.OptionChoice(name="Авто (по фактическим модам игрока)", value=""),
    disnake.OptionChoice(name="Схема 1", value="1"),
    disnake.OptionChoice(name="Схема 2", value="2"),
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

# /ресурсы — период подсчёта потраченных деталей/сигналов (services/resource_spend.py).
RESOURCE_PERIOD_CHOICES = [
    disnake.OptionChoice(name="Неделя", value="week"),
    disnake.OptionChoice(name="Месяц", value="month"),
    disnake.OptionChoice(name="3 месяца", value="3months"),
]

# Короткие подписи для колонки "Стат" в таблице /статы — полные названия (особенно
# Physical/Special Damage, Critical Chance/Avoidance) не влезают в ширину на мобильных.
# Health/Speed/Armor/Potency и т.п. уже достаточно короткие — оставлены как есть (fallback).
STAT_LABEL_SHORT = {
    "Relic": "Реликвия",
    "Total Life": "Общ.HP",
    "Protection": "Защита",
    "Physical Damage": "Атк.Ф",
    "Special Damage": "Атк.О",
    "Critical Chance": "КШ",
    "Physical Critical Chance": "КШ.Ф",
    "Special Critical Chance": "КШ.О",
    "Critical Damage": "КУ",
    "Critical Avoidance": "АКШ",
    "Physical Critical Avoidance": "АКШ.Ф",
    "Special Critical Avoidance": "АКШ.О",
    "Accuracy": "Точность",
    "Physical Accuracy": "Точн.Ф",
    "Special Accuracy": "Точн.О",
    "Health Steal": "Вампиризм",
    "Tenacity": "Стойкость",
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


def _parse_req_id(text: str):
    if not text:
        return None
    m = re.match(r"#(\d+)", text.strip())
    return int(m.group(1)) if m else None


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
        units = await activity_diff.fetch_player_units(bot.comlink, ally_code)
        if units:
            database.upsert_player_units(ally_code, units)
        unit = units.get(base_id)
        if unit:
            return unit, "только что"
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


def _stat_label(stat_name: str, priority: str) -> str:
    label = STAT_LABEL_SHORT.get(stat_name, stat_name)
    return f"{label}*" if priority == "optional" else label


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
    return f"{label}*" if priority == "optional" else label


def _mod_primary_option(slot_key: str, unit_stat_id) -> dict | None:
    for opt in stat_engine.MOD_PRIMARY_OPTIONS.get(slot_key, []):
        if opt["unit_stat"] == unit_stat_id:
            return opt
    return None


def _mod_primary_req_text(mod_slot: str, unit_stat_id) -> str:
    opt = _mod_primary_option(mod_slot, unit_stat_id)
    stat_label = opt["label"] if opt else f"#{unit_stat_id}"
    return f"{MOD_SLOT_LABELS.get(mod_slot, mod_slot)}: {stat_label}"


def _mod_primary_label(mod_slot: str, priority: str) -> str:
    label = f"Основа: {MOD_SLOT_LABELS.get(mod_slot, mod_slot)}"
    return f"{label}*" if priority == "optional" else label


def _compare_label(stat_name: str, compare_character_key: str, priority: str) -> str:
    label = f"{_stat_label(stat_name, '')} vs {_unit_display_name(compare_character_key)}"
    return f"{label}*" if priority == "optional" else label


def _compare_req_text(stat_name: str, operator: str, compare_character_key: str) -> str:
    return f"{_stat_label(stat_name, '')} {operator} {_unit_display_name(compare_character_key)}"


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
    return f"{label}*" if priority == "optional" else label


def _mod_set_req_text(set_id) -> str:
    set_id = int(set_id)
    pieces = stat_engine.MOD_SET_PIECE_COUNT.get(set_id, 2)
    return f"{_SET_NAME_BY_ID.get(set_id, set_id)} ({pieces} шт.)"


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
                set_id = int(r[5])
                required_pieces = stat_engine.MOD_SET_PIECE_COUNT.get(set_id, 2)
                if mod_set_counts.get(set_id, 0) >= required_pieces:
                    set_scores[r[14]] += 1

        if set_scores[1] != set_scores[2]:
            active_scheme = 1 if set_scores[1] > set_scores[2] else 2
        else:
            primary_scores = {1: 0, 2: 0}
            for r in rows:
                if r[3] == STAT_MOD_PRIMARY and r[14] in (1, 2):
                    actual = mod_primaries.get(r[12])
                    if actual is not None and actual == r[5]:
                        primary_scores[r[14]] += 1
            active_scheme = 1 if primary_scores[1] >= primary_scores[2] else 2
        is_forced = False

    scheme_label = scheme_labels.get(active_scheme, f"Схема {active_scheme}")
    return active_scheme, scheme_label, is_forced


def _load_char_rows(plate_name: str, base_id: str, guild_id: int = 1):
    """Общий префикс для обоих режимов расчёта: сохранённые требования персонажа в плейте,
    его отображаемое имя, требуемый по плейту релик, комментарии и лёгенда "опционально".
    Возвращает None, если для этого персонажа нет сохранённых требований."""
    rows = database.get_stat_requirements(plate_name, base_id, guild_id=guild_id)
    if not rows:
        return None
    char_name = _unit_display_name(base_id)
    relic_reqs = [r for r in rows if r[3] == "Relic"]
    required_relic = int(relic_reqs[0][5]) if relic_reqs else None
    comments = [r[8] for r in rows if r[8]]
    legend = " · * опционально" if any(r[6] == "optional" for r in rows) else ""
    return rows, char_name, required_relic, comments, legend


async def _evaluate_character_player(bot, plate_name: str, base_id: str, ally_code, force_refresh: bool, player_label, guild_id: int = 1, scenario: str = SCENARIO_RAW, forced_scheme: int | None = None):
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
    Возвращает None, если для этого персонажа нет сохранённых требований (пропускается в отчёте)."""
    loaded = _load_char_rows(plate_name, base_id, guild_id)
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

    # ModSet-требования (stat_name==STAT_SET) — как и Omicron/ModPrimary, не входят в
    # calc_final_stats: "надет ли сет" считается булевым фактом (достаточно ли деталей сета
    # реально экипировано, stat_engine.MOD_SET_PIECE_COUNT) и кладётся под ключом
    # "ModSet:<set_id>", сравнение оператором ">=" (как у Omicron) той же общей _compare-логикой.
    set_ids_needed = {int(row[5]) for row in rows if row[3] == STAT_SET}
    if set_ids_needed:
        for set_id in set_ids_needed:
            required_pieces = stat_engine.MOD_SET_PIECE_COUNT.get(set_id, 2)
            has_set = mod_set_counts.get(set_id, 0) >= required_pieces
            current_values[f"ModSet:{set_id}"] = 1.0 if has_set else 0.0

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
        caption += f" · Схема: {scheme_label} ({source})"
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
        is_compare = bool(compare_character_key)
        is_relic_row = stat_name == "Relic"

        compare_char_name = None
        if is_compare:
            # Сравнение с другим персонажем ТОГО ЖЕ игрока — заметка Коли 2026-09-20:
            # "офицер быстрее Хакса в ТП на Лкайло". threshold_value в строке — заглушка
            # (не используется), реальный порог — живой текущий стат compare-персонажа,
            # подставляется сюда и дальше течёт по той же общей логике _compare/сценариев,
            # что и обычный числовой порог.
            compare_char_name = _unit_display_name(compare_character_key)
            compare_unit, _ = await _get_unit_for_player(bot, ally_code, compare_character_key, force_refresh)
            if compare_unit is None:
                suffix = "*" if priority == "optional" else ""
                compare_lines.append(f"⚠️ {_stat_label(stat_name, '')} vs {compare_char_name}{suffix}: нет данных у сравниваемого")
                continue
            compare_stats = _with_total_life(dict(stat_engine.calc_final_stats(bot.stat_calc, compare_unit)))
            compare_stats["Relic"] = stat_engine.get_current_relic_level(compare_unit)
            threshold = compare_stats.get(stat_name)

        if is_mod_primary:
            label = _mod_primary_label(mod_slot, priority)
        elif is_omicron:
            label = _omicron_label(skill_id, priority)
        elif is_mod_set:
            label = _mod_set_label(threshold, priority)
        elif is_compare:
            label = _compare_label(stat_name, compare_character_key, priority)
        else:
            label = _stat_label(stat_name, priority)
        lookup_key = (
            f"ModPrimary:{mod_slot}" if is_mod_primary
            else f"Omicron:{skill_id}" if is_omicron
            else f"ModSet:{int(threshold)}" if is_mod_set
            else stat_name
        )
        if is_compare:
            req_cell = f"{operator} {compare_char_name}"
        elif is_mod_primary:
            req_cell = _mod_primary_req_text(mod_slot, threshold)
        elif is_omicron:
            req_cell = "разблокирован"
        elif is_mod_set:
            req_cell = "надет"
        else:
            req_cell = f"{operator} {_fmt_compact(threshold)}"
        cur_val = current_values.get(lookup_key)
        suffix = "*" if priority == "optional" else ""
        if cur_val is None or threshold is None:
            if is_omicron:
                omicron_lines.append(f"⚠️ {_omicron_ability_label(skill_id)}{suffix}: нет данных")
            elif is_mod_primary:
                mod_primary_lines.append(f"⚠️ {MOD_SLOT_LABELS.get(mod_slot, mod_slot)}{suffix}: нет данных")
            elif is_mod_set:
                mod_set_lines.append(f"⚠️ {_SET_NAME_BY_ID.get(int(threshold), threshold)}{suffix}: нет данных")
            elif is_compare:
                compare_lines.append(f"⚠️ {_stat_label(stat_name, '')} vs {compare_char_name}{suffix}: нет данных")
            else:
                table_rows.append([label, "нет данных", "—", "—", req_cell] if show_projection else [label, "нет данных", req_cell])
            continue
        cur_ok = _compare(cur_val, operator, threshold)
        if is_mod_primary:
            actual_opt = _mod_primary_option(mod_slot, cur_val)
            cur_cell = f"{actual_opt['label'] if actual_opt else f'#{int(cur_val)}'} {'✅' if cur_ok else '❌'}"
        elif is_omicron:
            cur_cell = "Есть ✅" if cur_ok else "Нет ❌"
        elif is_mod_set:
            cur_cell = "Надет ✅" if cur_ok else "Не надет ❌"
        elif is_compare:
            cur_cell = f"{_fmt_compact(cur_val)} vs {_fmt_compact(threshold)} {'✅' if cur_ok else '❌'}"
        else:
            cur_cell = f"{_fmt_compact(cur_val)} {'✅' if cur_ok else '❌'}"

        if is_omicron:
            omicron_lines.append(f"{cur_cell} — {_omicron_ability_label(skill_id)}{suffix}")
        elif is_mod_primary:
            slot_label = MOD_SLOT_LABELS.get(mod_slot, mod_slot)
            if cur_ok:
                mod_primary_lines.append(f"{cur_cell} — {slot_label}{suffix}")
            else:
                req_opt = _mod_primary_option(mod_slot, threshold)
                req_label = req_opt["label"] if req_opt else f"#{int(threshold)}"
                mod_primary_lines.append(f"{cur_cell} — {slot_label}{suffix} (нужно: {req_label})")
        elif is_mod_set:
            mod_set_lines.append(f"{cur_cell} — {_mod_set_req_text(threshold)}{suffix}")
        elif is_compare:
            compare_lines.append(f"{cur_cell} — {_stat_label(stat_name, '')} vs {compare_char_name}{suffix}")

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
                else "не надет" if is_mod_set
                else (actual_opt["label"] if actual_opt else "другая основа") if is_mod_primary
                else _fmt_compact(cur_val)
            )
            failed_required.append({
                "stat": label,
                "current": current_text,
                "requirement": requirement_text,
            })

    block = caption
    if table_rows:
        block += "\n" + _build_table(headers, table_rows)
    if omicron_lines:
        block += "\n**🧬 Омикроны:**\n" + "\n".join(omicron_lines)
    if mod_primary_lines:
        block += "\n**⚙️ Основы модов:**\n" + "\n".join(mod_primary_lines)
    if mod_set_lines:
        block += "\n**🔷 Сеты:**\n" + "\n".join(mod_set_lines)
    if compare_lines:
        block += "\n**⚔️ Сравнение с персонажами:**\n" + "\n".join(compare_lines)
    if comments:
        block += "\n" + "\n".join(f"💠 _{c}_" for c in comments)

    return char_name, block, matched, total, updated_at, failed_required, required_total, active_scheme, scheme_label


async def _build_guild_report(bot, plate_name: str, char_keys: list, guild_id: int = 1, scenario: str = SCENARIO_RAW, forced_scheme: int | None = None) -> dict:
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
    персонажа, а не только дробь matched/total."""
    roster = database.get_all_user_mappings(guild_id)
    if not roster:
        return {
            "error": "Никто из гильдии не зарегистрирован (/регистрация) — проверять некого.",
            "total_players": 0, "compliant": [], "problem": [], "no_data": [],
        }

    # required_relic на персонажа — не меняется от игрока к игроку, считаем один раз
    # (не в цикле по ростеру) и приклеиваем к каждой "проблемной" строке ниже: пригодится
    # для кнопки "поставить задачу" в вебе (см. web/routes/stat_forecast.py) — цель по
    # реликвии берётся прямо из требования плейта, без пересчёта.
    required_relic_by_char = {}
    for base_id in char_keys:
        loaded = _load_char_rows(plate_name, base_id, guild_id)
        if loaded is not None:
            required_relic_by_char[base_id] = loaded[2]

    compliant, problem, no_data = [], [], []
    for _discord_id, ally_code, name in roster:
        rows_total = 0  # ВСЕ приоритеты — только чтобы понять, есть ли вообще данные (no_data)
        required_total_all = 0
        required_matched_all = 0
        char_problems = []
        has_failed_required = False
        for base_id in char_keys:
            result = await _evaluate_character_player(bot, plate_name, base_id, ally_code, False, name, guild_id=guild_id, scenario=scenario, forced_scheme=forced_scheme)
            if result is None:
                continue
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
        if rows_total == 0:
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
    return {"error": None, "total_players": len(roster), "compliant": compliant, "problem": problem, "no_data": no_data}


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
        scheme_label = database.get_character_scheme_labels(scheme_leaf_plate, base_id, guild_id).get(active_scheme, f"Схема {active_scheme}")

    if required_relic is None:
        block = "⚠️ В требованиях плейта нет строки Relic — не от чего считать прибавку."
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
            cell = _mod_primary_req_text(mod_slot, threshold)
            table_rows.append([label, cell, cell])
            continue

        if stat_name == STAT_SET:
            # Надетый сет от релика не зависит — норма не меняется между релик-колонками.
            label = _mod_set_label(threshold, priority)
            cell = _mod_set_req_text(threshold)
            table_rows.append([label, cell, cell])
            continue

        if compare_character_key:
            # Сравнение с другим персонажем не проецируется на релик — оба живых значения
            # берутся как есть в момент проверки, норма не меняется между релик-колонками.
            label = _compare_label(stat_name, compare_character_key, priority)
            cell = _compare_req_text(stat_name, operator, compare_character_key)
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

    scheme_part = f" · Схема: {scheme_label}" if active_scheme is not None else ""
    block = f"Норма пересчитана с релика {required_relic} на {target_relic} (плоская часть + пропорциональный рост базы){scheme_part}" + legend + "\n" + _build_table(headers, table_rows)
    if comments:
        block += "\n" + "\n".join(f"💠 _{c}_" for c in comments)

    return char_name, block


# =====================================================================
# Автокомплиты (модульные функции — как autocomplete_players/autocomplete_datacron_*)
# =====================================================================
async def autocomplete_stat_plate(inter: disnake.ApplicationCommandInteraction, string: str):
    guild_id = guild_resolver.resolve_guild_id(inter.author)
    if guild_id is None:
        return []
    plates = database.get_all_stat_requirement_plates(guild_id=guild_id)
    if not plates:
        return ["❌ Список плейтов пуст."]
    search = string.lower().strip()
    return [p for p in plates if not search or search in p.lower()][:25]


async def autocomplete_stat_character(inter: disnake.ApplicationCommandInteraction, string: str):
    guild_id = guild_resolver.resolve_guild_id(inter.author)
    if guild_id is None:
        return []
    plate = inter.filled_options.get("плейт")
    if not plate:
        return ["⚠️ СНАЧАЛА выберите плейт!"]
    char_keys = database.get_stat_requirement_characters(plate, guild_id=guild_id)
    if not char_keys:
        return ["❌ У этого плейта нет сохранённых требований."]
    search = string.lower().strip()
    options = []
    for base_id in char_keys:
        label = f"{_unit_display_name(base_id)} [{base_id}]"
        if not search or search in label.lower():
            options.append(disnake.OptionChoice(name=label[:100], value=label))
    return options[:25]


async def autocomplete_stat_source_character(inter: disnake.ApplicationCommandInteraction, string: str):
    """Как autocomplete_stat_character, но читает выбранный параметр «источник» — используется
    в /статы_требования модуль_добавить/модуль_убрать, где «плейт» — это целевой модульный
    плейт, а не тот, из которого берётся персонаж."""
    guild_id = guild_resolver.resolve_guild_id(inter.author)
    if guild_id is None:
        return []
    source = inter.filled_options.get("источник")
    if not source:
        return ["⚠️ СНАЧАЛА выберите источник!"]
    char_keys = database.get_stat_requirement_characters(source, guild_id=guild_id)
    if not char_keys:
        return ["❌ У этого плейта нет сохранённых персонажей."]
    search = string.lower().strip()
    options = []
    for base_id in char_keys:
        label = f"{_unit_display_name(base_id)} [{base_id}]"
        if not search or search in label.lower():
            options.append(disnake.OptionChoice(name=label[:100], value=label))
    return options[:25]


async def autocomplete_omicron_phrase_character(inter: disnake.ApplicationCommandInteraction, string: str):
    rows = database.get_all_omicron_phrases()
    if not rows:
        return ["❌ Список пуст — фразы ещё не заданы."]
    search = string.lower().strip()
    options = []
    seen = set()
    for _, base_id, _skill_id, _phrase, _, _ in rows:
        if base_id in seen:
            continue
        seen.add(base_id)
        label = f"{_unit_display_name(base_id)} [{base_id}]"
        if not search or search in label.lower():
            options.append(disnake.OptionChoice(name=label[:100], value=label))
    return options[:25]


async def autocomplete_omicron_capable_character(inter: disnake.ApplicationCommandInteraction, string: str):
    """Только персонажи/корабли, у которых омикрон реально существует В ИГРЕ (game_units.
    has_omicron, обновляется в services/units_sync.py::sync_units из Comlink
    SkillDefinitions) — не привязано к тому, разблокировал ли его кто-то в этой
    конкретной гильдии (могли ещё не выдать), и не весь справочник game_units, где
    омикрона у большинства юнитов нет вовсе."""
    rows = database.get_all_omicron_capable_units()
    if not rows:
        return ["❌ Пока нет данных — справочник юнитов ещё не синхронизировался."]
    search = string.lower().strip()
    options = []
    for base_id, name in rows:
        label = f"{name} [{base_id}]"
        if not search or search in label.lower():
            options.append(disnake.OptionChoice(name=label[:100], value=label))
    return options[:25]


async def autocomplete_character_omicron_skill(inter: disnake.ApplicationCommandInteraction, string: str):
    """Требует, чтобы «персонаж» уже был выбран (тот же паттерн, что autocomplete_stat_character
    требует предварительного выбора «плейт») — у персонажа может быть больше одного омикрона
    (unit_omicron_skills), тут выбирается конкретный skill_id."""
    персонаж = inter.filled_options.get("персонаж")
    if not персонаж:
        return ["⚠️ СНАЧАЛА выберите персонажа!"]
    base_id = _parse_bracket_id(персонаж)
    options = _omicron_options_for_base(base_id)
    if not options:
        return ["❌ У этого персонажа нет омикронов в справочнике."]
    search = string.lower().strip()
    choices = []
    for skill_id, label in options:
        if not search or search in label.lower():
            choices.append(disnake.OptionChoice(name=label[:100], value=skill_id))
    return choices[:25]


async def autocomplete_mod_primary_for_slot(inter: disnake.ApplicationCommandInteraction, string: str):
    """Требует, чтобы «слот» уже был выбран (тот же паттерн, что autocomplete_character_omicron_skill
    требует предварительного выбора «персонаж») — легальные основы зависят от формы слота
    (stat_engine.MOD_PRIMARY_OPTIONS, square/diamond имеют всего один вариант)."""
    слот = inter.filled_options.get("слот")
    if not слот:
        return ["⚠️ СНАЧАЛА выберите слот!"]
    options = stat_engine.MOD_PRIMARY_OPTIONS.get(слот, [])
    if not options:
        return ["❌ Нет известных основ для этого слота."]
    search = string.lower().strip()
    choices = []
    for opt in options:
        if not search or search in opt["label"].lower():
            choices.append(disnake.OptionChoice(name=opt["label"][:100], value=str(opt["unit_stat"])))
    return choices[:25]


async def autocomplete_stat_req_id(inter: disnake.ApplicationCommandInteraction, string: str):
    guild_id = guild_resolver.resolve_guild_id(inter.author)
    if guild_id is None:
        return []
    rows = database.get_all_stat_requirements(guild_id=guild_id)
    if not rows:
        return ["❌ Список требований пуст."]
    search = string.lower().strip()
    options = []
    for row in rows:
        req_id, plate_name, character_key, stat_name, operator, threshold, priority, raw_text, comment, _, _, skill_id, mod_slot, compare_character_key, scheme_num = row
        char_name = _unit_display_name(character_key)
        if stat_name == STAT_OMICRON:
            label = f"#{req_id} [{PRIORITY_LABELS.get(priority, priority)}] {plate_name}: {char_name} — омикрон «{_omicron_ability_label(skill_id)}»"
        elif stat_name == STAT_MOD_PRIMARY:
            label = f"#{req_id} [{PRIORITY_LABELS.get(priority, priority)}] {plate_name}: {char_name} — основа «{_mod_primary_req_text(mod_slot, threshold)}»"
        elif stat_name == STAT_SET:
            label = f"#{req_id} [{PRIORITY_LABELS.get(priority, priority)}] {plate_name}: {char_name} — сет «{_mod_set_req_text(threshold)}»"
        elif compare_character_key:
            label = f"#{req_id} [{PRIORITY_LABELS.get(priority, priority)}] {plate_name}: {char_name} — {_compare_req_text(stat_name, operator, compare_character_key)}"
        else:
            label = f"#{req_id} [{PRIORITY_LABELS.get(priority, priority)}] {plate_name}: {char_name} {stat_name} {operator} {_fmt_value(threshold)}"
        if scheme_num:
            label += f" [Схема {scheme_num}]"
        if not search or search in label.lower():
            options.append(disnake.OptionChoice(name=label[:100], value=f"#{req_id}"))
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
        for event_id, ally_code, base_id, skill_id, guild_id in hits:
            if not feature_flags.is_enabled(guild_id, "omicron"):
                continue
            guild_cfg = database.get_guild_config(guild_id)
            channel_id = guild_cfg.get("omicron_channel_id") if guild_cfg else None
            if not channel_id:
                continue
            channel_id = int(channel_id)
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
                channel = channels_by_id[channel_id]
                phrase = _resolve_emoji_shortcodes(phrase, getattr(channel, "guild", None))
                line += f" {phrase}"
            items_by_channel.setdefault(channel_id, []).append((event_id, line))

        for channel_id, items in items_by_channel.items():
            channel = channels_by_id[channel_id]
            if len(items) >= self.OMICRON_COMBINE_THRESHOLD:
                header = f"🔁 Накопилось объявлений об омикронах: {len(items)} (бот был недоступен)"
                for chunk_event_ids, chunk_text in self._chunk_omicron_lines(header, items):
                    try:
                        await channel.send(chunk_text)
                    except Exception as e:
                        print(f"⚠️ [Омикрон] Не удалось отправить объединённое объявление в канал {channel_id}: {e}")
                        continue
                    for event_id in chunk_event_ids:
                        database.mark_activity_event_announced(event_id)
            else:
                for event_id, line in items:
                    try:
                        await channel.send(line)
                        database.mark_activity_event_announced(event_id)
                    except Exception as e:
                        print(f"⚠️ [Омикрон] Не удалось отправить объявление в канал {channel_id}: {e}")

    # ------------------ /омикрон_текст (автообъявления о выдаче омикронов) ------------------
    # Сама выдача детектится автоматически в player_units_sync_loop/_announce_omicrons
    # (сравнение снимков ростера, см. services/activity_diff.py) — эти команды только
    # настраивают канал и фразы-приписки к нему, ничего не публикуют напрямую.
    @commands.slash_command(name="омикрон_текст", description="Настройка автообъявлений о выдаче омикронов")
    async def omicron_group(self, inter: disnake.ApplicationCommandInteraction):
        pass

    @omicron_group.sub_command(name="канал", description="Задать канал, куда бот пишет объявления о выдаче омикронов")
    async def omicron_channel_set(
        self,
        inter: disnake.ApplicationCommandInteraction,
        канал: disnake.TextChannel = commands.Param(description="Канал для объявлений"),
    ):
        guild_id = await guild_resolver.require_guild_id(inter)
        if guild_id is None:
            return
        database.update_guild_config(guild_id, omicron_channel_id=str(канал.id))
        await inter.response.send_message(f"✅ Объявления о выдаче омикронов теперь идут в {канал.mention}.", ephemeral=True)

    @omicron_group.sub_command(name="фраза", description="Задать/обновить фразу-приписку для омикрона персонажа")
    async def omicron_phrase_set(
        self,
        inter: disnake.ApplicationCommandInteraction,
        персонаж: str = commands.Param(description="Персонаж", autocomplete=autocomplete_omicron_capable_character),
        текст: str = commands.Param(description="Текст, который бот допишет после шаблонного объявления"),
    ):
        base_id = _parse_bracket_id(персонаж)
        char_name = _unit_display_name(base_id)
        текст = текст.strip()
        database.set_omicron_phrase(base_id, текст, str(inter.author.id))
        reply = f"✅ Фраза для омикрона «{char_name}» сохранена: {текст}"
        guild_id = guild_resolver.resolve_guild_id(inter.author)
        warning = config_warning_text(database.get_guild_config(guild_id) if guild_id else None, "omicron")
        if warning:
            reply += f"\n{warning}"
        await inter.response.send_message(reply, ephemeral=True)

    @omicron_group.sub_command(name="удалить_фразу", description="Убрать фразу-приписку для омикрона персонажа")
    async def omicron_phrase_delete(
        self,
        inter: disnake.ApplicationCommandInteraction,
        персонаж: str = commands.Param(description="Персонаж", autocomplete=autocomplete_omicron_phrase_character),
    ):
        base_id = _parse_bracket_id(персонаж)
        char_name = _unit_display_name(base_id)
        if database.delete_omicron_phrase(base_id):
            await inter.response.send_message(f"✅ Фраза для «{char_name}» удалена.", ephemeral=True)
        else:
            await inter.response.send_message(f"❌ Для «{char_name}» фраза не была задана.", ephemeral=True)

    @omicron_group.sub_command(name="список", description="Показать все настроенные фразы для омикронов")
    async def omicron_phrase_list(self, inter: disnake.ApplicationCommandInteraction):
        rows = database.get_all_omicron_phrases()
        if not rows:
            await inter.response.send_message("Список пуст — фразы ещё не заданы.", ephemeral=True)
            return
        skill_info = database.get_skill_display_info([skill_id for _, _, skill_id, _, _, _ in rows if skill_id])
        lines = []
        for _, char_key, skill_id, phrase, _, _ in rows:
            char_name = _unit_display_name(char_key)
            if skill_id:
                ability_name, _ability_id, _ability_type, _omicron_mode = skill_info.get(skill_id, (skill_id, None, None, None))
                lines.append(f"**{char_name}** ({ability_name}) — {phrase}")
            else:
                lines.append(f"**{char_name}** (по умолчанию) — {phrase}")
        embeds = _lines_to_embeds("Фразы для омикронов", DATACRON_LIST_COLOR, lines)
        await inter.response.send_message(embed=embeds[0], ephemeral=True)
        for extra in embeds[1:]:
            await inter.followup.send(embed=extra, ephemeral=True)

    # ------------------ /омикроны отчёт (запрос из офицерского чата гильдии GR,
    # 2026-09-20: рекомендации "что поставить" были видны только в веб-дашборде
    # /omicrons/report, тут та же логика services/omicron_priority.py выведена в Discord). ------------------
    @commands.slash_command(name="омикроны", description="Отчёты по приоритетным для ВГ омикронам гильдии")
    async def omicrons_group(self, inter: disnake.ApplicationCommandInteraction):
        pass

    @omicrons_group.sub_command(name="отчёт", description="Какие приоритетные для ВГ омикроны игрок уже готов поставить, но не поставил")
    async def omicrons_report_player(
        self,
        inter: disnake.ApplicationCommandInteraction,
        игрок: str = commands.Param(default=None, description="Игрок гильдии — если не указан, берётся ваша регистрация (/регистрация)", autocomplete=autocomplete_players),
    ):
        await inter.response.defer(ephemeral=True)

        guild_id = await guild_resolver.require_feature(inter, "omicron")
        if guild_id is None:
            return

        if игрок is None:
            registration = database.get_user_registration(str(inter.author.id), guild_id=guild_id)
            if not registration:
                await inter.edit_original_response("❌ Игрок не указан, а вы не зарегистрированы — используйте `/регистрация` или укажите игрока явно.")
                return
            ally_code, игрок = registration
        else:
            cache = self.bot.guild_roster_caches.get(guild_id, {})
            ally_code = cache.get(игрок)
            if not ally_code:
                await inter.edit_original_response("❌ Игрок не найден в составе гильдии.")
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
    @commands.slash_command(name="статы_требования", description="Управление требованиями к статам персонажей по плейтам")
    async def stat_req(self, inter: disnake.ApplicationCommandInteraction):
        pass

    @stat_req.sub_command(name="добавить", description="Добавить требование к стату персонажа в плейт")
    async def stat_req_add(
        self,
        inter: disnake.ApplicationCommandInteraction,
        плейт: str = commands.Param(description="Плейт (как в HotUtils, например AC_ALL)", autocomplete=autocomplete_stat_plate),
        персонаж: str = commands.Param(description="Персонаж", autocomplete=units_autocomplete),
        стат: str = commands.Param(description="Какой стат проверяем (или Relic для уровня реликвии)", choices=STAT_CHOICES),
        оператор: str = commands.Param(description="Оператор сравнения", choices=OPERATOR_CHOICES),
        значение: float = commands.Param(description="Пороговое значение"),
        приоритет: str = commands.Param(default=PRIORITY_REQUIRED, description="Приоритет требования", choices=PRIORITY_CHOICES),
        комментарий: str = commands.Param(default=None, description="Заметка"),
        схема: str = commands.Param(default=None, description="Только для одной из двух схем мод-билда персонажа (если не задано — общее для обеих)", choices=SCHEME_CHOICES),
    ):
        guild_id = await guild_resolver.require_feature(inter, "stat_requirements")
        if guild_id is None:
            return

        if плейт not in database.get_all_stat_requirement_plates(guild_id=guild_id):
            await inter.response.send_message(
                f"❌ Плейт «{плейт}» не найден — выберите вариант из списка автодополнения либо создайте его сначала через /статы_требования создать.",
                ephemeral=True,
            )
            return
        if database.is_stat_plate_modular(плейт, guild_id=guild_id):
            await inter.response.send_message(
                f"❌ «{плейт}» — модульный плейт, в него нельзя добавлять требования напрямую. "
                f"Используйте /статы_требования модуль_добавить, чтобы подключить другой плейт или персонажа из него.",
                ephemeral=True,
            )
            return

        base_id = _parse_bracket_id(персонаж)
        char_name = _unit_display_name(base_id)
        raw_text = f"{char_name} {стат} {оператор} {_fmt_value(значение)}"
        req_id = database.add_stat_requirement(
            плейт, base_id, стат, оператор, значение, приоритет, raw_text, комментарий, str(inter.author.id),
            guild_id=guild_id, scheme_num=_parse_scheme_param(схема),
        )
        await inter.response.send_message(f"✅ Требование #{req_id} [{PRIORITY_LABELS[приоритет]}] добавлено: {raw_text}", ephemeral=True)

    @stat_req.sub_command(name="добавить_омикрон", description="Добавить требование на разблокированный омикрон персонажа в плейт")
    async def stat_req_add_omicron(
        self,
        inter: disnake.ApplicationCommandInteraction,
        плейт: str = commands.Param(description="Плейт (как в HotUtils, например AC_ALL)", autocomplete=autocomplete_stat_plate),
        персонаж: str = commands.Param(description="Персонаж/корабль с омикроном", autocomplete=autocomplete_omicron_capable_character),
        омикрон: str = commands.Param(description="Какой именно омикрон (если их несколько)", autocomplete=autocomplete_character_omicron_skill),
        приоритет: str = commands.Param(default=PRIORITY_REQUIRED, description="Приоритет требования", choices=PRIORITY_CHOICES),
        комментарий: str = commands.Param(default=None, description="Заметка"),
        схема: str = commands.Param(default=None, description="Только для одной из двух схем мод-билда персонажа (если не задано — общее для обеих)", choices=SCHEME_CHOICES),
    ):
        guild_id = await guild_resolver.require_feature(inter, "stat_requirements")
        if guild_id is None:
            return

        if плейт not in database.get_all_stat_requirement_plates(guild_id=guild_id):
            await inter.response.send_message(
                f"❌ Плейт «{плейт}» не найден — выберите вариант из списка автодополнения либо создайте его сначала через /статы_требования создать.",
                ephemeral=True,
            )
            return
        if database.is_stat_plate_modular(плейт, guild_id=guild_id):
            await inter.response.send_message(
                f"❌ «{плейт}» — модульный плейт, в него нельзя добавлять требования напрямую. "
                f"Используйте /статы_требования модуль_добавить, чтобы подключить другой плейт или персонажа из него.",
                ephemeral=True,
            )
            return

        base_id = _parse_bracket_id(персонаж)
        valid_skill_ids = {sid for sid, _ in _omicron_options_for_base(base_id)}
        if омикрон not in valid_skill_ids:
            await inter.response.send_message("❌ Выберите омикрон из списка автодополнения — у персонажа нет такого.", ephemeral=True)
            return

        char_name = _unit_display_name(base_id)
        ability_name = _omicron_ability_label(омикрон)
        raw_text = f"{char_name} — омикрон «{ability_name}»"
        req_id = database.add_stat_requirement(
            плейт, base_id, STAT_OMICRON, ">=", 1.0, приоритет, raw_text, комментарий, str(inter.author.id),
            guild_id=guild_id, skill_id=омикрон, scheme_num=_parse_scheme_param(схема),
        )
        await inter.response.send_message(f"✅ Требование #{req_id} [{PRIORITY_LABELS[приоритет]}] добавлено: {raw_text}", ephemeral=True)

    @stat_req.sub_command(name="добавить_основу", description="Добавить требование на основу (primary-стат) конкретного слота мода")
    async def stat_req_add_mod_primary(
        self,
        inter: disnake.ApplicationCommandInteraction,
        плейт: str = commands.Param(description="Плейт (как в HotUtils, например AC_ALL)", autocomplete=autocomplete_stat_plate),
        персонаж: str = commands.Param(description="Персонаж", autocomplete=units_autocomplete),
        слот: str = commands.Param(description="Форма слота мода", choices=MOD_SLOT_CHOICES),
        основа: str = commands.Param(description="Требуемая основа для этого слота", autocomplete=autocomplete_mod_primary_for_slot),
        приоритет: str = commands.Param(default=PRIORITY_REQUIRED, description="Приоритет требования", choices=PRIORITY_CHOICES),
        комментарий: str = commands.Param(default=None, description="Заметка"),
        схема: str = commands.Param(default=None, description="Только для одной из двух схем мод-билда персонажа (если не задано — общее для обеих)", choices=SCHEME_CHOICES),
    ):
        guild_id = await guild_resolver.require_feature(inter, "stat_requirements")
        if guild_id is None:
            return

        if плейт not in database.get_all_stat_requirement_plates(guild_id=guild_id):
            await inter.response.send_message(
                f"❌ Плейт «{плейт}» не найден — выберите вариант из списка автодополнения либо создайте его сначала через /статы_требования создать.",
                ephemeral=True,
            )
            return
        if database.is_stat_plate_modular(плейт, guild_id=guild_id):
            await inter.response.send_message(
                f"❌ «{плейт}» — модульный плейт, в него нельзя добавлять требования напрямую. "
                f"Используйте /статы_требования модуль_добавить, чтобы подключить другой плейт или персонажа из него.",
                ephemeral=True,
            )
            return

        try:
            unit_stat_id = int(основа)
        except ValueError:
            await inter.response.send_message("❌ Выберите основу из списка автодополнения.", ephemeral=True)
            return
        if _mod_primary_option(слот, unit_stat_id) is None:
            await inter.response.send_message("❌ Эта основа недопустима для выбранного слота — выберите вариант из списка автодополнения.", ephemeral=True)
            return

        base_id = _parse_bracket_id(персонаж)
        char_name = _unit_display_name(base_id)
        raw_text = f"{char_name} — основа «{_mod_primary_req_text(слот, unit_stat_id)}»"
        req_id = database.add_stat_requirement(
            плейт, base_id, STAT_MOD_PRIMARY, "=", float(unit_stat_id), приоритет, raw_text, комментарий, str(inter.author.id),
            guild_id=guild_id, mod_slot=слот, scheme_num=_parse_scheme_param(схема),
        )
        await inter.response.send_message(f"✅ Требование #{req_id} [{PRIORITY_LABELS[приоритет]}] добавлено: {raw_text}", ephemeral=True)

    @stat_req.sub_command(name="добавить_сет", description="Добавить требование на надетый сет модов (тот же справочник сетов, что в Калькуляторе)")
    async def stat_req_add_mod_set(
        self,
        inter: disnake.ApplicationCommandInteraction,
        плейт: str = commands.Param(description="Плейт (как в HotUtils, например AC_ALL)", autocomplete=autocomplete_stat_plate),
        персонаж: str = commands.Param(description="Персонаж", autocomplete=units_autocomplete),
        сет: int = commands.Param(description="Требуемый сет модов", choices=SET_CHOICES),
        приоритет: str = commands.Param(default=PRIORITY_REQUIRED, description="Приоритет требования", choices=PRIORITY_CHOICES),
        комментарий: str = commands.Param(default=None, description="Заметка"),
        схема: str = commands.Param(default=None, description="Только для одной из двух схем мод-билда персонажа (если не задано — общее для обеих)", choices=SCHEME_CHOICES),
    ):
        guild_id = await guild_resolver.require_feature(inter, "stat_requirements")
        if guild_id is None:
            return

        if плейт not in database.get_all_stat_requirement_plates(guild_id=guild_id):
            await inter.response.send_message(
                f"❌ Плейт «{плейт}» не найден — выберите вариант из списка автодополнения либо создайте его сначала через /статы_требования создать.",
                ephemeral=True,
            )
            return
        if database.is_stat_plate_modular(плейт, guild_id=guild_id):
            await inter.response.send_message(
                f"❌ «{плейт}» — модульный плейт, в него нельзя добавлять требования напрямую. "
                f"Используйте /статы_требования модуль_добавить, чтобы подключить другой плейт или персонажа из него.",
                ephemeral=True,
            )
            return

        base_id = _parse_bracket_id(персонаж)
        char_name = _unit_display_name(base_id)
        raw_text = f"{char_name} — сет «{_mod_set_req_text(сет)}»"
        req_id = database.add_stat_requirement(
            плейт, base_id, STAT_SET, ">=", float(сет), приоритет, raw_text, комментарий, str(inter.author.id),
            guild_id=guild_id, scheme_num=_parse_scheme_param(схема),
        )
        await inter.response.send_message(f"✅ Требование #{req_id} [{PRIORITY_LABELS[приоритет]}] добавлено: {raw_text}", ephemeral=True)

    @stat_req.sub_command(
        name="добавить_сравнение",
        description="Добавить требование на сравнение стата с другим персонажем того же игрока (например «быстрее X»)",
    )
    async def stat_req_add_compare(
        self,
        inter: disnake.ApplicationCommandInteraction,
        плейт: str = commands.Param(description="Плейт (как в HotUtils, например AC_ALL)", autocomplete=autocomplete_stat_plate),
        персонаж: str = commands.Param(description="Персонаж, к которому относится требование", autocomplete=units_autocomplete),
        стат: str = commands.Param(description="Какой стат сравниваем", choices=[c for c in STAT_CHOICES if c.value != "Relic"]),
        оператор: str = commands.Param(description="Оператор сравнения (стат персонажа ОПЕРАТОР стат сравниваемого)", choices=OPERATOR_CHOICES),
        персонаж_сравнения: str = commands.Param(description="С кем сравниваем (тот же игрок, другой персонаж)", autocomplete=units_autocomplete),
        приоритет: str = commands.Param(default=PRIORITY_REQUIRED, description="Приоритет требования", choices=PRIORITY_CHOICES),
        комментарий: str = commands.Param(default=None, description="Заметка"),
        схема: str = commands.Param(default=None, description="Только для одной из двух схем мод-билда персонажа (если не задано — общее для обеих)", choices=SCHEME_CHOICES),
    ):
        guild_id = await guild_resolver.require_feature(inter, "stat_requirements")
        if guild_id is None:
            return

        if плейт not in database.get_all_stat_requirement_plates(guild_id=guild_id):
            await inter.response.send_message(
                f"❌ Плейт «{плейт}» не найден — выберите вариант из списка автодополнения либо создайте его сначала через /статы_требования создать.",
                ephemeral=True,
            )
            return
        if database.is_stat_plate_modular(плейт, guild_id=guild_id):
            await inter.response.send_message(
                f"❌ «{плейт}» — модульный плейт, в него нельзя добавлять требования напрямую. "
                f"Используйте /статы_требования модуль_добавить, чтобы подключить другой плейт или персонажа из него.",
                ephemeral=True,
            )
            return

        base_id = _parse_bracket_id(персонаж)
        compare_base_id = _parse_bracket_id(персонаж_сравнения)
        if compare_base_id == base_id:
            await inter.response.send_message("❌ Нельзя сравнивать персонажа с самим собой — выберите другого персонажа для сравнения.", ephemeral=True)
            return

        char_name = _unit_display_name(base_id)
        raw_text = f"{char_name} — {_compare_req_text(стат, оператор, compare_base_id)}"
        req_id = database.add_stat_requirement(
            плейт, base_id, стат, оператор, 0.0, приоритет, raw_text, комментарий, str(inter.author.id),
            guild_id=guild_id, compare_character_key=compare_base_id, scheme_num=_parse_scheme_param(схема),
        )
        await inter.response.send_message(f"✅ Требование #{req_id} [{PRIORITY_LABELS[приоритет]}] добавлено: {raw_text}", ephemeral=True)

    @stat_req.sub_command(name="редактировать", description="Изменить требование к статам или удалить его")
    async def stat_req_edit(
        self,
        inter: disnake.ApplicationCommandInteraction,
        id: str = commands.Param(description="Требование для изменения", autocomplete=autocomplete_stat_req_id),
        значение: float = commands.Param(default=None, description="Новое пороговое значение"),
        оператор: str = commands.Param(default=None, description="Новый оператор", choices=OPERATOR_CHOICES),
        приоритет: str = commands.Param(default=None, description="Новый приоритет", choices=PRIORITY_CHOICES),
        комментарий: str = commands.Param(default=None, description="Новый комментарий"),
        схема: str = commands.Param(default=None, description="Перепривязать к другой схеме мод-билда (не меняет остальные поля)", choices=SCHEME_CHOICES),
        удалить: bool = commands.Param(default=False, description="Удалить это требование вместо редактирования"),
    ):
        guild_id = await guild_resolver.require_feature(inter, "stat_requirements")
        if guild_id is None:
            return

        req_id = _parse_req_id(id)
        if req_id is None:
            await inter.response.send_message("❌ Некорректный id — выберите вариант из списка автодополнения.", ephemeral=True)
            return
        row = database.get_stat_requirement(req_id, guild_id=guild_id)
        if not row:
            await inter.response.send_message(f"❌ Требование #{req_id} не найдено.", ephemeral=True)
            return

        if удалить:
            database.delete_stat_requirement(req_id, guild_id=guild_id)
            await inter.response.send_message(f"🗑️ Требование #{req_id} удалено.", ephemeral=True)
            return

        if схема is not None:
            database.set_stat_requirement_scheme(req_id, _parse_scheme_param(схема), guild_id=guild_id)

        _, plate_name, character_key, stat_name, cur_operator, cur_threshold, cur_priority, _raw_text, cur_comment, _, _, _skill_id, _mod_slot, compare_character_key, _scheme_num = row
        locked = stat_name in (STAT_OMICRON, STAT_MOD_PRIMARY, STAT_SET) or bool(compare_character_key)

        if stat_name == STAT_OMICRON and (оператор is not None or значение is not None):
            await inter.response.send_message(
                "❌ У требования на омикрон нельзя менять оператор/значение — доступны только приоритет и комментарий.",
                ephemeral=True,
            )
            return

        if stat_name == STAT_MOD_PRIMARY and (оператор is not None or значение is not None):
            await inter.response.send_message(
                "❌ У требования на основу мода нельзя менять оператор/значение — доступны только приоритет и комментарий. "
                "Чтобы сменить слот/основу, удалите это требование и добавьте новое.",
                ephemeral=True,
            )
            return

        if stat_name == STAT_SET and (оператор is not None or значение is not None):
            await inter.response.send_message(
                "❌ У требования на сет нельзя менять оператор/значение — доступны только приоритет и комментарий. "
                "Чтобы сменить сет, удалите это требование и добавьте новое.",
                ephemeral=True,
            )
            return

        if compare_character_key and (оператор is not None or значение is not None):
            await inter.response.send_message(
                "❌ У требования на сравнение с другим персонажем нельзя менять оператор/значение — доступны только "
                "приоритет и комментарий. Чтобы сменить стат/сравниваемого персонажа, удалите это требование и добавьте новое.",
                ephemeral=True,
            )
            return

        new_operator = оператор if оператор is not None else cur_operator
        new_threshold = значение if значение is not None else cur_threshold
        new_priority = приоритет if приоритет is not None else cur_priority
        new_comment = комментарий if комментарий is not None else cur_comment
        char_name = _unit_display_name(character_key)
        new_raw_text = _raw_text if locked else f"{char_name} {stat_name} {new_operator} {_fmt_value(new_threshold)}"
        database.update_stat_requirement(req_id, plate_name, character_key, stat_name, new_operator, new_threshold, new_priority, new_comment, guild_id=guild_id)
        scheme_part = f" · схема: {_parse_scheme_param(схема) or 'обе'}" if схема is not None else ""
        await inter.response.send_message(f"✅ Требование #{req_id} обновлено: {new_raw_text}{scheme_part}", ephemeral=True)

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
                scheme_part = f" · [{scheme_labels.get(scheme_num, f'Схема {scheme_num}')}]" if scheme_num else ""
                lines.append(f"`#{req_id}` {PRIORITY_EMOJI.get(priority, '')} {raw_text}{comment_part}{scheme_part}")

        embeds = _lines_to_embeds(f"📋 {плейт}", DATACRON_LIST_COLOR, lines)
        if not embeds:
            await inter.edit_original_response("❌ Нет требований для этого плейта.")
            return
        await inter.edit_original_response(embed=embeds[0])
        for e in embeds[1:]:
            await inter.followup.send(embed=e, ephemeral=True)

    @stat_req.sub_command(name="создать", description="Зарегистрировать новый плейт (набор норм статов)")
    async def stat_req_create_plate(
        self,
        inter: disnake.ApplicationCommandInteraction,
        плейт: str = commands.Param(description="Название нового плейта (как в HotUtils, например AC_ALL)"),
        описание: str = commands.Param(default=None, description="Заметка о плейте"),
        модульный: bool = commands.Param(
            default=False,
            description="Не содержит персонажей напрямую — ссылается на другие плейты (см. модуль_добавить)",
        ),
    ):
        guild_id = await guild_resolver.require_feature(inter, "stat_requirements")
        if guild_id is None:
            return

        created = database.create_stat_plate(плейт, описание, str(inter.author.id), guild_id=guild_id, modular=модульный)
        if not created:
            await inter.response.send_message(f"❌ Плейт «{плейт}» уже существует.", ephemeral=True)
            return
        suffix = f" · _{описание}_" if описание else ""
        if модульный:
            await inter.response.send_message(
                f"✅ Модульный плейт «{плейт}» создан.{suffix} Добавьте в него плейты/персонажей через /статы_требования модуль_добавить.",
                ephemeral=True,
            )
            return
        await inter.response.send_message(f"✅ Плейт «{плейт}» создан.{suffix}", ephemeral=True)

    @stat_req.sub_command(name="плейты", description="Показать список всех плейтов")
    async def stat_req_list_plates(self, inter: disnake.ApplicationCommandInteraction):
        await inter.response.defer(ephemeral=True)
        guild_id = await guild_resolver.require_feature(inter, "stat_requirements")
        if guild_id is None:
            return

        rows = database.get_all_stat_plates_detailed(guild_id=guild_id)
        if not rows:
            await inter.edit_original_response("❌ Плейтов пока нет — создайте через /статы_требования создать.")
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

    @stat_req.sub_command(name="переименовать", description="Переименовать плейт")
    async def stat_req_rename_plate(
        self,
        inter: disnake.ApplicationCommandInteraction,
        плейт: str = commands.Param(description="Плейт для переименования", autocomplete=autocomplete_stat_plate),
        новое_имя: str = commands.Param(description="Новое название плейта"),
    ):
        guild_id = await guild_resolver.require_feature(inter, "stat_requirements")
        if guild_id is None:
            return

        ok = database.rename_stat_plate(плейт, новое_имя, guild_id=guild_id)
        if not ok:
            await inter.response.send_message(
                f"❌ Не удалось переименовать: плейт «{плейт}» не найден либо «{новое_имя}» уже занято другим плейтом.",
                ephemeral=True,
            )
            return
        await inter.response.send_message(f"✅ Плейт «{плейт}» переименован в «{новое_имя}».", ephemeral=True)

    @stat_req.sub_command(name="описание", description="Изменить заметку/описание уже существующего плейта")
    async def stat_req_set_plate_description(
        self,
        inter: disnake.ApplicationCommandInteraction,
        плейт: str = commands.Param(description="Плейт", autocomplete=autocomplete_stat_plate),
        описание: str = commands.Param(description="Новый текст заметки (полностью заменяет старый)"),
    ):
        guild_id = await guild_resolver.require_feature(inter, "stat_requirements")
        if guild_id is None:
            return

        ok = database.update_stat_plate_description(плейт, описание, guild_id=guild_id)
        if not ok:
            await inter.response.send_message(f"❌ Плейт «{плейт}» не найден.", ephemeral=True)
            return
        await inter.response.send_message(f"✅ Описание плейта «{плейт}» обновлено: _{описание}_", ephemeral=True)

    @stat_req.sub_command(name="схема_переименовать", description="Задать название схемы мод-билда персонажа (например «Скорость»/«КД»)")
    async def stat_req_rename_scheme(
        self,
        inter: disnake.ApplicationCommandInteraction,
        плейт: str = commands.Param(description="Плейт", autocomplete=autocomplete_stat_plate),
        персонаж: str = commands.Param(description="Персонаж", autocomplete=autocomplete_stat_character),
        номер: str = commands.Param(description="Какая из двух схем", choices=[disnake.OptionChoice(name="Схема 1", value="1"), disnake.OptionChoice(name="Схема 2", value="2")]),
        название: str = commands.Param(description="Новое название схемы"),
    ):
        guild_id = await guild_resolver.require_feature(inter, "stat_requirements")
        if guild_id is None:
            return

        base_id = _parse_bracket_id(персонаж)
        char_name = _unit_display_name(base_id)
        database.set_character_scheme_label(плейт, base_id, int(номер), название.strip(), guild_id=guild_id)
        await inter.response.send_message(f"✅ Схема {номер} для «{char_name}» в «{плейт}» теперь называется «{название.strip()}».", ephemeral=True)

    @stat_req.sub_command(name="удалить", description="Удалить плейт целиком, либо одного персонажа из плейта (если указан)")
    async def stat_req_delete_plate(
        self,
        inter: disnake.ApplicationCommandInteraction,
        плейт: str = commands.Param(description="Плейт для удаления", autocomplete=autocomplete_stat_plate),
        персонаж: str = commands.Param(default=None, description="Персонаж из плейта (если не указан — удаляется весь плейт)", autocomplete=autocomplete_stat_character),
        подтвердить: bool = commands.Param(default=False, description="Установите true только после проверки количества требований для удаления"),
    ):
        guild_id = await guild_resolver.require_feature(inter, "stat_requirements")
        if guild_id is None:
            return

        is_modular = database.is_stat_plate_modular(плейт, guild_id=guild_id)

        if персонаж:
            if is_modular:
                await inter.response.send_message(
                    f"❌ «{плейт}» — модульный плейт, персонажи в нём приходят из подключённых плейтов. "
                    f"Используйте /статы_требования модуль_убрать.",
                    ephemeral=True,
                )
                return
            base_id = _parse_bracket_id(персонаж)
            char_name = _unit_display_name(base_id)
            count = database.count_stat_requirements_by_character(плейт, base_id, guild_id=guild_id)
            if count == 0:
                await inter.response.send_message(f"❌ У персонажа «{char_name}» нет требований в плейте «{плейт}».", ephemeral=True)
                return

            if not подтвердить:
                await inter.response.send_message(
                    f"⚠️ Будет удалён персонаж «{char_name}» из плейта «{плейт}» вместе с его требованиями: {count}. "
                    f"Повторите команду с подтвердить=True, чтобы подтвердить удаление.",
                    ephemeral=True,
                )
                return

            deleted = database.delete_stat_requirements_by_character(плейт, base_id, guild_id=guild_id)
            await inter.response.send_message(f"🗑️ Персонаж «{char_name}» удалён из плейта «{плейт}» вместе с требованиями: {deleted}.", ephemeral=True)
            return

        if is_modular:
            components = database.get_stat_plate_components(плейт, guild_id=guild_id)
            count = len(components)
        else:
            count = database.count_stat_requirements_by_plate(плейт, guild_id=guild_id)
        if database.get_stat_plate(плейт, guild_id=guild_id) is None and count == 0:
            await inter.response.send_message(f"❌ Плейт «{плейт}» не найден.", ephemeral=True)
            return

        referenced_by = [p for p in database.get_stat_plates_referencing(плейт, guild_id=guild_id) if p != плейт]
        warn_part = ""
        if referenced_by:
            warn_part = f" ⚠️ Этот плейт используется как компонент в: {', '.join(f'«{p}»' for p in referenced_by)} — там он просто перестанет что-то разворачивать."

        if not подтвердить:
            what = "компонентов состава" if is_modular else "требований"
            await inter.response.send_message(
                f"⚠️ Будет удалён плейт «{плейт}» и его {what}: {count}.{warn_part} "
                f"Повторите команду с подтвердить=True, чтобы подтвердить удаление.",
                ephemeral=True,
            )
            return

        deleted = database.delete_stat_plate(плейт, guild_id=guild_id)
        await inter.response.send_message(f"🗑️ Плейт «{плейт}» удалён вместе с требованиями: {deleted}.", ephemeral=True)

    # ------------------ Состав модульных плейтов ------------------
    @stat_req.sub_command(name="модуль_добавить", description="Подключить плейт (целиком либо одного персонажа из него) к модульному плейту")
    async def stat_req_module_add(
        self,
        inter: disnake.ApplicationCommandInteraction,
        плейт: str = commands.Param(description="Целевой МОДУЛЬНЫЙ плейт", autocomplete=autocomplete_stat_plate),
        источник: str = commands.Param(description="Плейт, который подключаем", autocomplete=autocomplete_stat_plate),
        персонаж: str = commands.Param(default=None, description="Только этот персонаж из источника (если не указан — источник целиком)", autocomplete=autocomplete_stat_source_character),
    ):
        guild_id = await guild_resolver.require_feature(inter, "stat_requirements")
        if guild_id is None:
            return

        if not database.is_stat_plate_modular(плейт, guild_id=guild_id):
            await inter.response.send_message(
                f"❌ «{плейт}» не модульный плейт (или не найден) — создайте его через /статы_требования создать с модульный=True.",
                ephemeral=True,
            )
            return
        if источник not in database.get_all_stat_requirement_plates(guild_id=guild_id):
            await inter.response.send_message(f"❌ Плейт-источник «{источник}» не найден.", ephemeral=True)
            return

        character_key = _parse_bracket_id(персонаж) if персонаж else ""
        existing = [(sp, ck) for _id, sp, ck in database.get_stat_plate_components(плейт, guild_id=guild_id)]
        if (источник, character_key) in existing:
            label = _unit_display_name(character_key) if character_key else "весь плейт"
            await inter.response.send_message(f"❌ «{источник}» ({label}) уже подключён к «{плейт}».", ephemeral=True)
            return

        ok, error = database.set_stat_plate_components(плейт, existing + [(источник, character_key)], str(inter.author.id), guild_id=guild_id)
        if not ok:
            await inter.response.send_message(f"❌ {error}", ephemeral=True)
            return

        label = _unit_display_name(character_key) if character_key else "весь плейт"
        await inter.response.send_message(f"✅ «{источник}» ({label}) подключён к модульному плейту «{плейт}».", ephemeral=True)

    @stat_req.sub_command(name="модуль_убрать", description="Отключить плейт/персонажа от модульного плейта")
    async def stat_req_module_remove(
        self,
        inter: disnake.ApplicationCommandInteraction,
        плейт: str = commands.Param(description="Целевой модульный плейт", autocomplete=autocomplete_stat_plate),
        источник: str = commands.Param(description="Подключённый плейт", autocomplete=autocomplete_stat_plate),
        персонаж: str = commands.Param(default=None, description="Только этот персонаж (если не указан — убирается вся ссылка на источник целиком)", autocomplete=autocomplete_stat_source_character),
    ):
        guild_id = await guild_resolver.require_feature(inter, "stat_requirements")
        if guild_id is None:
            return

        character_key = _parse_bracket_id(персонаж) if персонаж else ""
        existing = [(sp, ck) for _id, sp, ck in database.get_stat_plate_components(плейт, guild_id=guild_id)]
        if (источник, character_key) not in existing:
            await inter.response.send_message(f"❌ «{источник}» не подключён к «{плейт}» в таком виде.", ephemeral=True)
            return

        remaining = [c for c in existing if c != (источник, character_key)]
        ok, error = database.set_stat_plate_components(плейт, remaining, str(inter.author.id), guild_id=guild_id)
        if not ok:
            await inter.response.send_message(f"❌ {error}", ephemeral=True)
            return

        label = _unit_display_name(character_key) if character_key else "весь плейт"
        await inter.response.send_message(f"🗑️ «{источник}» ({label}) отключён от модульного плейта «{плейт}».", ephemeral=True)

    @stat_req.sub_command(name="модуль_список", description="Показать состав модульного плейта")
    async def stat_req_module_list(
        self,
        inter: disnake.ApplicationCommandInteraction,
        плейт: str = commands.Param(description="Модульный плейт", autocomplete=autocomplete_stat_plate),
    ):
        await inter.response.defer(ephemeral=True)
        guild_id = await guild_resolver.require_feature(inter, "stat_requirements")
        if guild_id is None:
            return

        if not database.is_stat_plate_modular(плейт, guild_id=guild_id):
            await inter.edit_original_response(f"❌ «{плейт}» не модульный плейт.")
            return

        components = database.get_stat_plate_components(плейт, guild_id=guild_id)
        if not components:
            await inter.edit_original_response(f"❌ У модульного плейта «{плейт}» пока нет ни одного компонента — добавьте через /статы_требования модуль_добавить.")
            return

        lines = []
        for _id, source_plate, character_key in components:
            if character_key:
                lines.append(f"`{source_plate}` → {_unit_display_name(character_key)}")
            else:
                lines.append(f"`{source_plate}` → весь плейт")

        char_count = len(database.get_stat_requirement_characters(плейт, guild_id=guild_id))
        lines.append("")
        lines.append(f"Итого развёрнуто персонажей: {char_count}")

        embeds = _lines_to_embeds(f"🧩 {плейт} — состав", DATACRON_LIST_COLOR, lines)
        await inter.edit_original_response(embed=embeds[0])
        for e in embeds[1:]:
            await inter.followup.send(embed=e, ephemeral=True)

    # ------------------ /статы (открытая команда) ------------------
    @commands.slash_command(name="статы", description="Прогноз статов персонажа(ей) игрока на релик плейта относительно требований")
    async def stats_check(
        self,
        inter: disnake.ApplicationCommandInteraction,
        плейт: str = commands.Param(description="Плейт (набор требований)", autocomplete=autocomplete_stat_plate),
        игрок: str = commands.Param(default=None, description="Игрок гильдии — если не указан, берётся ваша регистрация (/регистрация)", autocomplete=autocomplete_players),
        аликод: str = commands.Param(default=None, description="Код союзника — для игрока не из нашей гильдии, вместо параметра «игрок»"),
        персонаж: str = commands.Param(default=None, description="Персонаж из плейта (если не указан — весь плейт)", autocomplete=autocomplete_stat_character),
        обновить: bool = commands.Param(default=False, description="Обновить данные игрока из игры перед расчётом"),
        гильдия: bool = commands.Param(default=False, description="Проверить всю гильдию вместо одного игрока — только для офицеров"),
        сценарий: str = commands.Param(default=SCENARIO_RAW, description="Как сравнивать билд/моды с нормой плейта по релику", choices=SCENARIO_CHOICES),
        схема: str = commands.Param(default=None, description="Форсировать схему мод-билда вместо авто-детекта (не действует на персонажей без схем)", choices=FORCE_SCHEME_CHOICES),
    ):
        await inter.response.defer()

        guild_id = await guild_resolver.require_feature(inter, "stat_requirements")
        if guild_id is None:
            return

        if not self.bot.stat_calc:
            await inter.edit_original_response("⏳ Калькулятор статов ещё загружается, попробуйте через минуту.")
            return

        forced_scheme = _parse_scheme_param(схема)

        char_keys = [_parse_bracket_id(персонаж)] if персонаж is not None else database.get_stat_requirement_characters(плейт, guild_id=guild_id)
        if not char_keys:
            await inter.edit_original_response("❌ Нет сохранённых требований для этого плейта.")
            return

        if гильдия:
            if not guild_resolver.is_officer_for_resolved_guild(inter.author):
                await inter.edit_original_response("❌ Проверка по всей гильдии доступна только офицерам.")
                return

            report = await _build_guild_report(self.bot, плейт, char_keys, guild_id=guild_id, scenario=сценарий, forced_scheme=forced_scheme)
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

        if аликод is not None:
            if игрок is not None:
                await inter.edit_original_response("❌ Укажите либо игрока из списка, либо код союзника — не оба сразу.")
                return
            ally_code = guild_resolver.normalize_ally_code(аликод)
            if ally_code is None:
                await inter.edit_original_response("❌ Код союзника должен состоять из 9 цифр.")
                return
            cache = self.bot.guild_roster_caches.get(guild_id, {})
            игрок = cache.get(ally_code, ally_code)
        elif игрок is None:
            registration = database.get_user_registration(str(inter.author.id), guild_id=guild_id)
            if not registration:
                await inter.edit_original_response(
                    "❌ Игрок не указан, а вы не зарегистрированы — используйте `/регистрация`, укажите игрока явно "
                    "или код союзника."
                )
                return
            ally_code, игрок = registration
        else:
            cache = self.bot.guild_roster_caches.get(guild_id, {})
            ally_code = cache.get(игрок)
            if not ally_code:
                await inter.edit_original_response("❌ Игрок не найден в составе гильдии.")
                return

        lines = []
        matched_total = 0
        rows_total = 0
        updated_ats = []
        any_char_shown = False
        failed_required_by_char = []
        for base_id in char_keys:
            result = await _evaluate_character_player(self.bot, плейт, base_id, ally_code, обновить, игрок, guild_id=guild_id, scenario=сценарий, forced_scheme=forced_scheme)
            if result is None:
                continue
            char_name, block, matched, total, updated_at, failed_required, _required_total, _active_scheme, _scheme_label = result
            any_char_shown = True
            lines.append(f"## {char_name}")
            lines.append(block)
            matched_total += matched
            rows_total += total
            if updated_at:
                updated_ats.append(updated_at)
            if failed_required:
                failed_required_by_char.append((char_name, failed_required))

        if not any_char_shown:
            await inter.edit_original_response("❌ Нет сохранённых требований для этого плейта.")
            return

        # Итоговый блок внизу отчёта: только ОБЯЗАТЕЛЬНЫЕ статы, не прошедшие норму по выбранному
        # сценарию (см. failed_required в _evaluate_character_player) — раньше этот блок ВСЕГДА
        # считал по полному подгону релика независимо от параметра, теперь честно следует за ним.
        _scenario_titles = {
            SCENARIO_RAW: "как есть, без проекции — как в ХБ",
            SCENARIO_UP: "релик поднят только тем, кто ниже цели",
            SCENARIO_FULL: "с полным подгоном релика на цель плейта",
        }
        lines.append(f"## ⚠️ Итог: обязательные статы ({_scenario_titles[сценарий]})")
        if failed_required_by_char:
            for char_name, failed_items in failed_required_by_char:
                for item in failed_items:
                    lines.append(f"❌ **{char_name}** — {item['stat']}: сейчас {item['current']}, нужно {item['requirement']}")
        else:
            lines.append("✅ Все обязательные статы в норме.")

        # Цвет по тому же признаку, что и текст "Итог" выше — обязательным статам
        # (failed_required_by_char), а не matched_total == rows_total: иначе цвет мог
        # уйти в "частично" из-за невыполненного стата с приоритетом "по желанию"/
        # "полезно", хотя текст тут же говорит "все обязательные статы в норме".
        if not failed_required_by_char:
            color = DATACRON_CHECK_COLOR_FULL
        elif matched_total == 0:
            color = DATACRON_CHECK_COLOR_NONE
        else:
            color = DATACRON_CHECK_COLOR_PARTIAL

        title = f"📋 {плейт} (игрок: {игрок})"
        embeds = _lines_to_embeds(title, color, lines)
        if not embeds:
            await inter.edit_original_response("❌ Нечего показать.")
            return

        footer = f"Данные игрока обновлены: {updated_ats[0]}" if updated_ats else "⚠️ Нет кэшированных данных ни по одному из этих юнитов у игрока"
        embeds[-1].set_footer(text=footer)

        await inter.edit_original_response(embed=embeds[0])
        for e in embeds[1:]:
            await inter.followup.send(embed=e)

    # ------------------ /статы_релик (пересчёт нормы плейта на другой релик) ------------------
    @commands.slash_command(name="статы_релик", description="Пересчитать уже заданную в плейте норму персонажа на другой релик")
    async def stats_relic_check(
        self,
        inter: disnake.ApplicationCommandInteraction,
        плейт: str = commands.Param(description="Плейт (набор требований)", autocomplete=autocomplete_stat_plate),
        персонаж: str = commands.Param(description="Персонаж из плейта", autocomplete=autocomplete_stat_character),
        релик: int = commands.Param(description="Целевой уровень реликвии", ge=0, le=10),
        схема: str = commands.Param(default=None, description="Какую схему показывать, если у персонажа их две (нет живого игрока — по умолчанию схема 1)", choices=SCHEME_CHOICES),
    ):
        await inter.response.defer()

        guild_id = await guild_resolver.require_feature(inter, "stat_requirements")
        if guild_id is None:
            return

        if not self.bot.stat_calc:
            await inter.edit_original_response("⏳ Калькулятор статов ещё загружается, попробуйте через минуту.")
            return

        base_id = _parse_bracket_id(персонаж)
        result = await _project_character_relic(self.bot, плейт, base_id, релик, guild_id=guild_id, forced_scheme=_parse_scheme_param(схема))
        if result is None:
            await inter.edit_original_response("❌ Нет сохранённых требований для этого персонажа в плейте.")
            return

        char_name, block = result
        title = f"📋 {плейт}: {char_name} → релик {релик}"
        embeds = _lines_to_embeds(title, DATACRON_LIST_COLOR, [block])
        if not embeds:
            await inter.edit_original_response("❌ Нечего показать.")
            return

        embeds[-1].set_footer(text="Модель: плоская часть порога (роллы модов) не растёт, остальное масштабируется вместе с базой персонажа")

        await inter.edit_original_response(embed=embeds[0])
        for e in embeds[1:]:
            await inter.followup.send(embed=e)

    # ------------------ /ресурсы (потрачено деталей снаряжения + сигналов реликвии) ------------------
    @commands.slash_command(name="ресурсы", description="Сколько деталей снаряжения и сигналов реликвии потрачено на прокачку за период")
    async def resources_spent(
        self,
        inter: disnake.ApplicationCommandInteraction,
        игрок: str = commands.Param(default=None, description="Игрок гильдии — если не указан, берётся ваша регистрация (/регистрация)", autocomplete=autocomplete_players),
        аликод: str = commands.Param(default=None, description="Код союзника — вместо параметра «игрок»"),
        период: str = commands.Param(default="month", description="За какой период считать", choices=RESOURCE_PERIOD_CHOICES),
    ):
        await inter.response.defer()

        guild_id = await guild_resolver.require_feature(inter, "stat_requirements")
        if guild_id is None:
            return

        if аликод is not None:
            if игрок is not None:
                await inter.edit_original_response("❌ Укажите либо игрока из списка, либо код союзника — не оба сразу.")
                return
            ally_code = guild_resolver.normalize_ally_code(аликод)
            if ally_code is None:
                await inter.edit_original_response("❌ Код союзника должен состоять из 9 цифр.")
                return
            cache = self.bot.guild_roster_caches.get(guild_id, {})
            игрок = cache.get(ally_code, ally_code)
        elif игрок is None:
            registration = database.get_user_registration(str(inter.author.id), guild_id=guild_id)
            if not registration:
                await inter.edit_original_response(
                    "❌ Игрок не указан, а вы не зарегистрированы — используйте `/регистрация`, укажите игрока явно "
                    "или код союзника."
                )
                return
            ally_code, игрок = registration
        else:
            cache = self.bot.guild_roster_caches.get(guild_id, {})
            ally_code = cache.get(игрок)
            if not ally_code:
                await inter.edit_original_response("❌ Игрок не найден в составе гильдии.")
                return

        report = resource_spend.build_report(guild_id, ally_code, период)
        gear, relic = report["gear"], report["relic"]
        signals, craft = relic["signals"], relic["craft"]

        # Полный список без усечения топ-N — по запросу пользователя 2026-09-21
        # ("...и ещё 56 видов деталей тоже надо убрать и писать полный список, даже
        # если он большой"): _lines_to_embeds сама режет на несколько embed'ов/
        # followup-сообщений под лимит Discord, так что длина тут не проблема.
        lines = [f"За {report['period_label']}, с {report['date_from']}:", ""]

        if gear["upgrades"] == 0:
            lines.append("🔧 Снаряжение: повышений тира не было")
        else:
            lines.append(f"🔧 Снаряжение: **{gear['total']}** деталей ({gear['upgrades']} повыш. тира)")
            if gear["by_equipment"]:
                names = database.get_game_equipment_names(list(gear["by_equipment"].keys()))
                for equipment_id, qty in sorted(gear["by_equipment"].items(), key=lambda kv: kv[1], reverse=True):
                    lines.append(f"  • {names.get(equipment_id, equipment_id)} × {qty}")

        lines.append("")
        if relic["upgrades"] == 0:
            lines.append("📡 Реликвия: повышений тира не было")
        else:
            lines.append(f"📡 Реликвия: {relic['upgrades']} повыш. тира")
            lines.append(f"  Сигналы: **{signals['total']}**")
            if signals["by_material"]:
                names = database.get_game_equipment_names(list(signals["by_material"].keys()))
                for material_id, qty in sorted(signals["by_material"].items()):
                    lines.append(f"  • {names.get(material_id, material_id)} × {qty}")
            # Материалы "Мусорщика" — по запросу пользователя 2026-09-21: апгрейд
            # реликвии тратит не только сигналы, но и крафтовые детали (SCV_xxx).
            if craft["by_material"]:
                lines.append(f"  Детали для крафта: **{craft['total']}**")
                names = database.get_game_equipment_names(list(craft["by_material"].keys()))
                for material_id, qty in sorted(craft["by_material"].items()):
                    lines.append(f"  • {names.get(material_id, material_id)} × {qty}")

        title = f"📦 Ресурсы — {игрок}"
        embeds = _lines_to_embeds(title, DATACRON_LIST_COLOR, lines)
        if not embeds:
            await inter.edit_original_response("❌ Нечего показать.")
            return
        embeds[-1].set_footer(text="Точный расчёт по официальным рецептам тиров — не оценка")

        await inter.edit_original_response(embed=embeds[0])
        for e in embeds[1:]:
            await inter.followup.send(embed=e)


def setup(bot):
    bot.add_cog(StatRequirementsCog(bot))
