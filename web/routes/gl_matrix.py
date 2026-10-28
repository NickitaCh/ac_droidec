"""Матрица ГЛ — готовность каждого игрока к каждой ГЛ (флагману, путешествию) по
требованиям «Руководства», как GL progress в HotUtils. Только из кэша ежечасного синка
ростеров (services/guild_units.py::guide_matrix), без запросов в игру."""

import asyncio
import time
from pathlib import Path

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from services import guild_units
from web.deps import require_officer_access

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent.parent / "templates"))
KINDS = ("gl", "capital", "journey")
CACHE_SECONDS = 15 * 60  # чтение 50 ростеров из кэша — ~8 с, а ростеры обновляются раз в час
_cache: dict[tuple[int, str], tuple[float, dict]] = {}


@router.get("", response_class=HTMLResponse)
async def gl_matrix_page(request: Request, kind: str = "gl", user: dict = Depends(require_officer_access)):
    if kind not in KINDS:
        kind = "gl"
    key = (user["guild_id"], kind)
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < CACHE_SECONDS:
        matrix = hit[1]
    else:
        matrix = await asyncio.to_thread(guild_units.guide_matrix, user["guild_id"], kind)
        _cache[key] = (time.time(), matrix)
    return templates.TemplateResponse(request, "gl_matrix.html", {
        "user": user, "kind": kind, "kinds": [(k, matrix["kinds"][k]) for k in KINDS],
        "guides": matrix["guides"], "players": matrix["players"],
    })
