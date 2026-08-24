"""Веб-only "Конструктор" (/mod-builder) — гипотетическая сборка модов для персонажа,
которого никто ещё не прокачал: 6 слотов (форма мода) с выбором ТОЧНОГО primary-стата
(реальные игровые константы на 6★/ур.15, добыты эмпирически из реальных модов гильдии —
см. stat_engine.MOD_PRIMARY_OPTIONS) + отдельно интерактивный выбор сетов (сколько модов
какого сета, с подписанным количеством для бонуса) + вручную оценённая сумма вклада
ВТОРИЧНЫХ статов (единственное, что действительно нельзя посчитать точно — вторички
рандомны). Расчёт — stat_engine.build_hypothetical_unit (собирает синтетические
equippedStatMod-объекты, тот же формат, что реальные моды игроков в /статы) +
apply_manual_stat_totals для вторичек. Отдельная фича, не порт Discord-команды — той не
существует, поэтому не в stat_forecast.py (тот держит только веб-версии /статы и
/статы_релик)."""

from pathlib import Path
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

import database
import stat_engine
import services.stat_forecast as stat_forecast
from cogs.stat_requirements import STAT_CHOICES
from web.deps import require_officer_access

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent.parent / "templates"))

MOD_SET_CHOICES = sorted(((int(set_id), name) for set_id, name in stat_engine.MOD_SET_IDS.items()))
STAT_NAME_CHOICES = [(c.name, c.value) for c in STAT_CHOICES if c.value != "Relic"]
# Единица измерения (%/число) у поля ввода вторички для этого инструмента — шире, чем
# родной stat_engine.PERCENT_STATS (тот только про итоговые %-статы StatCalc): Health/
# Protection/Physical Damage/Special Damage тоже вводятся в %, т.к. на модах реально
# бывают Health%/Protection%/Offense%-вторички (см. stat_engine.PERCENT_OF_BASE_STATS и
# apply_manual_stat_totals, где именно так и обрабатывается). Единственное исключение —
# Speed: вторичка Speed% в игре не встречается, только штуками.
_STAT_UNIT_IS_PERCENT = stat_engine.PERCENT_STATS | stat_engine.PERCENT_OF_BASE_STATS
# Armor/Resistance — единственные статы, где сам ввод не в единицах ИТОГОВОГО стата: на
# модах это "Defense %" (вторичка от базового Defense-рейтинга, см.
# stat_engine.NONLINEAR_DEFENSE_STATS), а не "Armor %"/"Resistance %" напрямую — подпись
# явно это называет, чтобы не завести пользователя суммировать неправильные числа.
_DEFENSE_ROUTED_LABELS = {"Armor": "Defense % (влияет на Armor)", "Resistance": "Defense % (влияет на Resistance)"}
# Подпись зашита прямо в текст опции (не только рядом с полем через JS) — так видно сразу
# при выборе стата в выпадашке, не полагаясь на то, что пользователь заметит мелкую
# подсказку у поля ввода после выбора.
STAT_NAME_CHOICES_WITH_UNIT = [
    (_DEFENSE_ROUTED_LABELS.get(value) or (f"{name} (%)" if value in _STAT_UNIT_IS_PERCENT else f"{name} (число)"), value, value in _STAT_UNIT_IS_PERCENT)
    for name, value in STAT_NAME_CHOICES
]

# Реальные 6 форм модов в игре — у персонажа всегда ровно по одному слоту каждой формы.
# Иконки — свои простые SVG (не растровые ассеты HotUtils).
MOD_SLOT_DEFS = [
    ("square", "Квадрат"),
    ("arrow", "Стрела"),
    ("diamond", "Ромб"),
    ("triangle", "Треугольник"),
    ("circle", "Круг"),
    ("cross", "Крест"),
]
MOD_SLOT_KEYS = [key for key, _ in MOD_SLOT_DEFS]


def _get_comlink():
    # Как в services/stat_forecast.py — веб-процесс не поднимает диска-клиента, строит свой.
    from swgoh_comlink import SwgohComlink
    return SwgohComlink(url="http://localhost:3000")


def _fmt_value(value: float) -> str:
    """Не %g — тот уходит в научную нотацию за 1e6 (Health/Protection на релике легко
    больше миллиона, в отличие от порогов в /статы_требования, откуда скопирован этот
    формат исторически)."""
    if value == int(value):
        return str(int(value))
    return f"{value:.2f}".rstrip("0").rstrip(".")


