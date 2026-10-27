"""Разведка ВГ — web /tw/scout: своя гильдия против соперника (сводка, участники по рангу,
плейты на обеих сторонах). Расчёт и фоновый сбор — services/tw_scout.py."""
import json
from pathlib import Path
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

import database
from services import feature_flags, guild_compare, mod_compare, stat_forecast, tw_scout

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent.parent / "templates"))

_require = feature_flags.require_feature("tw_order")
PLATES_STATE = "tw_scout_plates"  # bot_state гильдии: JSON-список плейтов, выбранных для разведки


def _get_comlink():
    from swgoh_comlink import SwgohComlink
    return SwgohComlink(url="http://localhost:3000")


def _selected_plates(guild_id: int) -> list[str]:
    raw = database.get_bot_state(PLATES_STATE, guild_id=guild_id)
    try:
        return json.loads(raw) if raw else []
    except ValueError:
        return []


@router.get("", response_class=HTMLResponse)
async def scout_page(request: Request, opp: str = "", user: dict = Depends(_require)):
    guild_id = user["guild_id"]
    opp = opp.strip()
    all_plates = database.get_all_stat_requirement_plates(guild_id=guild_id)
    selected = [p for p in _selected_plates(guild_id) if p in all_plates]
    ctx = {"user": user, "opp": opp, "recent": guild_compare.load_recent_opponents(guild_id),
           "label": guild_compare.opponent_label, "plates": all_plates, "selected_plates": selected,
           "plate_sizes": {p: len(database.get_stat_requirement_characters(p, guild_id=guild_id)) for p in all_plates},
           "report": None, "progress": None, "error": request.query_params.get("error"), "lookup": None}
    if opp:
        comlink = _get_comlink()
        lookup = await mod_compare.resolve(comlink, opp)
        if not lookup.ok:
            ctx["error"] = lookup.error
        elif lookup.swgoh_guild_id == (database.get_guild_config(guild_id) or {}).get("swgoh_guild_id"):
            ctx["error"] = "Это ваша же гильдия."
        else:
            ctx["lookup"] = lookup
            ctx["report"] = tw_scout.load_report(guild_id, lookup.swgoh_guild_id)
            ctx["progress"] = tw_scout.progress(guild_id, lookup.swgoh_guild_id)
            if ctx["report"] is None and not (ctx["progress"] or {}).get("running"):
                stat_calc = stat_forecast.cached_stat_calc()
                if stat_calc is None:
                    stat_forecast.start_background_build(comlink)
                    ctx["progress"] = {"running": True, "stage": "Загружаю калькулятор статов", "done": 0, "total": 0}
                else:
                    tw_scout.start(comlink, guild_id, lookup, selected, stat_calc, refetch=False)
                    ctx["progress"] = tw_scout.progress(guild_id, lookup.swgoh_guild_id)
    return templates.TemplateResponse(request, "tw_scout.html", ctx)


@router.post("/run", response_class=HTMLResponse)
async def scout_run(request: Request, opp: str = Form(...), refetch: str = Form(default=""),
                    user: dict = Depends(_require)):
    """Пересчитать отчёт (с выбранными плейтами); refetch — заново собрать ростеры соперника."""
    guild_id = user["guild_id"]
    form = await request.form()
    all_plates = set(database.get_all_stat_requirement_plates(guild_id=guild_id))
    plates = [p for p in form.getlist("plates") if p in all_plates]
    database.set_bot_state(PLATES_STATE, json.dumps(plates, ensure_ascii=False), guild_id=guild_id)
    comlink = _get_comlink()
    lookup = await mod_compare.resolve(comlink, opp.strip())
    if not lookup.ok:
        return RedirectResponse(f"/tw/scout?{urlencode({'opp': opp, 'error': lookup.error})}", status_code=303)
    stat_calc = stat_forecast.cached_stat_calc()
    if stat_calc is None:
        stat_forecast.start_background_build(comlink)
        return RedirectResponse(f"/tw/scout?{urlencode({'opp': opp, 'error': 'Калькулятор статов ещё загружается — попробуйте через минуту.'})}", status_code=303)
    tw_scout.start(comlink, guild_id, lookup, plates, stat_calc, refetch=bool(refetch))
    return RedirectResponse(f"/tw/scout?{urlencode({'opp': opp})}", status_code=303)
