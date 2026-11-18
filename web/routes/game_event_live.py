"""Read-only event monitoring and personal TW defense preview."""
import asyncio
from datetime import datetime
from pathlib import Path
import secrets
import time
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

import database
from services import event_panels, game_account, game_account_readonly, game_event_live, game_watch, tw_def_notify
import tw_def_engine
from web.deps import require_officer_access

router = APIRouter()
from web.routes import tw_game_deploy
router.include_router(tw_game_deploy.router)
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parents[1] / "templates"))
_TITLES = {"tw": "ВГ сейчас", "raid": "Рейд сейчас"}
_MIN_REFRESH_INTERVAL = 60
_lock = asyncio.Lock()
_msk = ZoneInfo("Europe/Moscow")


def _scope(user, kind):
    if kind not in _TITLES:
        raise HTTPException(404)
    code = game_account_readonly.own_main(user, connected_only=False)
    if not code:
        raise HTTPException(403, "Доступно офицерам АС в тестовой версии с зарегистрированным основным аккаунтом.")
    return code


def _check_csrf(request, csrf):
    expected = request.session.get("game_event_csrf")
    if not expected or not secrets.compare_digest(csrf.encode(), expected.encode()):
        raise HTTPException(403)


def _check_plan(user, kind, plan):
    if plan is not None and (kind != "tw" or not database.get_tw_def_plan(user["guild_id"], plan)):
        raise HTTPException(404)


def _clock(value):
    return datetime.fromtimestamp(game_event_live.epoch(value), _msk).strftime("%d.%m %H:%M") if value else "—"


def _render(request, user, kind, plan_id, code, error=None):
    owner, guild_id = str(user["discord_id"]), user["guild_id"]
    cached = game_event_live.snapshot(guild_id, owner, kind)
    plan, preview = None, []
    if kind == "tw" and plan_id:
        plan = database.get_tw_def_plan(user["guild_id"], plan_id)
        if not plan:
            raise HTTPException(404, "План ВГ не найден.")
        owned = database.get_player_units(code)
        preview = game_event_live.tw_preview(
            plan, database.get_tw_def_assignments(user["guild_id"], plan_id), code,
            set(owned) if owned else None, tw_def_engine.ZONES_BY_KEY, tw_def_engine.zone_capacity,
        )
        unit_ids = sorted({unit for row in preview for unit in row["units"]})
        names = database.get_game_unit_names(unit_ids) if unit_ids else {}
        owned_datacrons = {}
        if any(row["datacron_ids"] for row in preview):
            owned_datacrons = {
                str(dc["id"]): dc for dc in database.get_player_datacrons_bulk([code]).get(code, [])
                if dc.get("id")
            }
        for row in preview:
            row["units"] = [names.get(unit, unit) for unit in row["units"]]
            row["datacrons"] = [
                tw_def_notify.describe_datacron(owned_datacrons[identifier], None)
                if identifier in owned_datacrons else "Назначенный датакрон отсутствует в сохранённом ростере."
                for identifier in row["datacron_ids"]
            ]
    roster = database.get_roster_by_player_id(user["guild_id"]) if kind == "raid" and cached.get("data") else {}
    request.session.setdefault("game_event_csrf", secrets.token_urlsafe(32))
    return templates.TemplateResponse(request, "game_event_live.html", {
        "user": user, "kind": kind, "title": _TITLES[kind],
        "csrf": request.session["game_event_csrf"], "cached": cached, "clock": _clock,
        "roster": roster, "preview": preview, "plan": plan,
        "error": error, "watch": game_watch.get(guild_id, owner, kind), "watch_intervals": game_watch.INTERVALS,
        "game_connected": bool(game_account_readonly._connection(owner, code)),
    }, headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"})


@router.get("/game-account/events/{kind}", response_class=HTMLResponse)
def page(request: Request, kind: str, plan: int | None = None, user: dict = Depends(require_officer_access)):
    code = _scope(user, kind)
    _check_plan(user, kind, plan)
    return _render(request, user, kind, plan, code)


@router.post("/game-account/events/{kind}", response_class=HTMLResponse)
async def sample(request: Request, kind: str, plan: int | None = None, csrf: str = Form(...),
                 user: dict = Depends(require_officer_access)):
    _check_csrf(request, csrf)
    code = await asyncio.to_thread(_scope, user, kind)
    await asyncio.to_thread(_check_plan, user, kind, plan)
    owner, guild_id = str(user["discord_id"]), user["guild_id"]
    error = None
    async with _lock:
        if game_watch.active(guild_id, owner, kind):
            error = "Снимки обновляет наблюдение."
        else:
            previous = await asyncio.to_thread(game_event_live.snapshot, guild_id, owner, kind)
            if time.time() - previous.get("at", 0) >= _MIN_REFRESH_INTERVAL:
                try:
                    await game_watch.refresh_saved(owner, guild_id, user["bot_profile"], user["tier"], code, kind,
                                                   reconnect_if_expired=True)
                except game_account.AccountError as exc:
                    error = str(exc)
            else:
                error = "Снимок младше минуты. Показаны уже полученные данные."
    return await asyncio.to_thread(_render, request, user, kind, plan, code, error)


@router.post("/game-account/events/{kind}/watch")
async def watch_action(request: Request, kind: str, plan: int | None = None, csrf: str = Form(...),
                       action: str = Form(...), interval: int = Form(300), confirm_login: bool = Form(False),
                       user: dict = Depends(require_officer_access)):
    _check_csrf(request, csrf)
    code = await asyncio.to_thread(_scope, user, kind)
    await asyncio.to_thread(_check_plan, user, kind, plan)
    owner, guild_id = str(user["discord_id"]), user["guild_id"]
    if action == "stop":
        game_watch.stop(guild_id, owner, kind)
    elif action == "start" and confirm_login and interval in game_watch.INTERVALS:
        await game_watch.start(owner, guild_id, user["bot_profile"], user["tier"], code, kind, interval)
    else:
        raise HTTPException(400, "Проверьте интервал и подтвердите новый вход в игру.")
    return RedirectResponse(f"/game-account/events/{kind}" + (f"?plan={plan}" if plan is not None else ""), status_code=303)


@router.get("/game-account/panel/{ticket}")
def open_panel(ticket: str, user: dict = Depends(require_officer_access)):
    guild_id = user.get("guild_id")
    if not event_panels.allowed(guild_id, user.get("tier"), user.get("bot_profile")):
        raise HTTPException(403, "Доступно офицерам АС в тестовой версии.")
    url = event_panels.panel_link(ticket, guild_id, str(user["discord_id"]))
    if not url:
        return PlainTextResponse(
            "Ссылка устарела или вы вошли под другим аккаунтом. Нажмите кнопку в актуальной сводке.",
            status_code=403, headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"},
        )
    return RedirectResponse(url, status_code=303, headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"})
