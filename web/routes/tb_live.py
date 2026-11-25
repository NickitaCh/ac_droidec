"""Officer view of samples collected through the saved read-only game session."""
import asyncio
from datetime import datetime
from pathlib import Path
import secrets
import time
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

import database
from services import ea_connect, feature_flags, game_account, game_watch, tb_forecast, tb_live, tb_live_order
import tb_plan_reader
from web.deps import selected_roster_account

router = APIRouter()
from web.routes import tb_commands
router.include_router(tb_commands.router)
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parents[1] / "templates"))
templates.env.globals["tb_classic_nav"] = lambda user: feature_flags.is_enabled(user["guild_id"], "tb_classic_nav")
_access = feature_flags.require_feature("tb_reports")
_msk = ZoneInfo("Europe/Moscow")
_refresh_lock = asyncio.Lock()
_min_refresh_interval = 60


def _pilot(user):
    owner, guild_id = str(user["discord_id"]), user["guild_id"]
    try:
        game_account.require_game_officer(owner, guild_id, user.get("bot_profile"), user.get("tier"))
        code = game_account.select_account(owner, guild_id)
    except game_account.AccountError as error:
        raise HTTPException(403, str(error)) from None
    return owner, code


def _clock(epoch, fmt="%d.%m %H:%M:%S"):
    return datetime.fromtimestamp(epoch, _msk).strftime(fmt)


