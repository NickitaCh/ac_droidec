from pathlib import Path
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Request, Form
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
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
        "registered_name": request.query_params.get("registered_name"),
    })


@router.get("/api/players", response_class=JSONResponse)
async def players_search(q: str = "", user: dict = Depends(require_guild_access)):
    if not q or len(q.strip()) < 2:
        return []
    search = q.strip().lower()
    matches = [
        {"discord_id": discord_id, "name": ingame_name}
        for discord_id, ally_code, ingame_name in database.get_all_main_registrations(user["guild_id"])
        if search in (ingame_name or "").lower()
    ]
    return matches[:20]


@router.post("", response_class=HTMLResponse)
async def submit_registration(
    request: Request,
    ally_code: str = Form(...),
    is_alt: bool = Form(False),
    discord_id_search: str = Form(""),
    discord_id_manual: str = Form(""),
    user: dict = Depends(require_guild_access),
):
    # Веб-дашборд уже целиком officer-only (require_guild_access), поэтому, в отличие
    # от бота (где /регистрация участник:@X отдельно проверяет is_officer_for_resolved_guild),
    # доп. проверка роли тут не нужна — сюда и так не попасть без офицерского тира.
    target_discord_id = (discord_id_search or discord_id_manual).strip()
    if target_discord_id and not target_discord_id.isdigit():
        return RedirectResponse(f"/registration?{urlencode({'error': 'Discord ID должен состоять только из цифр'})}", status_code=303)
    target_discord_id = target_discord_id or user["discord_id"]

    comlink = _get_comlink()
    result = await register_player(comlink, target_discord_id, ally_code, is_alt=is_alt)
    if not result.ok:
        return RedirectResponse(f"/registration?{urlencode({'error': result.error})}", status_code=303)
    params = {"registered_name": result.ingame_name} if target_discord_id != user["discord_id"] else {}
    return RedirectResponse(f"/registration?{urlencode(params)}" if params else "/registration", status_code=303)
