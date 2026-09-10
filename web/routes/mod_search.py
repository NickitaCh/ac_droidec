"""Веб-версия поиска модов гильдии (/mod-search) — по запросу NicolozZ, тред "Гайд по АС
Боту" #ас-задачи, 2026-09-10. Полностью динамический конструктор условий (сет/слот/primary +
произвольное число условий по вторичкам, повторяемые строки — как на /mod-builder), в
отличие от Discord-версии (cogs/mod_search.py), у которой фиксированные 3 слота под
вторички — веб не ограничен количеством слэш-команд-параметров.

Движок поиска — services/mod_search.py (общий с Discord-командой `/моды_поиск`), данные —
уже закэшированный player_unit_cache, без обращений к Comlink.

Режим вывода определяется числом выбранных игроков (по решению пользователя 2026-09-10):
ровно один игрок (чекбокс "вся гильдия" выключен, выбран один в таблице) -> список его
модов, как в Discord-версии без гильдия=True; несколько игроков или "вся гильдия" -> таблица
счётчиков по игрокам + раскрывающиеся детали (те же самые строки, что и в списке)."""

from pathlib import Path

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
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


@router.get("", response_class=HTMLResponse)
async def mod_search_page(request: Request, user: dict = Depends(require_officer_access)):
    guild_id = user["guild_id"]
    qp = request.query_params

    set_id_raw = qp.get("set_id", "")
    slot_key = qp.get("slot_key", "")
    primary_raw = qp.get("primary_stat_id", "")
    conditions = _parse_conditions(qp)
    condition_rows = _condition_rows(qp)
    guild_wide = qp.get("guild_wide") == "on"
    selected_codes = set(qp.getlist("ally_codes"))

    roster = database.get_all_user_mappings(guild_id)  # [(discord_id, ally_code, name), ...]
    roster_options = sorted(((ally_code, name) for _discord_id, ally_code, name in roster), key=lambda t: t[1])

    context = {
        "user": user,
        "set_choices": SET_CHOICES,
        "slot_choices": SLOT_CHOICES,
        "stat_choices": STAT_CHOICES,
        "operator_choices": OPERATOR_CHOICES,
        "selected_set_id": set_id_raw,
        "selected_slot_key": slot_key,
        "selected_primary": primary_raw,
        "condition_rows": condition_rows,
        "guild_wide": guild_wide,
        "selected_codes": selected_codes,
        "roster": roster_options,
        "result_mode": None,
        "list_result": None,
        "table_result": None,
    }

    has_filter = bool(set_id_raw or slot_key or primary_raw or conditions)
    target_codes = [ac for ac, _name in roster_options] if guild_wide else [ac for ac, _name in roster_options if ac in selected_codes]
    if not has_filter or not target_codes:
        return templates.TemplateResponse(request, "mod_search.html", context)

    filt = {
        "set_id": int(set_id_raw) if set_id_raw else None,
        "slot_key": slot_key or None,
        "primary_stat_id": int(primary_raw) if primary_raw else None,
        "conditions": conditions,
    }

    name_by_code = dict(roster_options)
    units_by_ally = database.get_player_units_bulk(target_codes)

    player_matches = []
    for ally_code in target_codes:
        matches = mod_search.search_units(units_by_ally.get(ally_code, {}), **filt)
        if matches:
            player_matches.append((name_by_code.get(ally_code, ally_code), matches))
    player_matches.sort(key=lambda pm: len(pm[1]), reverse=True)

    all_base_ids = {m["base_id"] for _name, matches in player_matches for m in matches}
    char_names = database.get_game_unit_names(list(all_base_ids))

    if len(target_codes) == 1:
        name = name_by_code.get(target_codes[0], target_codes[0])
        matches = player_matches[0][1] if player_matches else []
        context["result_mode"] = "list"
        context["list_result"] = {"name": name, "rows": [_mod_row(m, char_names) for m in matches]}
    else:
        context["result_mode"] = "table"
        context["table_result"] = {
            "total_players": len(target_codes),
            "matched_players": len(player_matches),
            "total_mods": sum(len(matches) for _name, matches in player_matches),
            "players": [
                {"name": name, "count": len(matches), "rows": [_mod_row(m, char_names) for m in matches]}
                for name, matches in player_matches
            ],
        }

    return templates.TemplateResponse(request, "mod_search.html", context)
