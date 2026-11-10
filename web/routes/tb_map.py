"""Карта ТБ и личные паки участников."""
import asyncio
from pathlib import Path
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates

import database
from services import tb_covert, tb_personal, tb_projection, tb_squads
from web.deps import require_member_access

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent.parent / "templates"))

SIDES = {"dark": "Тёмная сторона", "mixed": "Смешанная", "light": "Светлая сторона"}
COLORS = ["#d45830", "#bf8148", "#995360", "#8da7be", "#737a71", "#a3acba",
          "#7f9e9c", "#bd86ae", "#d5ad6f", "#b79b74", "#a9bdcf", "#b5d8ee",
          "#cdbb94", "#988d77", "#6c9d65", "#c0ac69", "#9daca4", "#56a6a0", "#78bdc2", "#7d98be"]


def _data(user):
    lib = tb_squads.library(user["guild_id"])
    for branch in tb_projection.BRANCHES.values():
        for phase, planet in enumerate(branch, 1):
            lib.setdefault(planet, {"phase": phase, "ru": tb_covert.planet_ru(planet), "missions": []})
    for planet, phase in tb_projection.BONUS_PHASE.items():
        lib.setdefault(planet, {"phase": phase, "ru": tb_covert.planet_ru(planet), "missions": []})
    personal = tb_personal.notes(str(user["discord_id"]))
    planets = [p for branch in tb_projection.BRANCHES.values() for p in branch] + list(tb_projection.BONUS_PHASE)
    for key, p in lib.items():
        p["side"] = next((s for s, branch in tb_projection.BRANCHES.items() if key in branch), "bonus")
        p["side_label"] = SIDES.get(p["side"], "Бонусная планета")
        p["color"] = COLORS[planets.index(key)] if key in planets else "#829aba"
        p["note_count"] = sum(1 for planet, _ in personal if planet == key)
        known = {m["id"] for m in p["missions"]}
        for planet, mission in personal:
            if planet == key and mission != "general" and mission not in known:
                p["missions"].append({"id": mission, "label": "Архивная миссия", "type": "archived",
                                      "requirements": [], "enemies": [], "squads": [], "removed_at": True})
    ids = {u for p in lib.values() for m in p["missions"] for s in m["squads"] for u in s["units"]}
    return lib, database.get_game_unit_names(sorted(ids)), personal


@router.get("")
async def page(request: Request, planet: str = "Mustafar", saved: bool = False,
               mission: str = "general", embedded: bool = False,
               user: dict = Depends(require_member_access)):
    lib, names, notes = await asyncio.to_thread(_data, user)
    if planet not in lib:
        raise HTTPException(404, "Планета не найдена")
    return templates.TemplateResponse(request, "tb_map.html", {
        "user": user, "lib": lib, "planet": planet, "selected": lib[planet],
        "names": names, "notes": notes, "saved": saved, "mission": mission, "embedded": embedded,
        "stages": [{"number": n, "planets": [(key, lib[key]) for key in tb_projection.phase_planets(n)
                                                 if key in lib]} for n in range(1, 7)]})


@router.post("/save")
async def save(planet: str = Form(...), mission: str = Form(...), body: str = Form("", max_length=4000),
               embedded: bool = Form(False),
               user: dict = Depends(require_member_access)):
    lib, _, _ = await asyncio.to_thread(_data, user)
    if planet not in lib or mission not in {"general", *(m["id"] for m in lib[planet]["missions"])}:
        raise HTTPException(400, "Неизвестная планета или миссия")
    await asyncio.to_thread(tb_personal.save, str(user["discord_id"]), planet, mission, body)
    params = {"planet": planet, "mission": mission, "saved": "true"}
    if embedded:
        params["embedded"] = "true"
    return RedirectResponse("/tb/map?" + urlencode(params)
                            + "#mission-" + mission, status_code=303)
