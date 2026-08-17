from pathlib import Path
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

import database
from services.guild_admin import (
    add_grant,
    add_guild,
    add_super_admin,
    deactivate_guild,
    list_admins,
    list_guilds,
    remove_grant,
    remove_super_admin,
)
from web.deps import require_super_admin

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent.parent / "templates"))


def _get_comlink():
    # См. web/routes/registration.py::_get_comlink — веб-процесс не поднимает
    # диска-клиента, строит свой SwgohComlink на тот же comlink-сайдкар.
    from swgoh_comlink import SwgohComlink
    return SwgohComlink(url="http://localhost:3000")


@router.get("/guilds", response_class=HTMLResponse)
async def guilds_page(request: Request, user: dict = Depends(require_super_admin)):
    return templates.TemplateResponse(request, "admin_guilds.html", {
        "user": user,
        "guilds": list_guilds(active_only=False),
        "error": request.query_params.get("error"),
    })


@router.post("/guilds/add", response_class=HTMLResponse)
async def guilds_add(
    request: Request,
    ally_code: str = Form(...),
    discord_guild_id: str = Form(...),
    user: dict = Depends(require_super_admin),
):
    comlink = _get_comlink()
    result = await add_guild(comlink, ally_code, discord_guild_id)
    if not result.ok:
        return RedirectResponse(f"/admin/guilds?{urlencode({'error': result.error})}", status_code=303)
    return RedirectResponse("/admin/guilds", status_code=303)


@router.post("/guilds/{guild_id}/deactivate", response_class=HTMLResponse)
async def guilds_deactivate(guild_id: int, user: dict = Depends(require_super_admin)):
    deactivate_guild(guild_id)
    return RedirectResponse("/admin/guilds", status_code=303)


@router.get("/access", response_class=HTMLResponse)
async def access_page(request: Request, user: dict = Depends(require_super_admin)):
    data = list_admins()
    return templates.TemplateResponse(request, "admin_access.html", {
        "user": user,
        "super_admins": data["super_admins"],
        "grants": data["grants"],
        "guilds": list_guilds(active_only=True),
        "error": request.query_params.get("error"),
    })


@router.post("/access/super-admins/add", response_class=HTMLResponse)
async def super_admins_add(
    discord_id: str = Form(...),
    username: str = Form(""),
    user: dict = Depends(require_super_admin),
):
    add_super_admin(discord_id, username or None, user["discord_id"])
    return RedirectResponse("/admin/access", status_code=303)


@router.post("/access/super-admins/{discord_id}/remove", response_class=HTMLResponse)
async def super_admins_remove(discord_id: str, user: dict = Depends(require_super_admin)):
    remove_super_admin(discord_id)
    return RedirectResponse("/admin/access", status_code=303)


@router.post("/access/grants/add", response_class=HTMLResponse)
async def grants_add(
    request: Request,
    discord_id: str = Form(...),
    ally_code: str = Form(...),
    tier: str = Form(...),
    guild_id: int = Form(...),
    user: dict = Depends(require_super_admin),
):
    comlink = _get_comlink()
    result = await add_grant(comlink, discord_id, ally_code, guild_id, tier, user["discord_id"])
    if not result.ok:
        return RedirectResponse(f"/admin/access?{urlencode({'error': result.error})}", status_code=303)
    return RedirectResponse("/admin/access", status_code=303)


@router.post("/access/grants/{discord_id}/remove", response_class=HTMLResponse)
async def grants_remove(discord_id: str, user: dict = Depends(require_super_admin)):
    remove_grant(discord_id)
    return RedirectResponse("/admin/access", status_code=303)


@router.get("/access-log", response_class=HTMLResponse)
async def access_log_page(request: Request, user: dict = Depends(require_super_admin)):
    return templates.TemplateResponse(request, "admin_access_log.html", {
        "user": user,
        "entries": database.get_web_access_log(limit=200),
    })
