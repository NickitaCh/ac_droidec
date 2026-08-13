from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

import database

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent.parent / "templates"))


@router.get("/", response_class=HTMLResponse)
async def home(request: Request):
    user = request.session.get("user")
    guild_cfg = None
    if user and user.get("guild_id") is not None:
        guild_cfg = database.get_guild_config(user["guild_id"])

    error = request.query_params.get("error")
    return templates.TemplateResponse(request, "dashboard.html", {
        "user": user,
        "guild_cfg": guild_cfg,
        "error": error,
    })
