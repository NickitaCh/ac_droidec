"""Веб-only "Спиздить билд" (/steal-build) — форма+рендер отчёта, расчёт целиком в
services/steal_build.py. GET с query-параметрами (как /mod-builder, /mod-search) —
результат можно заблокмаркить/переоткрыть по ссылке.

Сохранённые гильдии + история (добавлено по запросу пользователя, чат 2026-09-14: "часто
обращаться к одной гильдии") — тот же паттерн presets/history, что /mod-search
(database.py: steal_build_presets/steal_build_history), см. комментарий там."""

from pathlib import Path
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

import database
import services.stat_forecast as stat_forecast
import services.steal_build as steal_build
from web.deps import require_officer_access

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent.parent / "templates"))

DEFAULT_RELIC = "9"


def _get_comlink():
    # Как в services/stat_forecast.py/stat_builder.py — веб-процесс не поднимает диска-клиента,
    # строит свой поверх того же comlink-сайдкара.
    from swgoh_comlink import SwgohComlink
    return SwgohComlink(url="http://localhost:3000")


def _char_label(base_id: str) -> str:
    return database.get_game_unit_name(base_id) or base_id


def _preset_rows(guild_id: int):
    return [
        {
            "id": pid, "name": name,
            "guild_name": filt.get("guild_name") or "?",
            "character_label": _char_label(filt.get("character", "")),
            "relic": filt.get("relic"),
            "ally_code": filt.get("ally_code", ""),
        }
        for pid, name, filt, _created_by, _created_at in database.get_all_steal_build_presets(guild_id=guild_id)
    ]


def _history_rows(guild_id: int):
    return [
        {
            "id": hid,
            "guild_name": filt.get("guild_name") or "?",
            "character_label": _char_label(filt.get("character", "")),
            "relic": filt.get("relic"),
            "ally_code": filt.get("ally_code", ""),
            "created_at": created_at,
        }
        for hid, filt, _created_by, created_at in database.get_steal_build_history(guild_id=guild_id)
    ]


@router.get("", response_class=HTMLResponse)
async def steal_build_form(request: Request, user: dict = Depends(require_officer_access)):
    qp = request.query_params
    guild_id = user["guild_id"]
    ally_code = qp.get("ally_code", "").strip()
    character = qp.get("character", "")
    relic_raw = qp.get("relic", DEFAULT_RELIC)

    preset_id = qp.get("preset_id")
    history_id = qp.get("history_id")
    if preset_id:
        preset = database.get_steal_build_preset(int(preset_id), guild_id=guild_id)
        if preset:
            _, _, filt, _, _ = preset
            ally_code = filt.get("ally_code", ally_code)
            character = filt.get("character", character)
            relic_raw = str(filt.get("relic", relic_raw))
    elif history_id:
        entry = database.get_steal_build_history_entry(int(history_id), guild_id=guild_id)
        if entry:
            _, filt, _, _ = entry
            ally_code = filt.get("ally_code", ally_code)
            character = filt.get("character", character)
            relic_raw = str(filt.get("relic", relic_raw))

    context = {
        "user": user,
        "ally_code": ally_code,
        "selected_character": character,
        "selected_character_label": _char_label(character) if character else "",
        "selected_relic": relic_raw,
        "result": None,
        "loading": False,
        "error": None,
        "lookup_guild_name": None,
        "lookup_swgoh_guild_id": None,
        "presets": _preset_rows(guild_id),
        "history": _history_rows(guild_id),
    }
    if qp.get("error"):
        context["error"] = qp.get("error")

    if not ally_code or not character:
        return templates.TemplateResponse(request, "steal_build.html", context)

    try:
        relic = int(relic_raw)
    except ValueError:
        context["error"] = "Некорректный уровень реликвии."
        return templates.TemplateResponse(request, "steal_build.html", context)
    if not (0 <= relic <= 10):
        context["error"] = "Релик должен быть от 0 до 10."
        return templates.TemplateResponse(request, "steal_build.html", context)

    try:
        comlink = _get_comlink()
        stat_calc = await stat_forecast.get_stat_calc(comlink)
    except Exception:
        context["loading"] = True
        return templates.TemplateResponse(request, "steal_build.html", context)

    lookup = await steal_build.resolve_guild(comlink, ally_code)
    if not lookup.ok:
        context["error"] = lookup.error
        return templates.TemplateResponse(request, "steal_build.html", context)

    context["lookup_guild_name"] = lookup.guild_name
    context["lookup_swgoh_guild_id"] = lookup.swgoh_guild_id

    report = await steal_build.build_report(comlink, stat_calc, character, lookup, relic)

    # Гильдия резолвилась успешно (lookup.ok) — это уже полноценный поиск для истории, даже
    # если по конкретному персонажу результат "не открыт" (report тогда содержит error).
    filt = {"ally_code": ally_code, "character": character, "relic": relic, "guild_name": lookup.guild_name}
    database.add_steal_build_history(filt, user["discord_id"], guild_id=guild_id)
    context["history"] = _history_rows(guild_id)

    if report["error"]:
        context["error"] = report["error"]
        return templates.TemplateResponse(request, "steal_build.html", context)

    context["result"] = report
    return templates.TemplateResponse(request, "steal_build.html", context)


@router.post("/presets/save", response_class=HTMLResponse)
async def preset_save(request: Request, user: dict = Depends(require_officer_access)):
    form = await request.form()
    name = (form.get("name") or "").strip()
    ally_code = (form.get("ally_code") or "").strip()
    character = form.get("character") or ""
    relic_raw = form.get("relic") or DEFAULT_RELIC
    guild_name = form.get("guild_name") or ""
    qs = urlencode({"ally_code": ally_code, "character": character, "relic": relic_raw})

    if not name:
        error_qs = urlencode({"error": "Укажите название."})
        return RedirectResponse(f"/steal-build?{qs}&{error_qs}", status_code=303)

    try:
        relic = int(relic_raw)
    except ValueError:
        relic = 0

    filt = {"ally_code": ally_code, "character": character, "relic": relic, "guild_name": guild_name}
    ok = database.create_steal_build_preset(name, filt, user["discord_id"], guild_id=user["guild_id"])
    if not ok:
        error_qs = urlencode({"error": f"«{name}» уже сохранено."})
        return RedirectResponse(f"/steal-build?{qs}&{error_qs}", status_code=303)
    return RedirectResponse(f"/steal-build?{qs}", status_code=303)


@router.post("/presets/{preset_id}/delete", response_class=HTMLResponse)
async def preset_delete(preset_id: int, user: dict = Depends(require_officer_access)):
    database.delete_steal_build_preset(preset_id, guild_id=user["guild_id"])
    return RedirectResponse("/steal-build", status_code=303)
