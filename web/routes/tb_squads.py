"""Отряды для боёв ВИ (/tb/squads, офицеры): база swgoh.ru + правки гильдии — скрыть
базовый отряд, добавить свой (services/tb_squads.py). У каждого отряда — сколько игроков
гильдии могут его выставить сейчас (по кэшу ростеров, 15 минут)."""

import asyncio
import time
from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

import database
from services import tb_squads
from web.deps import require_officer_access

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent.parent / "templates"))
CACHE_SECONDS = 15 * 60
_rosters: dict[int, tuple[float, dict, set]] = {}


def _guild_rosters(guild_id: int) -> tuple[dict, set]:
    hit = _rosters.get(guild_id)
    if hit and time.time() - hit[0] < CACHE_SECONDS:
        return hit[1], hit[2]
    codes = [c for c, _n in database.get_roster_by_player_id(guild_id).values() if c]
    rosters = database.get_player_units_bulk(codes)
    ships = {b for b, t in database.get_unit_types(sorted({b for r in rosters.values() for b in r})).items() if t == "ship"}
    _rosters[guild_id] = (time.time(), rosters, ships)
    return rosters, ships


def _page(guild_id: int, phase: int) -> dict:
    lib = tb_squads.library(guild_id)
    planets = {k: v for k, v in lib.items() if v["phase"] == phase}
    rosters, ships = _guild_rosters(guild_id)
    ids = {u for p in planets.values() for m in p["missions"] for s in m["squads"] for u in s["units"]}
    names = database.get_game_unit_names(sorted(ids))
    for p in planets.values():
        for m in p["missions"]:
            for s in m["squads"]:
                s["names"] = [names.get(u, u) for u in s["units"]]
                s["can"] = sum(1 for r in rosters.values()
                               if all(tb_squads._fits(r.get(u), p["min_relic"], u in ships) for u in s["units"]))
    return {"planets": planets, "members": len(rosters), "phases": sorted({v["phase"] for v in lib.values() if v["phase"]}),
            "fetched": tb_squads.base().get("fetched")}


@router.get("", response_class=HTMLResponse)
async def squads_page(request: Request, phase: int = 1, user: dict = Depends(require_officer_access)):
    data = await asyncio.to_thread(_page, user["guild_id"], phase)
    return templates.TemplateResponse(request, "tb_squads.html", {
        "user": user, "phase": phase, "source": tb_squads.SOURCE_NAME, "error": request.query_params.get("error"), **data})


def _back(phase: str, planet: str, error: str = "") -> RedirectResponse:
    anchor = "#" + planet.replace(" ", "_") if planet else ""
    return RedirectResponse(f"/tb/squads?phase={phase}" + (f"&error={quote(error)}" if error else "") + anchor, status_code=303)


@router.post("/hide")
async def squads_hide(planet: str = Form(...), mission: str = Form(...), key: str = Form(...), hidden: int = Form(...),
                      phase: str = Form("1"), user: dict = Depends(require_officer_access)):
    await asyncio.to_thread(tb_squads.set_hidden, user["guild_id"], planet, mission, key, bool(hidden), str(user["discord_id"]))
    return _back(phase, planet)


@router.post("/add")
async def squads_add(planet: str = Form(...), mission: str = Form(...), name: str = Form(""), units: str = Form(...),
                     phase: str = Form("1"), user: dict = Depends(require_officer_access)):
    """units — имена через запятую (рус./англ./сокращения), как в поиске юнитов на сайте."""
    resolved, unknown = [], []
    for part in [p.strip() for p in units.split(",") if p.strip()]:
        hit = database.search_game_units(part, limit=1)
        if hit:
            resolved.append(hit[0][0])
        else:
            unknown.append(part)
    if unknown:
        return _back(phase, planet, "Не нашёл: " + ", ".join(unknown))
    if not resolved or len(resolved) > 8 or len(set(resolved)) != len(resolved):
        return _back(phase, planet, "Нужно от 1 до 8 разных юнитов")
    await asyncio.to_thread(tb_squads.add_custom, user["guild_id"], planet, mission, name, resolved, str(user["discord_id"]))
    return _back(phase, planet)


@router.post("/delete")
async def squads_delete(planet: str = Form(...), mission: str = Form(...), key: str = Form(...),
                        phase: str = Form("1"), user: dict = Depends(require_officer_access)):
    await asyncio.to_thread(tb_squads.delete_custom, user["guild_id"], planet, mission, key, str(user["discord_id"]))
    return _back(phase, planet)
