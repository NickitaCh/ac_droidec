"""Веб-версия поиска модов гильдии (/mod-search) — по запросу NicolozZ, тред "Гайд по АС
Боту" #ас-задачи, 2026-09-10. Полностью динамический конструктор условий (сет/слот/primary +
произвольное число условий по вторичкам, повторяемые строки — как на /mod-builder), в
отличие от Discord-версии (cogs/mod_search.py), у которой фиксированные 3 слота под
вторички — веб не ограничен количеством слэш-команд-параметров. Сет/слот/primary — чекбокс-
группы (множественный выбор внутри каждой категории, ИЛИ; между категориями — И), по
фидбеку пользователя 2026-09-11 — раньше были одиночные <select>.

Движок поиска — services/mod_search.py (общий с Discord-командой `/моды_поиск`), данные —
уже закэшированный player_unit_cache, без обращений к Comlink.

Режим вывода определяется числом выбранных игроков (по решению пользователя 2026-09-10):
ровно один игрок (чекбокс "вся гильдия" выключен, выбран один в таблице) -> список его
модов, как в Discord-версии без гильдия=True; несколько игроков или "вся гильдия" -> таблица
счётчиков по игрокам (по фидбеку 2026-09-11 — ВКЛЮЧАЯ игроков с 0 совпадений, раньше молча
пропускались) + раскрывающиеся детали по тем, у кого находки есть.

Пресеты (переиспользуемый критерий фильтра, без привязки к тому, по кому его запустят) и
история (полный контекст запуска — фильтр + кого проверяли) — по аналогии с /mod-builder
(database.py: mod_search_presets/mod_search_history, тот же паттерн, что
stat_mod_presets/stat_hypothetical_history)."""

from pathlib import Path
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

import database
import stat_engine
from services import mod_search
from web.deps import require_officer_access

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent.parent / "templates"))

SET_CHOICES = sorted(((int(sid), name) for sid, name in stat_engine.MOD_SET_IDS.items()), key=lambda t: t[1])
SLOT_CHOICES = mod_search.SLOT_CHOICES
STAT_CHOICES = mod_search.STAT_CHOICES  # [(stat_id, name, is_percent), ...]
OPERATOR_CHOICES = [">=", "<=", "="]


def _parse_conditions(mapping) -> list:
    """cond_stat/cond_op/cond_value — повторяющиеся параллельные поля (см. data-row-group
    в шаблоне, тот же приём, что на /mod-builder), .getlist() зипует их по порядку."""
    conditions = []
    for stat_raw, op, value_raw in zip(mapping.getlist("cond_stat"), mapping.getlist("cond_op"), mapping.getlist("cond_value")):
        if not stat_raw or not op or not value_raw:
            continue
        try:
            stat_id = int(stat_raw)
            value = float(value_raw)
        except ValueError:
            continue
        if op not in mod_search.OPERATORS:
            continue
        conditions.append((stat_id, op, value))
    return conditions


def _condition_rows(mapping) -> list:
    rows = list(zip(mapping.getlist("cond_stat"), mapping.getlist("cond_op"), mapping.getlist("cond_value")))
    return rows if rows else [("", "", "")]


def _parse_int_list(mapping, key: str) -> list:
    values = []
    for raw in mapping.getlist(key):
        try:
            values.append(int(raw))
        except ValueError:
            continue
    return values


def _build_filter(set_ids, slot_keys, primary_stat_ids, conditions) -> dict:
    return {
        "set_ids": set_ids or [],
        "slot_keys": slot_keys or [],
        "primary_stat_ids": primary_stat_ids or [],
        "conditions": [list(c) for c in (conditions or [])],
    }


def _search_kwargs(filt: dict) -> dict:
    """filt (наша JSON-хранимая форма, всегда списки) -> kwargs services.mod_search.mod_matches
    ждёт (пустой список = "любой", None тоже сработал бы, но пустой список честнее отражает
    "явно ничего не выбрано")."""
    return {
        "set_ids": filt.get("set_ids") or None,
        "slot_keys": filt.get("slot_keys") or None,
        "primary_stat_ids": filt.get("primary_stat_ids") or None,
        "conditions": [tuple(c) for c in (filt.get("conditions") or [])],
    }


def _filter_summary(filt: dict) -> str:
    parts = []
    set_ids = filt.get("set_ids") or []
    if set_ids:
        parts.append("Сеты: " + ", ".join(stat_engine.MOD_SET_IDS.get(str(sid), f"#{sid}") for sid in set_ids))
    slot_keys = filt.get("slot_keys") or []
    if slot_keys:
        parts.append("Слоты: " + ", ".join(mod_search.SLOT_KEY_TO_LABEL.get(k, k) for k in slot_keys))
    primary_ids = filt.get("primary_stat_ids") or []
    if primary_ids:
        parts.append("Primary: " + ", ".join(mod_search.STAT_ID_TO_INFO.get(sid, (f"#{sid}", False))[0] for sid in primary_ids))
    conditions = filt.get("conditions") or []
    if conditions:
        cond_txt = []
        for stat_id, op, value in conditions:
            name = mod_search.STAT_ID_TO_INFO.get(stat_id, (f"#{stat_id}", False))[0]
            cond_txt.append(f"{name}{op}{mod_search.fmt_value(value)}")
        parts.append("Вторички: " + ", ".join(cond_txt))
    return "; ".join(parts) if parts else "— без условий —"