def _char_label(base_id: str) -> str:
    return database.get_game_unit_name(base_id) or base_id


def _primary_option_by_stat(slot_key: str, unit_stat_id):
    if not unit_stat_id:
        return None
    for opt in stat_engine.MOD_PRIMARY_OPTIONS.get(slot_key, []):
        if opt["unit_stat"] == int(unit_stat_id):
            return opt
    return None


def _primaries_to_list(primaries: dict) -> list:
    """{slot_key: unit_stat_id} -> [option|None, ...] в порядке MOD_SLOT_DEFS, для
    stat_engine.build_hypothetical_unit (позиционный список из 6 физических модов)."""
    return [_primary_option_by_stat(slot_key, primaries.get(slot_key)) for slot_key, _ in MOD_SLOT_DEFS]


def _sets_summary(set_counts: dict) -> str:
    set_names = dict(MOD_SET_CHOICES)
    parts = [f"{count}×{set_names.get(int(set_id), f'#{set_id}')}" for set_id, count in sorted(set_counts.items(), key=lambda kv: int(kv[0])) if count]
    return ", ".join(parts) if parts else "—"


def _primaries_summary(primaries: dict) -> str:
    parts = []
    for slot_key, slot_label in MOD_SLOT_DEFS:
        opt = _primary_option_by_stat(slot_key, primaries.get(slot_key))
        if opt:
            parts.append(f"{slot_label}: {opt['label']}")
    return ", ".join(parts) if parts else "—"


def _stats_summary(manual_stats: dict) -> str:
    if not manual_stats:
        return "—"
    return ", ".join(
        f"{name} +{_fmt_value(value)}{'%' if name in _STAT_UNIT_IS_PERCENT else ''}"
        for name, value in manual_stats.items()
    )


def _preset_rows(guild_id: int):
    return [
        {
            "id": pid, "name": name, "sets_summary": _sets_summary(sets),
            "primaries_summary": _primaries_summary(primaries), "stats_summary": _stats_summary(stats),
        }
        for pid, name, sets, primaries, stats, _created_by, _created_at in database.get_all_stat_mod_presets(guild_id=guild_id)
    ]


def _history_rows(guild_id: int):
    return [
        {
            "id": hid, "char_name": _char_label(char_key), "relic": relic, "rarity": rarity,
            "sets_summary": _sets_summary(sets), "primaries_summary": _primaries_summary(primaries),
            "stats_summary": _stats_summary(stats), "created_at": created_at,
        }
        for hid, char_key, relic, sets, primaries, stats, _created_by, created_at, rarity in database.get_stat_hypothetical_history(guild_id=guild_id)
    ]


def _parse_set_counts(mapping) -> dict:
    """mapping — Starlette QueryParams (GET) или FormData (POST), обе умеют .get()."""
    counts = {}
    for set_id, _ in MOD_SET_CHOICES:
        raw = mapping.get(f"set_{set_id}")
        if not raw:
            continue
        try:
            n = int(raw)
        except ValueError:
            continue
        if n > 0:
            counts[set_id] = n
    return counts


def _parse_primary_picks(mapping) -> dict:
    """Возвращает {slot_key: unit_stat_id} — только валидные пары (стат реально входит
    в список опций этого слота, см. stat_engine.MOD_PRIMARY_OPTIONS)."""
    picks = {}
    for slot_key in MOD_SLOT_KEYS:
        raw = mapping.get(f"slot_{slot_key}")
        if not raw:
            continue
        try:
            stat_id = int(raw)
        except ValueError:
            continue
        if _primary_option_by_stat(slot_key, stat_id):
            picks[slot_key] = stat_id
    return picks


def _parse_manual_stats(mapping) -> dict:
    """Строки stat_name/stat_value идут параллельными повторяющимися полями (см.
    data-row-group в шаблоне) — .getlist() зипует их по порядку."""
    names = mapping.getlist("stat_name")
    values = mapping.getlist("stat_value")
    totals = {}
    for name, raw_value in zip(names, values):
        if not name or not raw_value:
            continue
        try:
            value = float(raw_value)
        except ValueError:
            continue
        totals[name] = totals.get(name, 0) + value
    return totals


