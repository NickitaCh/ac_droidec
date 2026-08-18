from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

import database
from cogs.violations import WARNS_STRUCTURE
from services import dashboard_data
from web.deps import require_guild_access

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent.parent / "templates"))

# Подписи веток — намеренно те же самые, что в choices Discord-команды тб_отчет план
# (cogs/guild_events.py::tb_set_plan) для консистентности офицерского UX между ботом и
# вебом. Не путать с TB_CONFLICT_LABELS (cogs/guild_events.py) — тот словарь используется
# только как fallback-подпись в отчётах на чтение и расходится с этими ("01"->Dark там,
# "01"->Light здесь) — это существовавшее расхождение в самом боте, не веб-баг.
TB_PLAN_CONFLICT_OPTIONS = [
    {"value": "01", "label": "Light (красная)"},
    {"value": "02", "label": "Dark (жёлтая)"},
    {"value": "03", "label": "Mixed (синяя)"},
    {"value": "bonus", "label": "Бонус/ОЗ-зона"},
]


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


@router.get("/tb/player/{name}", response_class=HTMLResponse)
async def tb_player(name: str, request: Request, user: dict = Depends(require_guild_access)):
    report = dashboard_data.get_tb_player_report(user["guild_id"], name)
    if report is None:
        raise HTTPException(status_code=404, detail=f"Нет сохранённых данных ТБ для игрока «{name}»")
    return templates.TemplateResponse(request, "tb_player.html", {"user": user, "report": report})


@router.get("/tb/compare/{name}", response_class=HTMLResponse)
async def tb_compare_player(name: str, request: Request, user: dict = Depends(require_guild_access)):
    compare = dashboard_data.get_tb_player_compare(user["guild_id"], name)
    if compare is None:
        raise HTTPException(status_code=404, detail=f"Нет сохранённых данных ТБ для игрока «{name}»")
    return templates.TemplateResponse(request, "tb_compare_player.html", {"user": user, "compare": compare})


@router.get("/tb/plan", response_class=HTMLResponse)
async def tb_plan(request: Request, user: dict = Depends(require_guild_access)):
    current = database.get_tb_planet_names(user["guild_id"])
    conflict_label_map = {opt["value"]: opt["label"] for opt in TB_PLAN_CONFLICT_OPTIONS}
    rows = [
        {
            "phase": phase,
            "conflict_key": conflict_key,
            "conflict_label": conflict_label_map.get(conflict_key, conflict_key),
            "planet_name": planet_name,
        }
        for (phase, conflict_key), planet_name in sorted(current.items())
    ]
    return templates.TemplateResponse(request, "tb_plan.html", {
        "user": user,
        "rows": rows,
        "conflict_options": TB_PLAN_CONFLICT_OPTIONS,
        "error": request.query_params.get("error"),
        "saved": request.query_params.get("saved"),
    })


@router.post("/tb/plan", response_class=HTMLResponse)
async def tb_plan_save(
    phase: int = Form(...),
    conflict_key: str = Form(...),
    planet_name: str = Form(...),
    user: dict = Depends(require_guild_access),
):
    planet_name = planet_name.strip()
    valid_conflicts = {opt["value"] for opt in TB_PLAN_CONFLICT_OPTIONS}
    if not planet_name:
        return RedirectResponse(f"/tb/plan?{urlencode({'error': 'Название планеты не может быть пустым'})}", status_code=303)
    if not (1 <= phase <= 6):
        return RedirectResponse(f"/tb/plan?{urlencode({'error': 'Некорректный номер этапа (1-6)'})}", status_code=303)
    if conflict_key not in valid_conflicts:
        return RedirectResponse(f"/tb/plan?{urlencode({'error': 'Некорректная ветка'})}", status_code=303)
    database.set_tb_planet_name(str(phase), conflict_key, planet_name, source="manual", guild_id=user["guild_id"])
    return RedirectResponse("/tb/plan?saved=1", status_code=303)


@router.get("/activity", response_class=HTMLResponse)
async def activity(request: Request, user: dict = Depends(require_guild_access)):
    player_filter = request.query_params.get("player") or None
    rows = dashboard_data.get_guild_activity(user["guild_id"], ally_code=player_filter)
    players = dashboard_data.get_guild_activity_players(user["guild_id"])

    breakdown = Counter(r.action_label for r in rows)
    breakdown_rows = sorted(breakdown.items(), key=lambda kv: kv[1], reverse=True)
    max_breakdown = breakdown_rows[0][1] if breakdown_rows else 0

    return templates.TemplateResponse(request, "activity.html", {
        "user": user,
        "rows": rows,
        "players": players,
        "player_filter": player_filter,
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
        "warns_structure": WARNS_STRUCTURE,
        "error": request.query_params.get("error"),
    })


