"""Веб-only "Анализ модинга" (/mod-analysis) — форма (персонаж + целевой релик, опционально
код союзника/ID чужой гильдии) + рендер отчёта, расчёт целиком в services/mod_analysis.py.
GET с query-параметрами (как /mod-search, /steal-build) — результат можно заблокмаркить/
переоткрыть по ссылке.

Реализация ТЗ из "Описание модели анализа модинга персонажа по гильдии.pdf" (пользователь,
чат 2026-09-15). По умолчанию данные — уже закэшированный player_unit_cache своей гильдии,
без обращений к Comlink, кроме однократной сборки StatCalc. Опция "чужая гильдия" (добавлено
2026-09-16, по прямому запросу пользователя) переиспользует services.steal_build.resolve_guild
и бьёт по Comlink живьём, как /steal-build."""

from pathlib import Path
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

import database
import services.mod_analysis as mod_analysis
import services.stat_forecast as stat_forecast
import services.steal_build as steal_build
from services import feature_flags

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent.parent / "templates"))

DEFAULT_RELIC = "9"


def _view_urls(character: str, relic_raw: str, guild_ref: str) -> dict:
    """Ссылки переключателя Δ/%/Итог (запрос пользователя 2026-09-22) — обычный GET-реюз
    страницы с другим view, тот же паттерн, что segmented-toggle в activity.html и др."""
    base_params = {"character": character, "relic": relic_raw}
    if guild_ref:
        base_params["guild_ref"] = guild_ref
    return {v: "/mod-analysis?" + urlencode({**base_params, "view": v}) for v in mod_analysis.VIEWS}


def _get_comlink():
    # Как в services/stat_forecast.py/steal_build.py — веб-процесс не поднимает диска-клиента,
    # строит свой поверх того же comlink-сайдкара.
    from swgoh_comlink import SwgohComlink
    return SwgohComlink(url="http://localhost:3000")


def _char_label(base_id: str) -> str:
    return database.get_game_unit_name(base_id) or base_id


def _history_rows(guild_id: int):
    return [
        {
            "id": hid, "character_label": _char_label(base_id), "base_id": base_id, "relic": relic,
            "created_at": created_at, "guild_ref": guild_ref or "", "guild_name": guild_name or "",
        }
        for hid, base_id, relic, _created_by, created_at, guild_ref, guild_name in database.get_mod_analysis_history(guild_id=guild_id)
    ]


@router.get("", response_class=HTMLResponse)
async def mod_analysis_page(request: Request, user: dict = Depends(feature_flags.require_feature("mod_analysis"))):
    qp = request.query_params
    guild_id = user["guild_id"]
    character = qp.get("character", "")
    relic_raw = qp.get("relic", DEFAULT_RELIC)
    guild_ref = qp.get("guild_ref", "").strip()
    view = qp.get("view", "delta")
    if view not in mod_analysis.VIEWS:
        view = "delta"

    history_id = qp.get("history_id")
    if history_id:
        entries = database.get_mod_analysis_history(guild_id=guild_id)
        entry = next((e for e in entries if e[0] == int(history_id)), None)
        if entry:
            _hid, character, relic_raw, _created_by, _created_at, guild_ref, _guild_name = entry
            relic_raw = str(relic_raw)
            guild_ref = guild_ref or ""

    context = {
        "user": user,
        "selected_character": character,
        "selected_character_label": _char_label(character) if character else "",
        "selected_relic": relic_raw,
        "guild_ref": guild_ref,
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

    guild_name_for_history = None
    if guild_ref:
        lookup = await steal_build.resolve_guild(comlink, guild_ref)
        if not lookup.ok:
            context["error"] = lookup.error
            return templates.TemplateResponse(request, "mod_analysis.html", context)
        guild_name_for_history = lookup.guild_name
        report = await mod_analysis.build_report_live(comlink, stat_calc, character, relic, lookup, view=view)
    else:
        report = await mod_analysis.build_report(stat_calc, character, relic, guild_id=guild_id, view=view)

    if report["error"]:
        context["error"] = report["error"]
        return templates.TemplateResponse(request, "mod_analysis.html", context)

    context["result"] = report
    context["view_urls"] = _view_urls(character, relic_raw, guild_ref)

    database.add_mod_analysis_history(
        character, relic, user["discord_id"], guild_id=guild_id,
        guild_ref=guild_ref or None, guild_name=guild_name_for_history,
    )
    context["history"] = _history_rows(guild_id)

    return templates.TemplateResponse(request, "mod_analysis.html", context)
