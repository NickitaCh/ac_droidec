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
from services import activity_diff
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
    "Physical Damage": 100,
    "Special Damage": 100,
    "Armor": 20,
}

OPERATOR_CHOICES = [
    disnake.OptionChoice(name=">=", value=">="),
    disnake.OptionChoice(name="<=", value="<="),
    disnake.OptionChoice(name="=", value="="),
]

# Короткие подписи для колонки "Стат" в таблице /статы — полные названия (особенно
# Physical/Special Damage, Critical Chance/Avoidance) не влезают в ширину на мобильных.
# Health/Speed/Armor/Potency и т.п. уже достаточно короткие — оставлены как есть (fallback).
STAT_LABEL_SHORT = {
    "Relic": "Реликвия",
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


async def _evaluate_character_player(bot, plate_name: str, base_id: str, ally_code, force_refresh: bool, player_label, guild_id: int = 1):
    """Возвращает (char_name, block, matched, total, updated_at, matched_relic_free, total_relic_free, failed_required)
    для одного персонажа плейта у конкретного игрока — статы берутся из его реальных модов/шмота,
    прогноз на релик плейта. matched/total — соответствие СЕЙЧАС (при текущем релике игрока, как
    видно в колонке "Сейчас"); matched_relic_free/total_relic_free — соответствие БЕЗ УЧЁТА нехватки
    реликвии (при показанном прогнозе — как в колонке "Релик N", т.е. прошёл бы билд/моды норму,
    если бы релик уже был нужного уровня; строка Relic из этого счёта исключена — сравнивать
    "реликвию с самой собой" бессмысленно). Используется гильдийским отчётом (_build_guild_report)
    для переключателя "учитывать реликвию" — сама детальная таблица блока не меняется, там и так
    видны обе колонки одновременно. failed_required — список обязательных (priority=="required")
    строк, не прошедших норму, ПЕРЕСЧИТАННУЮ на текущий релик игрока (то же "ok", что и в колонке
    "Нужно" — т.е. билд/моды сравниваются с нормой, скорректированной под фактический релик,
    а не в лоб с порогом плейта, который был задан для другого релика); используется для итогового
    блока в /статы, чтобы низкий релик сам по себе не превращал приемлемый билд в "не пройдено".
    Возвращает None, если для этого персонажа нет сохранённых требований (пропускается в отчёте)."""
    loaded = _load_char_rows(plate_name, base_id, guild_id)
    if loaded is None:
        return None
    rows, char_name, required_relic, comments, legend = loaded
    matched = 0
    total = 0
    matched_relic_free = 0
    total_relic_free = 0
    failed_required = []

    unit, updated_at = await _get_unit_for_player(bot, ally_code, base_id, force_refresh)
    if not unit:
        block = f"⚠️ нет юнита у игрока «{player_label}» (не открыт либо ещё не синхронизирован)"
        return char_name, block, 0, 0, None, 0, 0, []

    current_relic = stat_engine.get_current_relic_level(unit)
    current_values = dict(stat_engine.calc_final_stats(bot.stat_calc, unit))
    current_values["Relic"] = current_relic

    # Показываем прогноз не только вверх (у игрока релик ниже требуемого), но и вниз
    # (у игрока уже выше — интересно, каким был бы стат ровно на уровне плейта).
    # Формула "Нужно" (порог минус дельта) не зависит от направления: дельта от релика
    # к релику при тех же модах/шмоте фиксирована в обе стороны.
    show_projection = required_relic is not None and required_relic != current_relic
    target_relic = required_relic if show_projection else current_relic
    projected_values = None
    if show_projection:
        projected_unit = stat_engine.project_unit_relic(unit, target_relic)
        projected_values = dict(stat_engine.calc_final_stats(bot.stat_calc, projected_unit))
        projected_values["Relic"] = target_relic

    caption = f"Релик игрока: {current_relic}"
    if show_projection:
        if target_relic > current_relic:
            caption += f" → цель по плейту: {target_relic}"
        else:
            caption += f" → плейт требует {target_relic} (у игрока выше)"
    caption += legend
    if show_projection:
        headers = ["Стат", "Сейчас", "Нужно", f"Релик {target_relic}", "Норма"]
    else:
        headers = ["Стат", "Сейчас", "Норма"]

    table_rows = []
    for row in rows:
        _, _, _, stat_name, operator, threshold, priority, raw_text, comment, _, _ = row
        label = _stat_label(stat_name, priority)
        req_cell = f"{operator} {_fmt_compact(threshold)}"
        cur_val = current_values.get(stat_name)
        if cur_val is None:
            table_rows.append([label, "нет данных", "—", "—", req_cell] if show_projection else [label, "нет данных", req_cell])
            continue
        total += 1
        cur_ok = _compare(cur_val, operator, threshold)
        if cur_ok:
            matched += 1
        cur_cell = f"{_fmt_compact(cur_val)} {'✅' if cur_ok else '❌'}"

        # relic-free счёт: для Relic — пропускаем строку целиком (сравнивать реликвию саму
        # с собой на прогнозе бессмысленно); для остальных статов — берём прогнозное значение
        # на целевой релик, если есть прогноз, иначе (игрок уже на нужном релике) то же cur_val.
        if stat_name != "Relic":
            relic_free_val = (projected_values.get(stat_name) if projected_values else None) if show_projection else cur_val
            if relic_free_val is not None:
                total_relic_free += 1
                if _compare(relic_free_val, operator, threshold):
                    matched_relic_free += 1

        # adjusted_ok/adjusted_req_text — норма, пересчитанная под ФАКТИЧЕСКИЙ релик игрока
        # (та же величина, что показана в колонке "Нужно"), а не порог плейта в лоб. Для
        # строки Relic и для случаев без прогноза пересчитывать нечего — сравниваем как есть.
        adjusted_ok = cur_ok
        adjusted_req_text = req_cell

        if not show_projection:
            table_rows.append([label, cur_cell, req_cell])
        else:
            needed_cell = "—"
            proj_cell = "—"
            if stat_name != "Relic":
                proj_val = projected_values.get(stat_name) if projected_values else None
                if proj_val is not None:
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
                    adjusted_ok = needed_ok
                    adjusted_req_text = f"{operator} {_fmt_compact(needed_now)}"
            table_rows.append([label, cur_cell, needed_cell, proj_cell, req_cell])

        if priority == PRIORITY_REQUIRED and not adjusted_ok:
            failed_required.append({
                "stat": label,
                "current": _fmt_compact(cur_val),
                "requirement": adjusted_req_text,
            })

    block = caption + "\n" + _build_table(headers, table_rows)
    if comments:
        block += "\n" + "\n".join(f"💠 _{c}_" for c in comments)

    return char_name, block, matched, total, updated_at, matched_relic_free, total_relic_free, failed_required


async def _build_guild_report(bot, plate_name: str, char_keys: list, guild_id: int = 1, account_for_relic: bool = True) -> dict:
    """Гильдийский вариант _evaluate_character_player — прогоняет весь зарегистрированный
    ростер по каждому персонажу плейта (char_keys сужается снаружи, если проверяем один
    персонаж), используя уже закэшированные в player_unit_cache данные (player_units_sync_loop,
    без обращений к Comlink — то же самое, что видит /статы без "обновить"). Раскладывает
    игроков на три бакета для рендера и в Discord, и в вебе.

    account_for_relic (по умолчанию True — прежнее поведение без изменений): считать
    соответствие по РЕАЛЬНЫМ текущим статам игрока (низкий релик естественно валит многие
    строки). False — игнорировать нехватку реликвии: считать по прогнозу на релик плейта
    (билд/моды уже готовы, реликвию просто ещё не подняли) — сама реликвия как требование
    из подсчёта в этом режиме исключается (см. _evaluate_character_player)."""
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
        matched_total = 0
        rows_total = 0
        char_problems = []
        for base_id in char_keys:
            result = await _evaluate_character_player(bot, plate_name, base_id, ally_code, False, name, guild_id=guild_id)
            if result is None:
                continue
            char_name, _block, matched_cur, total_cur, _updated_at, matched_rf, total_rf, _failed_required = result
            matched, total = (matched_cur, total_cur) if account_for_relic else (matched_rf, total_rf)
            matched_total += matched
            rows_total += total
            if total > 0 and matched < total:
                char_problems.append({
                    "char_name": char_name, "base_id": base_id, "matched": matched, "total": total,
                    "required_relic": required_relic_by_char.get(base_id),
                })

        entry = {"name": name, "ally_code": ally_code, "matched": matched_total, "total": rows_total, "chars": char_problems}
        if rows_total == 0:
            no_data.append(entry)
        elif matched_total == rows_total:
            compliant.append(entry)
        else:
            problem.append(entry)

    problem.sort(key=lambda r: (r["matched"] - r["total"], r["name"].lower()))
    compliant.sort(key=lambda r: r["name"].lower())
    no_data.sort(key=lambda r: r["name"].lower())
    return {"error": None, "total_players": len(roster), "compliant": compliant, "problem": problem, "no_data": no_data}


async def _project_character_relic(bot, plate_name: str, base_id: str, target_relic: int, guild_id: int = 1):
    """Возвращает (char_name, block) — пересчёт уже заданных в плейте норм на другой релик.
    Модель — та же, что в гильдийской Google-таблице (BASESTAT*MODMULT+flat): порог на
    исходном релике раскладывается на плоскую часть (RELIC_PROJECTION_FLAT_OFFSET — роллы
    с модов, не растут с базой) и оставшуюся часть, которая масштабируется пропорционально
    голой безмодовой базе стата. Множитель находится из уже заданного порога:
        multiplier = (threshold − flat) / base(исходный релик)
        projected  = base(целевой релик) × multiplier + flat
    Для статов без записи в RELIC_PROJECTION_FLAT_OFFSET (Speed, Potency, крит-статы и т.п.)
    норма не пересчитывается — как и в самой таблице, она просто переносится как есть.
    Возвращает None, если для этого персонажа нет сохранённых требований в плейте."""
    loaded = _load_char_rows(plate_name, base_id, guild_id)
    if loaded is None:
        return None
    rows, char_name, required_relic, comments, legend = loaded

    if required_relic is None:
        block = "⚠️ В требованиях плейта нет строки Relic — не от чего считать прибавку."
        return char_name, block

    ref_unit = _build_synthetic_unit(base_id, required_relic)
    target_unit = _build_synthetic_unit(base_id, target_relic)
    ref_values = dict(stat_engine.calc_final_stats(bot.stat_calc, ref_unit))
    target_values = dict(stat_engine.calc_final_stats(bot.stat_calc, target_unit))

    headers = ["Стат", f"Норма Р{required_relic}", f"Норма Р{target_relic}"]
    table_rows = []
    for row in rows:
        _, _, _, stat_name, operator, threshold, priority, raw_text, comment, _, _ = row
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

    block = f"Норма пересчитана с релика {required_relic} на {target_relic} (плоская часть + пропорциональный рост базы)" + legend + "\n" + _build_table(headers, table_rows)
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
        req_id, plate_name, character_key, stat_name, operator, threshold, priority, raw_text, comment, _, _ = row
        char_name = _unit_display_name(character_key)
        label = f"#{req_id} [{PRIORITY_LABELS.get(priority, priority)}] {plate_name}: {char_name} {stat_name} {operator} {_fmt_value(threshold)}"
        if not search or search in label.lower():
            options.append(disnake.OptionChoice(name=label[:100], value=f"#{req_id}"))
    return options[:25]


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
        await inter.response.send_message(f"✅ Фраза для омикрона «{char_name}» сохранена: {текст}", ephemeral=True)

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
    ):
        guild_id = await guild_resolver.require_guild_id(inter)
        if guild_id is None:
            return

        if плейт not in database.get_all_stat_requirement_plates(guild_id=guild_id):
            await inter.response.send_message(
                f"❌ Плейт «{плейт}» не найден — выберите вариант из списка автодополнения либо создайте его сначала через /статы_требования создать.",
                ephemeral=True,
            )
            return

        base_id = _parse_bracket_id(персонаж)
        char_name = _unit_display_name(base_id)
        raw_text = f"{char_name} {стат} {оператор} {_fmt_value(значение)}"
        req_id = database.add_stat_requirement(
            плейт, base_id, стат, оператор, значение, приоритет, raw_text, комментарий, str(inter.author.id), guild_id=guild_id
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
        удалить: bool = commands.Param(default=False, description="Удалить это требование вместо редактирования"),
    ):
        guild_id = await guild_resolver.require_guild_id(inter)
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

        _, plate_name, character_key, stat_name, cur_operator, cur_threshold, cur_priority, _raw_text, cur_comment, _, _ = row
        new_operator = оператор if оператор is not None else cur_operator
        new_threshold = значение if значение is not None else cur_threshold
        new_priority = приоритет if приоритет is not None else cur_priority
        new_comment = комментарий if комментарий is not None else cur_comment
        char_name = _unit_display_name(character_key)
        new_raw_text = f"{char_name} {stat_name} {new_operator} {_fmt_value(new_threshold)}"
        database.update_stat_requirement(req_id, plate_name, character_key, stat_name, new_operator, new_threshold, new_priority, new_comment, guild_id=guild_id)
        await inter.response.send_message(f"✅ Требование #{req_id} обновлено: {new_raw_text}", ephemeral=True)

    @stat_req.sub_command(name="список", description="Показать сохранённые требования по плейту (и опционально персонажу)")
    async def stat_req_list(
        self,
        inter: disnake.ApplicationCommandInteraction,
        плейт: str = commands.Param(description="Плейт", autocomplete=autocomplete_stat_plate),
        персонаж: str = commands.Param(default=None, description="Персонаж (если не указан — весь плейт)", autocomplete=autocomplete_stat_character),
    ):
        await inter.response.defer(ephemeral=True)
        guild_id = await guild_resolver.require_guild_id(inter)
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
            lines.append(f"## {_unit_display_name(base_id)}")
            for row in rows:
                req_id, _, _, _, _, _, priority, raw_text, comment, _, _ = row
                comment_part = f" · _{comment}_" if comment else ""
                lines.append(f"`#{req_id}` {PRIORITY_EMOJI.get(priority, '')} {raw_text}{comment_part}")

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
    ):
        guild_id = await guild_resolver.require_guild_id(inter)
        if guild_id is None:
            return

        created = database.create_stat_plate(плейт, описание, str(inter.author.id), guild_id=guild_id)
        if not created:
            await inter.response.send_message(f"❌ Плейт «{плейт}» уже существует.", ephemeral=True)
            return
        suffix = f" · _{описание}_" if описание else ""
        await inter.response.send_message(f"✅ Плейт «{плейт}» создан.{suffix}", ephemeral=True)

    @stat_req.sub_command(name="плейты", description="Показать список всех плейтов")
    async def stat_req_list_plates(self, inter: disnake.ApplicationCommandInteraction):
        await inter.response.defer(ephemeral=True)
        guild_id = await guild_resolver.require_guild_id(inter)
        if guild_id is None:
            return

        rows = database.get_all_stat_plates_detailed(guild_id=guild_id)
        if not rows:
            await inter.edit_original_response("❌ Плейтов пока нет — создайте через /статы_требования создать.")
            return

        lines = []
        for name, description, char_count, req_count in rows:
            desc_part = f" — _{description}_" if description else ""
            lines.append(f"`{name}`{desc_part} · персонажей: {char_count}, требований: {req_count}")

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
        guild_id = await guild_resolver.require_guild_id(inter)
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

    @stat_req.sub_command(name="удалить", description="Удалить плейт целиком, либо одного персонажа из плейта (если указан)")
    async def stat_req_delete_plate(
        self,
        inter: disnake.ApplicationCommandInteraction,
        плейт: str = commands.Param(description="Плейт для удаления", autocomplete=autocomplete_stat_plate),
        персонаж: str = commands.Param(default=None, description="Персонаж из плейта (если не указан — удаляется весь плейт)", autocomplete=autocomplete_stat_character),
        подтвердить: bool = commands.Param(default=False, description="Установите true только после проверки количества требований для удаления"),
    ):
        guild_id = await guild_resolver.require_guild_id(inter)
        if guild_id is None:
            return

        if персонаж:
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

        count = database.count_stat_requirements_by_plate(плейт, guild_id=guild_id)
        if database.get_stat_plate(плейт, guild_id=guild_id) is None and count == 0:
            await inter.response.send_message(f"❌ Плейт «{плейт}» не найден.", ephemeral=True)
            return

        if not подтвердить:
            await inter.response.send_message(
                f"⚠️ Будет удалён плейт «{плейт}» и его требований: {count}. "
                f"Повторите команду с подтвердить=True, чтобы подтвердить удаление.",
                ephemeral=True,
            )
            return

        deleted = database.delete_stat_plate(плейт, guild_id=guild_id)
        await inter.response.send_message(f"🗑️ Плейт «{плейт}» удалён вместе с требованиями: {deleted}.", ephemeral=True)

    # ------------------ /статы (открытая команда) ------------------
    @commands.slash_command(name="статы", description="Прогноз статов персонажа(ей) игрока на релик плейта относительно требований")
    async def stats_check(
        self,
        inter: disnake.ApplicationCommandInteraction,
        плейт: str = commands.Param(description="Плейт (набор требований)", autocomplete=autocomplete_stat_plate),
        игрок: str = commands.Param(default=None, description="Игрок гильдии — если не указан, берётся ваша регистрация (/регистрация)", autocomplete=autocomplete_players),
        персонаж: str = commands.Param(default=None, description="Персонаж из плейта (если не указан — весь плейт)", autocomplete=autocomplete_stat_character),
        обновить: bool = commands.Param(default=False, description="Обновить данные игрока из игры перед расчётом"),
        гильдия: bool = commands.Param(default=False, description="Проверить всю гильдию вместо одного игрока — только для офицеров"),
        учитывать_релик: bool = commands.Param(default=True, description="Гильдия: True — как сейчас (низкий релик валит статы), False — прогноз на релик плейта"),
    ):
        await inter.response.defer()

        guild_id = await guild_resolver.require_guild_id(inter)
        if guild_id is None:
            return

        if not self.bot.stat_calc:
            await inter.edit_original_response("⏳ Калькулятор статов ещё загружается, попробуйте через минуту.")
            return

        char_keys = [_parse_bracket_id(персонаж)] if персонаж is not None else database.get_stat_requirement_characters(плейт, guild_id=guild_id)
        if not char_keys:
            await inter.edit_original_response("❌ Нет сохранённых требований для этого плейта.")
            return

        if гильдия:
            if not guild_resolver.is_officer_for_resolved_guild(inter.author):
                await inter.edit_original_response("❌ Проверка по всей гильдии доступна только офицерам.")
                return

            report = await _build_guild_report(self.bot, плейт, char_keys, guild_id=guild_id, account_for_relic=учитывать_релик)
            if report["error"]:
                await inter.edit_original_response(f"❌ {report['error']}")
                return

            lines = [f"✅ Полностью соответствуют: {len(report['compliant'])}/{report['total_players']}"]
            if not учитывать_релик:
                lines.append("_(без учёта нехватки реликвии — прогноз на релик плейта)_")
            if report["no_data"]:
                lines.append(f"⚠️ Нет данных: {len(report['no_data'])}")
            lines.append("")
            if report["problem"]:
                for r in report["problem"]:
                    lines.append(f"❌ {r['name']} — {r['matched']}/{r['total']} (не выполнено: {r['total'] - r['matched']})")
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

        if игрок is None:
            registration = database.get_user_registration(str(inter.author.id), guild_id=guild_id)
            if not registration:
                await inter.edit_original_response(
                    "❌ Игрок не указан, а вы не зарегистрированы — используйте `/регистрация` или укажите игрока явно."
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
            result = await _evaluate_character_player(self.bot, плейт, base_id, ally_code, обновить, игрок, guild_id=guild_id)
            if result is None:
                continue
            char_name, block, matched, total, updated_at, _matched_rf, _total_rf, failed_required = result
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

        # Итоговый блок внизу отчёта: только ОБЯЗАТЕЛЬНЫЕ статы, не прошедшие норму, где норма
        # пересчитана под фактический релик игрока (см. failed_required в
        # _evaluate_character_player) — низкий релик сам по себе не топит билд в этом счёте,
        # только реально плохие моды/шмот.
        lines.append("## ⚠️ Итог: обязательные статы (с пересчётом на текущий релик)")
        if failed_required_by_char:
            for char_name, failed_items in failed_required_by_char:
                for item in failed_items:
                    lines.append(f"❌ **{char_name}** — {item['stat']}: сейчас {item['current']}, нужно {item['requirement']}")
        else:
            lines.append("✅ Все обязательные статы в норме.")

        if rows_total and matched_total == rows_total:
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
    ):
        await inter.response.defer()

        guild_id = await guild_resolver.require_guild_id(inter)
        if guild_id is None:
            return

        if not self.bot.stat_calc:
            await inter.edit_original_response("⏳ Калькулятор статов ещё загружается, попробуйте через минуту.")
            return

        base_id = _parse_bracket_id(персонаж)
        result = await _project_character_relic(self.bot, плейт, base_id, релик, guild_id=guild_id)
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


def setup(bot):
    bot.add_cog(StatRequirementsCog(bot))