@router.get("/violations/api/players", response_class=JSONResponse)
async def violations_players_search(q: str = "", user: dict = Depends(require_guild_access)):
    if not q or len(q.strip()) < 2:
        return []
    q_lower = q.strip().lower()
    mappings = database.get_all_user_mappings(user["guild_id"])
    matches = [
        {"ally_code": ally_code, "name": name}
        for _discord_id, ally_code, name in mappings
        if name and q_lower in name.lower()
    ]
    matches.sort(key=lambda m: m["name"].lower())
    return matches[:20]


@router.post("/violations/add", response_class=HTMLResponse)
async def violation_add(
    ally_code: str = Form(...),
    category: str = Form(...),
    violation_1: str = Form(""),
    comment_1: str = Form(""),
    violation_2: str = Form(""),
    comment_2: str = Form(""),
    violation_3: str = Form(""),
    comment_3: str = Form(""),
    date: str = Form(""),
    user: dict = Depends(require_guild_access),
):
    guild_id = user["guild_id"]
    ally_code = ally_code.strip()

    if category not in WARNS_STRUCTURE:
        return RedirectResponse(f"/violations?{urlencode({'error': 'Некорректный режим (ТБ/ВГ/Рейд).'})}", status_code=303)
    if not ally_code:
        return RedirectResponse(f"/violations?{urlencode({'error': 'Выберите игрока из подсказок.'})}", status_code=303)

    if date:
        try:
            date_str = datetime.strptime(date, "%Y-%m-%d").strftime("%d.%m.%Y")
        except ValueError:
            return RedirectResponse(f"/violations?{urlencode({'error': 'Некорректная дата.'})}", status_code=303)
    else:
        date_str = datetime.now().strftime("%d.%m.%Y")

    # Дедуп одинаковых пунктов в одном сабмите — та же логика, что /нарушение (cogs/violations.py).
    pairs = [
        (violation_1, comment_1.strip() or None),
        (violation_2, comment_2.strip() or None),
        (violation_3, comment_3.strip() or None),
    ]
    seen = set()
    unique_pairs = []
    for v, c in pairs:
        if v and v in WARNS_STRUCTURE[category] and v not in seen:
            seen.add(v)
            unique_pairs.append((v, c))

    if not unique_pairs:
        return RedirectResponse(f"/violations?{urlencode({'error': 'Выберите хотя бы одно корректное нарушение.'})}", status_code=303)

    for v, c in unique_pairs:
        database.add_warn(ally_code, category, v, date_str, c, guild_id=guild_id)

    return RedirectResponse(f"/violations/{ally_code}", status_code=303)


@router.get("/violations/{ally_code}", response_class=HTMLResponse)
async def violation_dossier(request: Request, ally_code: str, user: dict = Depends(require_guild_access)):
    guild_id = user["guild_id"]
    names_by_code = {code: name for _discord_id, code, name in database.get_all_user_mappings(guild_id)}
    player_name = names_by_code.get(ally_code, ally_code)

    rows = database.get_player_warns(ally_code, guild_id=guild_id)
    three_months_ago = datetime.now() - timedelta(days=90)
    entries = []
    recent_count = 0
    for warn_id, category, subcategory, date_str, comment in rows:
        is_recent = False
        try:
            is_recent = datetime.strptime(date_str, "%d.%m.%Y") >= three_months_ago
        except ValueError:
            pass
        if is_recent:
            recent_count += 1
        entries.append({
            "id": warn_id, "category": category, "subcategory": subcategory,
            "date_str": date_str, "comment": comment, "recent": is_recent,
        })

    return templates.TemplateResponse(request, "violation_dossier.html", {
        "user": user,
        "ally_code": ally_code,
        "player_name": player_name,
        "entries": entries,
        "recent_count": recent_count,
        "lifetime_count": len(entries),
        "n_limit": dashboard_data.N_LIMIT,
        "error": request.query_params.get("error"),
    })


@router.post("/violations/{warn_id}/delete", response_class=HTMLResponse)
async def violation_delete(
    warn_id: int,
    ally_code: str = Form(...),
    user: dict = Depends(require_guild_access),
):
    database.remove_warn_by_id(warn_id, guild_id=user["guild_id"])
    return RedirectResponse(f"/violations/{ally_code}", status_code=303)
