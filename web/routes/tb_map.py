"""Карта ТБ и личные паки участников."""
import asyncio
from pathlib import Path
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates

import database
from services import tb_atlas, tb_covert, tb_personal, tb_projection, tb_squads
from web.deps import require_member_access

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent.parent / "templates"))

SIDES = {"dark": "Тёмная сторона", "mixed": "Смешанная", "light": "Светлая сторона"}
MISSION_LABELS = {"usual": "Наземный бой", "fleet": "Бой флота", "archived": "Архивная миссия"}
COLORS = ["#d45830", "#bf8148", "#995360", "#8da7be", "#737a71", "#a3acba",
          "#7f9e9c", "#bd86ae", "#d5ad6f", "#b79b74", "#a9bdcf", "#b5d8ee",
          "#cdbb94", "#988d77", "#6c9d65", "#c0ac69", "#9daca4", "#56a6a0", "#78bdc2", "#7d98be"]


def _rote_zones(guild_id):
    library = tb_squads.library(guild_id)
    zones = {}
    for phase in range(1, 7):
        for planet in tb_projection.phase_planets(phase):
            zones[planet] = library.get(planet, {"phase": phase, "ru": tb_covert.planet_ru(planet), "missions": []})
    colors = dict(zip(tb_atlas.ROTE_COORDINATES, COLORS, strict=True))
    for key, zone in zones.items():
        zone["side"] = next((s for s, branch in tb_projection.BRANCHES.items() if key in branch), "bonus")
        zone["side_label"] = SIDES.get(zone["side"], "Бонусная планета")
        zone["color"] = colors[key]
        zone["art"] = tb_atlas.ROTE_ART_NAMES.get(key, key.lower())
        zone["x"], zone["y"] = tb_atlas.ROTE_COORDINATES[key]
    return zones


def _zones(event, guild_id):
    if event not in tb_atlas.EVENTS:
        raise HTTPException(404, "Территориальная битва не найдена")
    return _rote_zones(guild_id) if event == "rote" else tb_atlas.legacy_zones(event)


def _data(user, event, planet):
    lib = _zones(event, user["guild_id"])
    planet = planet if planet is not None else next(iter(lib))
    if planet not in lib:
        raise HTTPException(404, "Зона ТБ не найдена")
    personal = tb_personal.notes(str(user["discord_id"]))
    for key, p in lib.items():
        p["note_count"] = sum(1 for planet, _ in personal if planet == key)
        known = {m["id"] for m in p["missions"]}
        counts = {}
        for item in p["missions"]:
            label = MISSION_LABELS.get(item["type"], "Спецмиссия")
            counts[label] = counts.get(label, 0) + 1
            item["map_label"] = label
            item["map_number"] = counts[label]
        for note_planet, mission in personal:
            if note_planet == key and mission != "general" and mission not in known:
                label = MISSION_LABELS["archived"]
                counts[label] = counts.get(label, 0) + 1
                p["missions"].append({"id": mission, "label": "Архивная миссия", "type": "archived",
                                      "requirements": [], "enemies": [], "squads": [], "removed_at": True,
                                      "map_label": label, "map_number": counts[label]})
        p["mission_counts"] = counts
    ids = {u for m in lib[planet]["missions"] for s in m["squads"] for u in s["units"]}
    return lib, planet, database.get_game_unit_names(sorted(ids)), personal


@router.get("")
async def page(request: Request, planet: str | None = None, saved: bool = False, event: str = "rote",
               mission: str = "general", embedded: bool = False,
               user: dict = Depends(require_member_access)):
    planet_view = event == "rote" and planet is not None
    lib, planet, names, notes = await asyncio.to_thread(_data, user, event, planet)
    groups = {"left": [], "right": [], "other": []}
    for item in lib[planet]["missions"]:
        position = item.get("position")
        group = groups[position] if position in ("left", "right") and not item.get("removed_at") else groups["other"]
        group.append(item)
    missions = [item for group in groups.values() for item in group]
    coordinates = tb_atlas.ROTE_MISSION_PINS[lib[planet]["art"]] if planet_view else {}
    surface_pins = [(item, coordinates[item["id"]]) for item in missions
                    if item["id"] in coordinates and not item.get("removed_at")]
    if mission not in {item["id"] for item in missions}:
        mission = "general"
    return templates.TemplateResponse(request, "tb_map.html", {
        "user": user, "lib": lib, "planet": planet, "selected": lib[planet],
        "names": names, "notes": notes, "saved": saved, "mission": mission, "embedded": embedded,
        "groups": groups, "missions": missions, "events": tb_atlas.EVENTS, "event": event,
        "planet_view": planet_view, "surface_pins": surface_pins})


@router.post("/save")
async def save(planet: str = Form(...), mission: str = Form(...), body: str = Form("", max_length=4000),
               embedded: bool = Form(False), event: str = Form("rote"),
               user: dict = Depends(require_member_access)):
    if event not in tb_atlas.EVENTS:
        raise HTTPException(400, "Неизвестная территориальная битва")
    lib = await asyncio.to_thread(_zones, event, user["guild_id"])
    notes = await asyncio.to_thread(tb_personal.notes, str(user["discord_id"]))
    if planet not in lib or (
        mission != "general"
        and mission not in {m["id"] for m in lib[planet]["missions"]}
        and (planet, mission) not in notes
    ):
        raise HTTPException(400, "Неизвестная планета или миссия")
    await asyncio.to_thread(tb_personal.save, str(user["discord_id"]), planet, mission, body)
    params = {"planet": planet, "mission": mission, "saved": "true"}
    if event != "rote":
        params["event"] = event
    if embedded:
        params["embedded"] = "true"
    anchor = "#mission-map" if event == "rote" else "#mission-" + mission
    return RedirectResponse("/tb/map?" + urlencode(params) + anchor, status_code=303)