def _render(request, user, owner, ally_code, selected=None, error=None):
    personal_code = selected_roster_account(user) if user.get("active_ally_code") else ally_code
    recent = tb_live.history(user["guild_id"], owner)
    index = next((i for i, (ts, _) in enumerate(recent) if ts == selected), 0)
    captured, state = recent[index] if recent else (None, None)
    zones, players, personal = [], [], []
    stale = bool(state and state["end_ms"] <= time.time() * 1000)
    if state:
        previous = recent[index + 1][1] if index + 1 < len(recent) else None
        if previous and (previous["event"], previous["phase"]) != (state["event"], state["phase"]):
            previous = None
        roster = database.get_roster_by_player_id(user["guild_id"])
        players = [(roster.get(pid, (None, "Игрок вне ростера"))[1], score)
                   for pid, score in sorted(state["players"].items(), key=lambda pair: -pair[1])]
        unit_ids = {slot["unit"] for zone in state["zones"].values() if zone["status"] == tb_live.OPEN_ZONE
                    for operation in zone["operations"] for slot in operation["slots"] if slot["unit"]}
        unit_names = database.get_game_unit_names(sorted(unit_ids)) if unit_ids else {}
        player_names = database.get_player_names(user["guild_id"]) if state.get("order_name") else {}
        if personal_code and not selected and not stale and state.get("order_name"):
            owned = database.get_player_units(personal_code)
            personal = tb_live_order.personal_preview(state, personal_code, set(owned) if owned else None)
            for row in personal:
                row["unit"] = unit_names.get(row["unit"], row["unit"])
                row["planet"] = tb_plan_reader.PLANET_NAME_TO_RU.get(row["planet"], row["planet"]).capitalize()
        planet_defs = tb_forecast.zones().get("planets", {})
        for key, zone in state["zones"].items():
            if zone["status"] != tb_live.OPEN_ZONE:
                continue
            planet = tb_forecast.planet_of(key) if key.startswith("tb3_mixed_") else None
            name = tb_plan_reader.PLANET_NAME_TO_RU.get(planet[1], planet[1]).capitalize() if planet else key
            old = previous["zones"].get(key, {}).get("points") if previous else None
            change = zone["points"] - old if zone["points"] is not None and old is not None else None
            operations = []
            for operation in zone["operations"]:
                slots = [
                    (unit_names.get(slot["unit"], slot["unit"]), slot["donor"],
                     roster.get(slot["donor"], (None, "Игрок вне ростера"))[1] if slot["donor"] else "")
                    for slot in operation["slots"]
                ]
                order = operation.get("order")
                operations.append({
                    "number": tb_live_order.operation_number(operation["id"]),
                    "slots": slots, "filled": sum(bool(slot[1]) for slot in slots),
                    "order": tb_live_order.display(order, unit_names, player_names) if order is not None else None,
                    "order_done": sum(row[-1] == "done" for row in order or ()),
                })
            operations.sort(key=lambda op: op["number"] or 99)
            thresholds = planet_defs.get(planet[1], {}).get("stars", []) if planet else []
            stars = sum(zone["points"] >= threshold for threshold in thresholds) if zone["points"] is not None else None
            zones.append({"id": key, "name": name, "points": zone["points"], "change": change, "stars": stars if thresholds else None,
                          "missions": zone["missions"], "special": zone.get("special", ()), "operations": operations,
                          "filled": sum(op["filled"] for op in operations),
                          "slots": sum(len(op["slots"]) for op in operations)})
    request.session.setdefault("tb_live_csrf", secrets.token_urlsafe(32))
    return templates.TemplateResponse(request, "tb_live.html", {
        "user": user, "csrf": request.session["tb_live_csrf"],
        "captured": _clock(captured) if captured else None, "state": state, "zones": zones, "players": players,
        "end": _clock(state["end_ms"] / 1000, "%d.%m %H:%M") if state else None,
        "stale": stale, "personal": personal, "personal_code": personal_code,
        "history": [(ts, _clock(ts), snap["phase"] if snap else None) for ts, snap in recent],
        "selected": selected, "error": error,
        "watch": game_watch.get(user["guild_id"], owner, "tb"), "watch_intervals": game_watch.INTERVALS,
        "game_connected": bool(ea_connect.target_for_registration(owner, user["guild_id"], ally_code)),
    }, headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"})


@router.get("/tb/live", response_class=HTMLResponse)
def page(request: Request, snapshot: int | None = None, user: dict = Depends(_access)):
    owner, code = _pilot(user)
    return _render(request, user, owner, code, selected=snapshot)


def _check_csrf(request, csrf):
    expected = request.session.get("tb_live_csrf")
    if not expected or not secrets.compare_digest(csrf.encode(), expected.encode()):
        raise HTTPException(403)


@router.post("/tb/live", response_class=HTMLResponse)
async def sample(request: Request, csrf: str = Form(...),
                 user: dict = Depends(_access)):
    _check_csrf(request, csrf)
    owner, code = await asyncio.to_thread(_pilot, user)
    guild_id = user["guild_id"]
    error = None
    async with _refresh_lock:
        if game_watch.active(guild_id, owner, "tb"):
            error = "Снимки обновляет наблюдение."
        else:
            recent = await asyncio.to_thread(tb_live.history, guild_id, owner, 1)
            if not recent or time.time() - recent[0][0] >= _min_refresh_interval:
                try:
                    await game_watch.refresh_saved(owner, guild_id, user["bot_profile"], user["tier"], code, "tb",
                                                   reconnect_if_expired=True)
                except game_account.AccountError as exc:
                    error = str(exc)
            else:
                error = "Снимок младше минуты. Показаны уже полученные данные."
    return await asyncio.to_thread(_render, request, user, owner, code, error=error)


@router.post("/tb/live/watch")
async def watch_action(request: Request, action: str = Form(...), csrf: str = Form(...),
                       interval: int = Form(300), confirm_login: bool = Form(False),
                       user: dict = Depends(_access)):
    _check_csrf(request, csrf)
    owner, code = await asyncio.to_thread(_pilot, user)
    guild_id = user["guild_id"]
    if action == "stop":
        game_watch.stop(guild_id, owner, "tb")
    elif action == "start" and confirm_login and interval in game_watch.INTERVALS:
        async with _refresh_lock:
            await game_watch.start(owner, guild_id, user["bot_profile"], user["tier"], code, "tb", interval)
    else:
        raise HTTPException(400, "Проверьте интервал и подтвердите новый вход в игру.")
    return RedirectResponse("/tb/live", status_code=303)