def _redirect_qs(mapping) -> str:
    """Пересобирает querystring из текущего запроса (character/relic/звёздность/сеты/
    primary-статы/вторички) — чтобы после сохранения пресета редирект вернул пользователя
    на тот же расчёт."""
    pairs = []
    character = mapping.get("character")
    if character:
        pairs.append(("character", character))
    relic = mapping.get("relic")
    if relic:
        pairs.append(("relic", relic))
    rarity = mapping.get("rarity")
    if rarity:
        pairs.append(("rarity", rarity))
    target_stat = mapping.get("target_stat")
    target_value = mapping.get("target_value")
    if target_stat and target_value:
        pairs.append(("target_stat", target_stat))
        pairs.append(("target_value", target_value))
    for set_id, _ in MOD_SET_CHOICES:
        val = mapping.get(f"set_{set_id}")
        if val:
            pairs.append((f"set_{set_id}", val))
    for slot_key in MOD_SLOT_KEYS:
        val = mapping.get(f"slot_{slot_key}")
        if val:
            pairs.append((f"slot_{slot_key}", val))
    for name, value in zip(mapping.getlist("stat_name"), mapping.getlist("stat_value")):
        if name and value:
            pairs.append(("stat_name", name))
            pairs.append(("stat_value", value))
    return urlencode(pairs)


@router.get("", response_class=HTMLResponse)
async def builder_form(request: Request, user: dict = Depends(require_officer_access)):
    guild_id = user["guild_id"]
    qp = request.query_params

    character = qp.get("character", "")
    relic_raw = qp.get("relic", "")
    rarity_raw = qp.get("rarity", "7")
    target_stat = qp.get("target_stat", "")
    target_value_raw = qp.get("target_value", "")
    set_counts = _parse_set_counts(qp)
    primaries = _parse_primary_picks(qp)
    manual_stats = _parse_manual_stats(qp)

    history_id = qp.get("history_id")
    preset_id = qp.get("preset_id")
    reopened_from_history = False
    if history_id:
        entry = database.get_stat_hypothetical_history_entry(int(history_id), guild_id=guild_id)
        if entry:
            _, character, relic_int, set_counts, primaries, manual_stats, _, _, rarity_int = entry
            relic_raw = str(relic_int)
            rarity_raw = str(rarity_int)
            reopened_from_history = True
    elif preset_id:
        preset = database.get_stat_mod_preset(int(preset_id), guild_id=guild_id)
        if preset:
            _, _, set_counts, primaries, manual_stats, _, _ = preset

    # JSON-хранение (пресеты/история) отдаёт ключи sets как строки — приводим к int, чтобы
    # совпадало с MOD_SET_CHOICES/шаблоном; для set_counts из _parse_set_counts ключи и так
    # int. primaries хранит {slot_key: unit_stat_id} — ключи уже строки, значения int.
    set_counts = {int(k): v for k, v in set_counts.items()}

    context = {
        "user": user,
        "mod_sets": MOD_SET_CHOICES,
        "mod_set_piece_count": stat_engine.MOD_SET_PIECE_COUNT,
        "mod_slots": MOD_SLOT_DEFS,
        "mod_primary_options": stat_engine.MOD_PRIMARY_OPTIONS,
        "stat_name_choices": STAT_NAME_CHOICES_WITH_UNIT,
        "selected_character": character,
        "selected_character_label": _char_label(character) if character else "",
        "selected_relic": relic_raw,
        "selected_rarity": rarity_raw,
        "selected_target_stat": target_stat,
        "selected_target_value": target_value_raw,
        "set_counts": set_counts,
        "primaries": primaries,
        "manual_stat_rows": [(name, _fmt_value(value)) for name, value in manual_stats.items()] if manual_stats else [("", "")],
        "presets": _preset_rows(guild_id),
        "history": _history_rows(guild_id),
        "result": None,
        "target": None,
        "loading": False,
        "error": qp.get("error"),
    }

    if not character or not relic_raw:
        return templates.TemplateResponse(request, "stat_builder.html", context)

    try:
        relic = int(relic_raw)
    except ValueError:
        context["error"] = "Некорректный уровень реликвии."
        return templates.TemplateResponse(request, "stat_builder.html", context)
    if not (0 <= relic <= 10):
        context["error"] = "Релик должен быть от 0 до 10."
        return templates.TemplateResponse(request, "stat_builder.html", context)

    try:
        rarity = int(rarity_raw)
    except ValueError:
        context["error"] = "Некорректная звёздность."
        return templates.TemplateResponse(request, "stat_builder.html", context)
    if not (1 <= rarity <= 7):
        context["error"] = "Звёздность должна быть от 1 до 7."
        return templates.TemplateResponse(request, "stat_builder.html", context)

    try:
        comlink = _get_comlink()
        stat_calc = await stat_forecast.get_stat_calc(comlink)
    except Exception:
        context["loading"] = True
        return templates.TemplateResponse(request, "stat_builder.html", context)

    primary_list = _primaries_to_list(primaries)
    unit = stat_engine.build_hypothetical_unit(character, relic, set_counts, primary_list, rarity=rarity)
    base_final_stats = stat_engine.calc_final_stats(stat_calc, unit)
    final_stats = stat_engine.apply_manual_stat_totals(base_final_stats, manual_stats)

    context["result"] = {
        "char_name": _char_label(character),
        "rows": [(label, _fmt_value(final_stats.get(value, 0))) for label, value in STAT_NAME_CHOICES],
    }

    if target_stat and target_value_raw:
        try:
            target_value = float(target_value_raw)
        except ValueError:
            target_value = None
        if target_value is not None:
            # Считаем ОТ base_final_stats (сеты+primary+relic+звёздность, без ручных
            # вторичек) — "сколько ещё нужно набрать в допах", а не "сколько ещё сверху
            # уже введённого" (см. запрос пользователя 2026-08-24).
            base_value = base_final_stats.get(target_stat, 0)
            needed = stat_engine.required_manual_contribution(base_value, target_value, target_stat)
            # ВАЖНО: две разные единицы, не путать. target_value/base_value — это САМ стат
            # в его родном виде (Health/Physical Damage и т.п. — всегда число, даже когда их
            # вторичка вводится в %; см. stat_engine.PERCENT_STATS, а не более широкий
            # _STAT_UNIT_IS_PERCENT). needed — это то, что реально набирается во вторичках,
            # там единица всегда % (кроме Speed). Armor/Resistance — особый случай: needed
            # там в % Defense (см. stat_engine.NONLINEAR_DEFENSE_STATS), не в % самой Брони/
            # Сопротивления, поэтому подпись результата называет это явно.
            value_unit = "%" if target_stat in stat_engine.PERCENT_STATS else ""
            needed_unit = "%" if target_stat in _STAT_UNIT_IS_PERCENT else ""
            needed_label = "Defense" if target_stat in stat_engine.NONLINEAR_DEFENSE_STATS else dict((v, l) for l, v in STAT_NAME_CHOICES).get(target_stat, target_stat)
            context["target"] = {
                "stat_label": dict((v, l) for l, v in STAT_NAME_CHOICES).get(target_stat, target_stat),
                "needed_label": needed_label,
                "base_value_fmt": _fmt_value(base_value),
                "target_value_fmt": _fmt_value(target_value),
                "needed_fmt": _fmt_value(needed),
                "unit": value_unit,
                "needed_unit": needed_unit,
                "already_reached": needed <= 0,
            }

    if not reopened_from_history:
        database.add_stat_hypothetical_history(character, relic, set_counts, primaries, manual_stats, user["discord_id"], guild_id=guild_id, rarity=rarity)
        context["history"] = _history_rows(guild_id)

    return templates.TemplateResponse(request, "stat_builder.html", context)


