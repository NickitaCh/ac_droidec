"""Веб-only "Спиздить билд" (/steal-build) — форма+рендер отчёта, расчёт целиком в
services/steal_build.py. GET с query-параметрами (как /mod-builder, /mod-search) —
результат можно заблокмаркить/переоткрыть по ссылке."""

from pathlib import Path

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
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


@router.get("", response_class=HTMLResponse)
async def steal_build_form(request: Request, user: dict = Depends(require_officer_access)):
    qp = request.query_params
    ally_code = qp.get("ally_code", "").strip()
    character = qp.get("character", "")
    relic_raw = qp.get("relic", DEFAULT_RELIC)

    context = {
        "user": user,
        "ally_code": ally_code,
        "selected_character": character,
        "selected_character_label": _char_label(character) if character else "",
        "selected_relic": relic_raw,
        "result": None,
        "loading": False,
        "error": None,
    }

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

    report = await steal_build.build_report(comlink, stat_calc, character, lookup, relic)
    if report["error"]:
        context["error"] = report["error"]
        return templates.TemplateResponse(request, "steal_build.html", context)

    context["result"] = report
    return templates.TemplateResponse(request, "steal_build.html", context)