def _target_summary(filt: dict, name_by_code: dict) -> str:
    if filt.get("guild_wide"):
        return "Вся гильдия"
    codes = filt.get("ally_codes") or []
    names = [name_by_code.get(c, c) for c in codes]
    if len(names) == 1:
        return names[0]
    return f"{len(names)} игроков"


def _mod_row(m: dict, char_names: dict) -> dict:
    decoded = m["mod"]
    primary = decoded["primary"]
    primary_txt = (
        f"{primary['name']} {mod_search.fmt_value(primary['value'])}{'%' if primary['is_percent'] else ''}"
        if primary else "?"
    )
    secondaries_txt = ", ".join(
        f"{s['name']} {mod_search.fmt_value(s['value'])}{'%' if s['is_percent'] else ''}" for s in decoded["secondaries"]
    ) or "—"
    set_name = stat_engine.MOD_SET_IDS.get(str(decoded["set_id"]), f"#{decoded['set_id']}")
    return {
        "char_name": char_names.get(m["base_id"], m["base_id"]),
        "slot_label": decoded["slot_label"],
        "set_name": set_name,
        "primary": primary_txt,
        "secondaries": secondaries_txt,
    }


def _preset_rows(guild_id: int):
    return [
        {"id": pid, "name": name, "summary": _filter_summary(filt)}
        for pid, name, filt, _created_by, _created_at in database.get_all_mod_search_presets(guild_id=guild_id)
    ]


def _history_rows(guild_id: int, name_by_code: dict):
    return [
        {"id": hid, "summary": _filter_summary(filt), "target": _target_summary(filt, name_by_code), "created_at": created_at}
        for hid, filt, _created_by, created_at in database.get_mod_search_history(guild_id=guild_id)
    ]


def _redirect_qs(mapping) -> str:
    """Пересобирает querystring текущего фильтра+цели — чтобы после сохранения пресета
    редирект вернул пользователя на тот же расчёт (тот же приём, что /mod-builder)."""
    pairs = []
    for key in ("set_id", "slot_key", "primary_stat_id"):
        for val in mapping.getlist(key):
            pairs.append((key, val))
    for name, op, value in zip(mapping.getlist("cond_stat"), mapping.getlist("cond_op"), mapping.getlist("cond_value")):
        if name and op and value:
            pairs.append(("cond_stat", name))
            pairs.append(("cond_op", op))
            pairs.append(("cond_value", value))
    if mapping.get("guild_wide") == "on":
        pairs.append(("guild_wide", "on"))
    for code in mapping.getlist("ally_codes"):
        pairs.append(("ally_codes", code))
    return urlencode(pairs)


