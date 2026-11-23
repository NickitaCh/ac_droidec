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
from services import event_panels, game_account, game_account_readonly, game_event_live, game_watch, tw_battle_log, tw_def_notify, tw_score
from services import tw_game_deploy as defense_deploy
import tw_def_engine
from web.deps import require_officer_access, selected_roster_account

router = APIRouter()
from web.routes import tw_game_deploy
router.include_router(tw_game_deploy.router)
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parents[1] / "templates"))
_TITLES = {"tw": "Анализ ВГ", "raid": "Рейд сейчас"}
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
    cached = (game_event_live.latest_tw_snapshot(guild_id) if kind == "tw"
              else game_event_live.snapshot(guild_id, owner, kind))
    plan, preview = None, []
    personal_code = (selected_roster_account(user) if user.get("active_ally_code") else code) if kind == "tw" else None
    if kind == "tw" and plan_id:
        plan = database.get_tw_def_plan(user["guild_id"], plan_id)
        if not plan:
            raise HTTPException(404, "План ВГ не найден.")
        owned = database.get_player_units(personal_code) if personal_code else {}
        preview = game_event_live.tw_preview(
            plan, database.get_tw_def_assignments(user["guild_id"], plan_id), personal_code,
            set(owned) if owned else None, tw_def_engine.ZONES_BY_KEY, tw_def_engine.zone_capacity,
        )
        unit_ids = sorted({unit for row in preview for unit in row["units"]})
        names = database.get_game_unit_names(unit_ids) if unit_ids else {}
        owned_datacrons = {}
        if any(row["datacron_ids"] for row in preview):
            owned_datacrons = {
                str(dc["id"]): dc for dc in database.get_player_datacrons_bulk([personal_code]).get(personal_code, [])
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
    battle_log = tw_battle_log.latest(guild_id) if kind == "tw" else {}
    if battle_log and cached.get("at", 0) >= battle_log["at"] and (
        (cached.get("data") or {}).get("event_id") != battle_log.get("event_id")
    ):
        battle_log = {}
    battle_zones = tw_battle_log.zone_options(battle_log) if kind == "tw" else {}
    battle_zone = tw_battle_log.selected_zone(battle_log, battle_zones, request.query_params.get("battle_zone"))
    request.session.setdefault("game_event_csrf", secrets.token_urlsafe(32))
    return templates.TemplateResponse(request, "game_event_live.html", {
        "user": user, "kind": kind, "title": _TITLES[kind],
        "csrf": request.session["game_event_csrf"], "cached": cached, "clock": _clock,
        "roster": roster, "preview": preview, "plan": plan,
        "personal_code": personal_code,
        "personal_connected": bool(personal_code and game_account_readonly._connection(owner, personal_code)),
        "defense_plans": database.list_tw_def_plans(guild_id) if kind == "tw" else [],
        "deployment_open": kind == "tw" and _deployment_open(cached.get("data")),
        "battle_log": battle_log, "battle_rows": tw_battle_log.display_rows(battle_log, zone=battle_zone),
        "battle_zones": battle_zones, "battle_zone": battle_zone,
        "attack_analysis": tw_score.analysis(cached.get("data")) if kind == "tw" else [],
        "error": error, "watch": game_watch.get(guild_id, owner, kind), "watch_intervals": game_watch.INTERVALS,
        "game_connected": bool(game_account_readonly._connection(owner, code)),
    }, headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"})


@router.get("/game-account/events/{kind}", response_class=HTMLResponse)
def page(request: Request, kind: str, plan: int | None = None, user: dict = Depends(require_officer_access)):
    code = _scope(user, kind)
    _check_plan(user, kind, plan)
    return _render(request, user, kind, plan, code)


def _deployment_open(data):
    return bool(data and data.get("event_id") and data.get("round") == 0
                and game_event_live.epoch(data.get("end") or 0) > time.time())


@router.post("/game-account/events/tw/defense")
def create_defense(request: Request, plan: int = Form(...), csrf: str = Form(...),
                   ally_code: str = Form(""),
                   user: dict = Depends(require_officer_access)):
    _check_csrf(request, csrf)
    code = _scope(user, "tw")
    if ally_code or user.get("active_ally_code"):
        code = selected_roster_account(user, ally_code)
        if not code:
            raise HTTPException(403, "Выберите свой аккаунт из состава этой гильдии.")
    guild_id = user["guild_id"]
    selected_plan = database.get_tw_def_plan(guild_id, plan)
    if not selected_plan:
        raise HTTPException(404)
    state = game_event_live.latest_tw_snapshot(guild_id).get("data")
    if not _deployment_open(state):
        return _render(request, user, "tw", plan, code, "Обновите данные ВГ во время расстановки.")
    assignments = database.get_tw_def_assignments(guild_id, plan)
    preview = game_event_live.tw_preview(selected_plan, assignments, code, None,
                                         tw_def_engine.ZONES_BY_KEY, tw_def_engine.zone_capacity)
    if any(row["issues"] for row in preview):
        return _render(request, user, "tw", plan, code, "Исправьте ошибки в назначениях.")
    rows = []
    for row in assignments:
        if str(row["ally_code"]) == str(code):
            datacron = (row.get("datacron_id") or "").split(",")[0]
            rows.append({"zone": row["zone"], "name": row["squad_name"],
                         "units": [unit for unit in row["units"] if unit], "datacron": datacron})
    try:
        ticket = defense_deploy.create(user, code, state["event_id"], rows)
    except game_account.AccountError as error:
        return _render(request, user, "tw", plan, code, str(error))
    return RedirectResponse(f"/game-account/tw-defense/{ticket}", status_code=303)


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