@router.post("/presets/save", response_class=HTMLResponse)
async def preset_save(request: Request, user: dict = Depends(require_officer_access)):
    form = await request.form()
    name = (form.get("name") or "").strip()
    if not name:
        return RedirectResponse(f"/mod-builder?{urlencode({'error': 'Укажите имя пресета.'})}", status_code=303)

    set_counts = _parse_set_counts(form)
    primaries = _parse_primary_picks(form)
    manual_stats = _parse_manual_stats(form)
    ok = database.create_stat_mod_preset(name, set_counts, primaries, manual_stats, user["discord_id"], guild_id=user["guild_id"])
    qs = _redirect_qs(form)
    if not ok:
        error_qs = urlencode({"error": f"Пресет «{name}» уже существует."})
        return RedirectResponse(f"/mod-builder?{qs}&{error_qs}" if qs else f"/mod-builder?{error_qs}", status_code=303)
    return RedirectResponse(f"/mod-builder?{qs}" if qs else "/mod-builder", status_code=303)


@router.post("/presets/{preset_id}/delete", response_class=HTMLResponse)
async def preset_delete(preset_id: int, user: dict = Depends(require_officer_access)):
    database.delete_stat_mod_preset(preset_id, guild_id=user["guild_id"])
    return RedirectResponse("/mod-builder", status_code=303)