@router.get("", response_class=HTMLResponse)
async def mod_search_page(request: Request, user: dict = Depends(require_officer_access)):
    guild_id = user["guild_id"]
    qp = request.query_params

    set_ids = _parse_int_list(qp, "set_id")
    slot_keys = qp.getlist("slot_key")
    primary_stat_ids = _parse_int_list(qp, "primary_stat_id")
    conditions = _parse_conditions(qp)
    condition_rows = _condition_rows(qp)
    guild_wide = qp.get("guild_wide") == "on"
    selected_codes = set(qp.getlist("ally_codes"))

    roster = database.get_all_user_mappings(guild_id)  # [(discord_id, ally_code, name), ...]
    roster_options = sorted(((ally_code, name) for _discord_id, ally_code, name in roster), key=lambda t: t[1])
    name_by_code = dict(roster_options)

    preset_id = qp.get("preset_id")
    history_id = qp.get("history_id")
    reopened_from_history = False
    if preset_id:
        preset = database.get_mod_search_preset(int(preset_id), guild_id=guild_id)
        if preset:
            _, _, filt, _, _ = preset
            set_ids = filt.get("set_ids") or []
            slot_keys = filt.get("slot_keys") or []
            primary_stat_ids = filt.get("primary_stat_ids") or []
            conditions = [tuple(c) for c in (filt.get("conditions") or [])]
            condition_rows = [(str(sid), op, mod_search.fmt_value(value)) for sid, op, value in conditions] or [("", "", "")]
    elif history_id:
        entry = database.get_mod_search_history_entry(int(history_id), guild_id=guild_id)
        if entry:
            _, filt, _, _ = entry
            set_ids = filt.get("set_ids") or []
            slot_keys = filt.get("slot_keys") or []
            primary_stat_ids = filt.get("primary_stat_ids") or []
            conditions = [tuple(c) for c in (filt.get("conditions") or [])]
            condition_rows = [(str(sid), op, mod_search.fmt_value(value)) for sid, op, value in conditions] or [("", "", "")]
            guild_wide = bool(filt.get("guild_wide"))
            selected_codes = set(filt.get("ally_codes") or [])
            reopened_from_history = True

    context = {
        "user": user,
        "set_choices": SET_CHOICES,
        "slot_choices": SLOT_CHOICES,
        "stat_choices": STAT_CHOICES,
        "operator_choices": OPERATOR_CHOICES,
        "selected_set_ids": set(set_ids),
        "selected_slot_keys": set(slot_keys),
        "selected_primary_ids": set(primary_stat_ids),
        "condition_rows": condition_rows,
        "guild_wide": guild_wide,
        "selected_codes": selected_codes,
        "roster": roster_options,
        "presets": _preset_rows(guild_id),
        "history": _history_rows(guild_id, name_by_code),
        "result_mode": None,
        "list_result": None,
        "table_result": None,
        "current_filter_qs": "",
        "error": qp.get("error"),
    }

    has_filter = bool(set_ids or slot_keys or primary_stat_ids or conditions)
    target_codes = [ac for ac, _name in roster_options] if guild_wide else [ac for ac, _name in roster_options if ac in selected_codes]
    if not has_filter or not target_codes:
        return templates.TemplateResponse(request, "mod_search.html", context)

    filt = _build_filter(set_ids, slot_keys, primary_stat_ids, conditions)
    search_kwargs = _search_kwargs(filt)

    units_by_ally = database.get_player_units_bulk(target_codes)

    # По фидбеку 2026-09-11: держим запись на КАЖДОГО из target_codes, даже с 0 находок —
    # раньше молча выкидывали игроков без совпадений, было непонятно, кого вообще проверили.
    player_matches = []
    for ally_code in target_codes:
        matches = mod_search.search_units(units_by_ally.get(ally_code, {}), **search_kwargs)
        player_matches.append((name_by_code.get(ally_code, ally_code), matches))
    player_matches.sort(key=lambda pm: len(pm[1]), reverse=True)

    all_base_ids = {m["base_id"] for _name, matches in player_matches for m in matches}
    char_names = database.get_game_unit_names(list(all_base_ids))

    if len(target_codes) == 1:
        name, matches = player_matches[0]
        context["result_mode"] = "list"
        context["list_result"] = {"name": name, "rows": [_mod_row(m, char_names) for m in matches]}
    else:
        matched_count = sum(1 for _name, matches in player_matches if matches)
        context["result_mode"] = "table"
        context["table_result"] = {
            "total_players": len(target_codes),
            "matched_players": matched_count,
            "total_mods": sum(len(matches) for _name, matches in player_matches),
            "players": [
                {"name": name, "count": len(matches), "rows": [_mod_row(m, char_names) for m in matches]}
                for name, matches in player_matches
            ],
        }

    context["current_filter_qs"] = _redirect_qs(qp)

    if not reopened_from_history:
        history_filt = dict(filt, guild_wide=guild_wide, ally_codes=[] if guild_wide else target_codes)
        database.add_mod_search_history(history_filt, user["discord_id"], guild_id=guild_id)
        context["history"] = _history_rows(guild_id, name_by_code)

    return templates.TemplateResponse(request, "mod_search.html", context)


@router.post("/presets/save", response_class=HTMLResponse)
async def preset_save(request: Request, user: dict = Depends(require_officer_access)):
    form = await request.form()
    name = (form.get("name") or "").strip()
    if not name:
        return RedirectResponse(f"/mod-search?{urlencode({'error': 'Укажите имя пресета.'})}", status_code=303)

    filt = _build_filter(
        _parse_int_list(form, "set_id"),
        form.getlist("slot_key"),
        _parse_int_list(form, "primary_stat_id"),
        _parse_conditions(form),
    )
    ok = database.create_mod_search_preset(name, filt, user["discord_id"], guild_id=user["guild_id"])
    qs = _redirect_qs(form)
    if not ok:
        error_qs = urlencode({"error": f"Пресет «{name}» уже существует."})
        return RedirectResponse(f"/mod-search?{qs}&{error_qs}" if qs else f"/mod-search?{error_qs}", status_code=303)
    return RedirectResponse(f"/mod-search?{qs}" if qs else "/mod-search", status_code=303)


@router.post("/presets/{preset_id}/delete", response_class=HTMLResponse)
async def preset_delete(preset_id: int, user: dict = Depends(require_officer_access)):
    database.delete_mod_search_preset(preset_id, guild_id=user["guild_id"])
    return RedirectResponse("/mod-search", status_code=303)
