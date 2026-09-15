"""Веб-only "Анализ модинга" (/mod-analysis) — форма (персонаж + целевой релик) + рендер
отчёта, расчёт целиком в services/mod_analysis.py. GET с query-параметрами (как
/mod-search, /steal-build) — результат можно заблокмаркить/переоткрыть по ссылке.

Реализация ТЗ из "Описание модели анализа модинга персонажа по гильдии.pdf" (пользователь,
чат 2026-09-15). Данные — уже закэшированный player_unit_cache всей нашей гильдии (не чужой,
в отличие от /steal-build) — без обращений к Comlink, кроме однократной сборки StatCalc."""

from pathlib import Path

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

import database
import services.mod_analysis as mod_analysis
import services.stat_forecast as stat_forecast
from services import feature_flags

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent.parent / "templates"))

DEFAULT_RELIC = "9"


def _get_comlink():
    # Как в services/stat_forecast.py/steal_build.py — веб-процесс не поднимает диска-клиента,
    # строит свой поверх того же comlink-сайдкара.
    from swgoh_comlink import SwgohComlink
    return SwgohComlink(url="http://localhost:3000")


def _char_label(base_id: str) -> str:
    return database.get_game_unit_name(base_id) or base_id


def _history_rows(guild_id: int):
    return [
        {"id": hid, "character_label": _char_label(base_id), "base_id": base_id, "relic": relic, "created_at": created_at}
        for hid, base_id, relic, _created_by, created_at in database.get_mod_analysis_history(guild_id=guild_id)
    ]


@router.get("", response_class=HTMLResponse)
async def mod_analysis_page(request: Request, user: dict = Depends(feature_flags.require_feature("mod_analysis"))):
    qp = request.query_params
    guild_id = user["guild_id"]
    character = qp.get("character", "")
    relic_raw = qp.get("relic", DEFAULT_RELIC)

    history_id = qp.get("history_id")
    if history_id:
        entries = database.get_mod_analysis_history(guild_id=guild_id)
        entry = next((e for e in entries if e[0] == int(history_id)), None)
        if entry:
            _hid, character, relic_raw, _created_by, _created_at = entry
            relic_raw = str(relic_raw)

    context = {
        "user": user,
        "selected_character": character,
        "selected_character_label": _char_label(character) if character else "",
        "selected_relic": relic_raw,
        "result": None,
        "loading": False,
        "error": None,
        "history": _history_rows(guild_id),
    }
    if qp.get("error"):
        context["error"] = qp.get("error")

    if not character:
        return templates.TemplateResponse(request, "mod_analysis.html", context)

    try:
        relic = int(relic_raw)
    except ValueError:
        context["error"] = "Некорректный уровень реликвии."
        return templates.TemplateResponse(request, "mod_analysis.html", context)
    if not (0 <= relic <= 10):
        context["error"] = "Релик должен быть от 0 до 10."
        return templates.TemplateResponse(request, "mod_analysis.html", context)

    try:
        comlink = _get_comlink()
        stat_calc = await stat_forecast.get_stat_calc(comlink)
    except Exception:
        context["loading"] = True
        return templates.TemplateResponse(request, "mod_analysis.html", context)

    report = await mod_analysis.build_report(stat_calc, character, relic, guild_id=guild_id)
    if report["error"]:
        context["error"] = report["error"]
        return templates.TemplateResponse(request, "mod_analysis.html", context)

    context["result"] = report

    database.add_mod_analysis_history(character, relic, user["discord_id"], guild_id=guild_id)
    context["history"] = _history_rows(guild_id)

    return templates.TemplateResponse(request, "mod_analysis.html", context)
