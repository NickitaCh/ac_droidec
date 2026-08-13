from pathlib import Path
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

import database
from services.registration import register_player
from web.deps import require_guild_access

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent.parent / "templates"))


def _get_comlink():
    # main.py создаёт единственный SwgohComlink и вешает на bot.comlink — веб-процесс
    # не поднимает диска-клиента, поэтому строит свой (тот же URL, тот же comlink-сайдкар).
    from swgoh_comlink import SwgohComlink
    return SwgohComlink(url="http://localhost:3000")


@router.get("", response_class=HTMLResponse)
async def my_registration(request: Request, user: dict = Depends(require_guild_access)):
    accounts = database.get_user_registrations(user["discord_id"], guild_id=user["guild_id"])
    return templates.TemplateResponse(request, "registration.html", {
        "user": user,
        "accounts": accounts,
        "error": request.query_params.get("error"),
    })


@router.post("", response_class=HTMLResponse)
async def submit_registration(
    request: Request,
    ally_code: str = Form(...),
    is_alt: bool = Form(False),
    user: dict = Depends(require_guild_access),
):
    comlink = _get_comlink()
    result = await register_player(comlink, user["guild_id"], user["discord_id"], ally_code, is_alt=is_alt)
    if not result.ok:
        return RedirectResponse(f"/registration?{urlencode({'error': result.error})}", status_code=303)
    return RedirectResponse("/registration", status_code=303)
