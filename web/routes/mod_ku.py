"""МодКу (/mod-ku) — рейтинг качества модов гильдии, расчёт в services/mod_ku.py. Только
супер-админам (решение пользователя 2026-10-14; Discord-команду убрали). Считается по
гильдии, выбранной в переключателе гильдий. Переключатель режима (с плейтами / обычный)
показывается и принимается только при флаге mod_ku_plates (сейчас только АС)."""

import asyncio
from pathlib import Path

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

import database
from services import mod_ku
from web.deps import require_super_admin

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent.parent / "templates"))


@router.get("", response_class=HTMLResponse)
async def mod_ku_page(request: Request, user: dict = Depends(require_super_admin)):
    guild_id = user["guild_id"]
    if guild_id is None:
        raise HTTPException(status_code=400, detail="Выберите гильдию в переключателе гильдий.")
    data = await asyncio.to_thread(mod_ku.compute_guild, guild_id)

    selected = request.query_params.get("player", "")
    detail = next((r for r in data["rows"] if r["ally_code"] == selected and r["result"]), None)
    names = {}
    if detail:
        names = database.get_game_unit_names([c["base_id"] for c in detail["result"]["weakest"]])

    return templates.TemplateResponse(request, "mod_ku.html", {
        "user": user,
        "rows": data["rows"],
        "mode": data["mode"],
        "modes": mod_ku.MODES,
        "can_switch": mod_ku.plates_mode_available(guild_id),
        "detail": detail,
        "unit_names": names,
        "explanation": mod_ku.EXPLANATION,
        "explanation_plates": mod_ku.EXPLANATION_PLATES,
    })


@router.post("/mode")
async def mod_ku_set_mode(mode: str = Form(...), user: dict = Depends(require_super_admin)):
    try:
        mod_ku.set_mode(user["guild_id"], mode)
    except ValueError as e:
        raise HTTPException(status_code=403, detail=str(e))
    return RedirectResponse("/mod-ku", status_code=303)
