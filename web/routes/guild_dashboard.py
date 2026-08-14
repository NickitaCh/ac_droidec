from pathlib import Path

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

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
