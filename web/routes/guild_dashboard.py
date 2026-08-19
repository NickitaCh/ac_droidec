import asyncio
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

import database
from cogs.violations import WARNS_STRUCTURE
from services import activity_diff, dashboard_data
from web.deps import require_guild_access

MSK = ZoneInfo("Europe/Moscow")


def _get_comlink():
    # См. web/routes/admin.py::_get_comlink / registration.py::_get_comlink — веб-процесс
    # не поднимает бота, строит свой SwgohComlink на тот же comlink-сайдкар.
    from swgoh_comlink import SwgohComlink
    return SwgohComlink(url="http://localhost:3000")


def _valid_iso_date(value: str | None) -> str | None:
    if not value:
        return None
    try:
        datetime.strptime(value, "%Y-%m-%d")
        return value
    except ValueError:
        return None


def _format_delta(delta: timedelta) -> str:
    total_minutes = round(delta.total_seconds() / 60)
    hours, minutes = divmod(abs(total_minutes), 60)
    parts = []
    if hours:
        parts.append(f"{hours} ч")
    parts.append(f"{minutes} мин")
    return " ".join(parts)


def _hours_label(hours: int) -> str:
    """Склонение "час"/"часа"/"часов" под PLAYER_STATS_SYNC_HOURS — значение конфигурируемое
    (сейчас 1), поэтому текст на /activity не может просто хардкодить "часов"."""
    n = abs(hours) % 100
    if 11 <= n <= 14:
        word = "часов"
    elif n % 10 == 1:
        word = "час"
    elif 2 <= n % 10 <= 4:
        word = "часа"
    else:
        word = "часов"
    return f"{hours} {word}"


def _sync_status_text(sync_status: dict) -> dict:
    """(last_sync, next_auto, sync_hours) -> человекочитаемые строки для панели на /activity."""
    last_sync_text = None
    if sync_status["last_sync"]:
        last_sync_text = sync_status["last_sync"].strftime("%d.%m.%Y %H:%M (МСК)")

    next_auto_text = None
    next_auto = sync_status["next_auto"]
    if next_auto:
        now = datetime.now(MSK)
        if next_auto <= now:
            next_auto_text = "вот-вот"
        else:
            next_auto_text = f"через {_format_delta(next_auto - now)}"

    return {
        "last_sync_text": last_sync_text,
        "next_auto_text": next_auto_text,
        "sync_hours_text": _hours_label(sync_status["sync_hours"]),
    }

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
    date_from = _valid_iso_date(request.query_params.get("date_from"))
    date_to = _valid_iso_date(request.query_params.get("date_to"))
    rows = dashboard_data.get_guild_activity(user["guild_id"], ally_code=player_filter,
                                              date_from=date_from, date_to=date_to)
    players = dashboard_data.get_guild_activity_players(user["guild_id"])
    grouped = dashboard_data.group_activity(rows)
    sync_status = dashboard_data.get_activity_sync_status(user["guild_id"])
    sync_status_text = _sync_status_text(sync_status)

    breakdown = Counter(r.action_label for r in rows)
    breakdown_rows = sorted(breakdown.items(), key=lambda kv: kv[1], reverse=True)
    max_breakdown = breakdown_rows[0][1] if breakdown_rows else 0

    return templates.TemplateResponse(request, "activity.html", {
        "user": user,
        "rows": rows,
        "grouped": grouped,
        "players": players,
        "player_filter": player_filter,
        "date_from": date_from or "",
        "date_to": date_to or "",
        "breakdown_rows": breakdown_rows,
        "max_breakdown": max_breakdown,
        "sync_status": sync_status_text,
        "synced_now": request.query_params.get("synced"),
    })


@router.post("/activity/sync", response_class=HTMLResponse)
async def activity_sync(
    request: Request,
    player: str = Form(""),
    date_from: str = Form(""),
    date_to: str = Form(""),
    user: dict = Depends(require_guild_access),
):
    # Ограниченная параллельность (semaphore) + таймаут внутри sync_player — тот же паттерн,
    # что и в web/routes/datacrons.py::_build_guild_report, чтобы HTTP-запрос от кнопки
    # "Обновить сейчас" не висел последовательно по 15с на каждого из ~50 игроков гильдии.
    guild_id = user["guild_id"]
    ally_codes = [code for _, code, _ in database.get_all_user_mappings(guild_id)]
    comlink = _get_comlink()
    today = datetime.now(MSK).date().isoformat()
    semaphore = asyncio.Semaphore(6)

    async def sync_one(ally_code):
        async with semaphore:
            try:
                # guild_ids не {guild_id} — игрок теоретически состоит сразу в нескольких
                # зарегистрированных гильдиях; player_unit_cache общий на всех, и если
                # обновить его тут только под текущую гильдию, автоцикл бота позже не
                # найдёт разницы и "чужая" гильдия потеряет эти события безвозвратно.
                guild_ids = database.get_guild_ids_for_ally_code(ally_code) or {guild_id}
                # Объявления в Discord из omicron_hits тут не постим — у веб-процесса нет
                # Discord-клиента, см. services/activity_diff.py::sync_player.
                _, added, _ = await activity_diff.sync_player(comlink, ally_code, guild_ids, today)
                return added
            except Exception as e:
                print(f"⚠️ [/activity/sync] Не удалось обновить ростер {ally_code}: {e}")
                return 0

    results = await asyncio.gather(*(sync_one(ac) for ac in ally_codes))
    params = {"synced": sum(results)}
    if _valid_iso_date(date_from):
        params["date_from"] = date_from
    if _valid_iso_date(date_to):
        params["date_to"] = date_to
    if player:
        params["player"] = player
    return RedirectResponse(f"/activity?{urlencode(params)}", status_code=303)


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
