"""«Прогресс модинга» (/mod-progress, только офицеры) — трекеры по плейтам, сводка по
гильдии и таблица игрока «требование × дата снимка». Логика — services/mod_progress.py,
автоснимки — cogs/mod_progress.py. Ручной снимок идёт в фоне веб-процесса (на всю гильдию
это десятки секунд), страница показывает «снимок делается»."""

import asyncio
from pathlib import Path

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

import database
import services.mod_progress as mod_progress
import services.stat_forecast as stat_forecast
from web.deps import require_officer_access

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent.parent / "templates"))

# tracker_id → фоновая задача ручного снимка (в этом процессе).
_running: dict[int, asyncio.Task] = {}
_errors: dict[int, str] = {}


def _get_comlink():
    from swgoh_comlink import SwgohComlink
    return SwgohComlink(url="http://localhost:3000")


def _is_running(tracker_id: int) -> bool:
    task = _running.get(tracker_id)
    return task is not None and not task.done()


async def _snapshot_job(tracker_id: int, taken_by: str):
    try:
        comlink = _get_comlink()
        stat_calc = await stat_forecast.get_stat_calc(comlink)
        await mod_progress.take_snapshot(stat_forecast._bot_stand_in(comlink, stat_calc), tracker_id, "manual", taken_by)
        _errors.pop(tracker_id, None)
    except Exception as e:
        _errors[tracker_id] = str(e) or e.__class__.__name__
        print(f"⚠️ [web] снимок прогресса модинга {tracker_id} не удался: {_errors[tracker_id]}")


def _start_snapshot(tracker_id: int, user: dict):
    if not _is_running(tracker_id):
        _running[tracker_id] = asyncio.create_task(_snapshot_job(tracker_id, user.get("username") or str(user.get("discord_id"))))


def _tracker_or_404(guild_id: int, tracker_id: int) -> dict:
    tracker = mod_progress.get_tracker(guild_id, tracker_id)
    if not tracker:
        raise HTTPException(status_code=404, detail="Трекер не найден")
    return tracker


@router.get("", response_class=HTMLResponse)
async def mod_progress_list(request: Request, user: dict = Depends(require_officer_access)):
    guild_id = user["guild_id"]
    trackers = mod_progress.list_trackers(guild_id)
    for t in trackers:
        t["running"] = _is_running(t["id"])
    return templates.TemplateResponse(request, "mod_progress.html", {
        "user": user, "trackers": trackers, "periods": mod_progress.PERIODS,
        "plates": database.get_all_stat_requirement_plates(guild_id),
        "error": request.query_params.get("error"),
    })


@router.post("/create")
async def mod_progress_create(request: Request, plate: str = Form(...), period_days: int = Form(0),
                              user: dict = Depends(require_officer_access)):
    tracker_id = mod_progress.create_tracker(user["guild_id"], plate, period_days,
                                             user.get("username") or str(user.get("discord_id")))
    if tracker_id is None:
        return RedirectResponse("/mod-progress?error=empty", status_code=303)
    _start_snapshot(tracker_id, user)  # первый снимок — сразу при создании
    return RedirectResponse(f"/mod-progress/{tracker_id}", status_code=303)


@router.get("/{tracker_id}", response_class=HTMLResponse)
async def mod_progress_tracker(request: Request, tracker_id: int, user: dict = Depends(require_officer_access)):
    scenario = mod_progress.scenario_or_default(request.query_params.get("scenario"))
    summary = mod_progress.guild_summary(user["guild_id"], tracker_id, scenario)
    if summary is None:
        raise HTTPException(status_code=404, detail="Трекер не найден")
    current = mod_progress.plate_requirements(summary["tracker"]["plate_name"], user["guild_id"])
    latest = mod_progress.sync_version(tracker_id)
    return templates.TemplateResponse(request, "mod_progress_tracker.html", {
        "user": user, **summary, "periods": mod_progress.PERIODS, "scenarios": mod_progress.SCENARIOS,
        "running": _is_running(tracker_id), "job_error": _errors.get(tracker_id),
        "plate_missing": not current, "latest_version": latest,
    })


@router.post("/{tracker_id}/snapshot")
async def mod_progress_snapshot(tracker_id: int, user: dict = Depends(require_officer_access)):
    _tracker_or_404(user["guild_id"], tracker_id)
    _start_snapshot(tracker_id, user)
    return RedirectResponse(f"/mod-progress/{tracker_id}", status_code=303)


@router.post("/{tracker_id}/period")
async def mod_progress_period(tracker_id: int, period_days: int = Form(...), user: dict = Depends(require_officer_access)):
    _tracker_or_404(user["guild_id"], tracker_id)
    mod_progress.set_period(user["guild_id"], tracker_id, period_days)
    return RedirectResponse(f"/mod-progress/{tracker_id}", status_code=303)


@router.post("/{tracker_id}/delete")
async def mod_progress_delete(tracker_id: int, user: dict = Depends(require_officer_access)):
    mod_progress.delete_tracker(user["guild_id"], tracker_id)
    return RedirectResponse("/mod-progress", status_code=303)


@router.post("/{tracker_id}/snapshot/{snapshot_id}/delete")
async def mod_progress_snapshot_delete(tracker_id: int, snapshot_id: int, user: dict = Depends(require_officer_access)):
    mod_progress.delete_snapshot(user["guild_id"], tracker_id, snapshot_id)
    return RedirectResponse(f"/mod-progress/{tracker_id}", status_code=303)


@router.get("/{tracker_id}/player/{ally_code}", response_class=HTMLResponse)
async def mod_progress_player(request: Request, tracker_id: int, ally_code: str, user: dict = Depends(require_officer_access)):
    show_all = request.query_params.get("all") == "1"
    scenario = mod_progress.scenario_or_default(request.query_params.get("scenario"))
    table = mod_progress.player_table(user["guild_id"], tracker_id, ally_code, limit=None if show_all else 6,
                                      scenario=scenario)
    if table is None:
        raise HTTPException(status_code=404, detail="Трекер не найден")
    return templates.TemplateResponse(request, "mod_progress_player.html", {"user": user, **table, "show_all": show_all,
                                                                          "scenarios": mod_progress.SCENARIOS})
