from collections import Counter
from pathlib import Path

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

import database
from services import dashboard_data
from web.deps import require_guild_access

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent.parent / "templates"))


@router.get("/roster", response_class=HTMLResponse)
async def roster(request: Request, user: dict = Depends(require_guild_access)):
    rows = dashboard_data.get_roster(user["guild_id"])
    registered_count = sum(1 for r in rows if r.registered)
    registered_pct = round(registered_count / len(rows) * 100) if rows else 0
    return templates.TemplateResponse(request, "roster.html", {
        "user": user,
        "rows": rows,
        "registered_count": registered_count,
        "registered_pct": registered_pct,
    })


@router.get("/tb", response_class=HTMLResponse)
async def tb_report(request: Request, user: dict = Depends(require_guild_access)):
    report = dashboard_data.get_tb_report(user["guild_id"])
    max_summary = report.latest[0].summary if report and report.latest else 0
    max_trend_total = max((t for _, t in report.event_totals), default=0) if report else 0
    return templates.TemplateResponse(request, "tb_report.html", {
        "user": user,
        "report": report,
        "max_summary": max_summary,
        "max_trend_total": max_trend_total,
    })


@router.get("/activity", response_class=HTMLResponse)
async def activity(request: Request, user: dict = Depends(require_guild_access)):
    player_filter = request.query_params.get("player") or None
    rows = dashboard_data.get_guild_activity(user["guild_id"], ally_code=player_filter)
    players = dashboard_data.get_guild_activity_players(user["guild_id"])
    guild_cfg = database.get_guild_config(user["guild_id"])

    breakdown = Counter(r.action_label for r in rows)
    breakdown_rows = sorted(breakdown.items(), key=lambda kv: kv[1], reverse=True)
    max_breakdown = breakdown_rows[0][1] if breakdown_rows else 0

    return templates.TemplateResponse(request, "activity.html", {
        "user": user,
        "rows": rows,
        "players": players,
        "player_filter": player_filter,
        "gg_configured": bool(guild_cfg and guild_cfg.get("swgoh_gg_guild_id")),
        "breakdown_rows": breakdown_rows,
        "max_breakdown": max_breakdown,
    })


@router.get("/violations", response_class=HTMLResponse)
async def violations(request: Request, user: dict = Depends(require_guild_access)):
    show_all = request.query_params.get("all") == "1"
    rows = dashboard_data.get_violations_overview(user["guild_id"], include_zero=show_all)
    top_offenders = [r for r in rows if r.recent_total > 0][:8]
    max_recent = top_offenders[0].recent_total if top_offenders else 0
    return templates.TemplateResponse(request, "violations.html", {
        "user": user,
        "rows": rows,
        "show_all": show_all,
        "n_limit": dashboard_data.N_LIMIT,
        "top_offenders": top_offenders,
        "max_recent": max_recent,
    })
