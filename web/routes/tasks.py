"""Веб-версия /task_add + /sync_units. Аудит дедлайнов и авто-завершение
(cogs/tasks.py::tasks_audit_loop) остаётся только в боте — веб лишь создаёт
задачи и показывает уже проставленный ботом статус (ACTIVE/COMPLETED/FAILED),
плюс кнопка ручного sync_units (services/units_sync.py)."""

from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

import database
from services.units_sync import sync_units
from web.deps import require_officer_access

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent.parent / "templates"))

TARGET_TYPE_OPTIONS = [
    ("stars", "⭐ Звёзды (1-7)"),
    ("relic", "♦️ Реликвия (0-9)"),
    ("omicron", "🧬 Омикрон"),
]
TARGET_TYPE_LABELS = dict(TARGET_TYPE_OPTIONS)

STATUS_LABELS = {"ACTIVE": "В работе", "COMPLETED": "Выполнено", "FAILED": "Провалено"}
STATUS_BADGE = {"ACTIVE": "badge-neutral", "COMPLETED": "badge-ok", "FAILED": "badge-danger"}


def _get_comlink():
    # См. web/routes/registration.py::_get_comlink — веб-процесс не поднимает
    # диска-клиента, строит свой SwgohComlink на тот же сайдкар.
    from swgoh_comlink import SwgohComlink
    return SwgohComlink(url="http://localhost:3000")


@router.get("", response_class=HTMLResponse)
async def tasks_list(request: Request, user: dict = Depends(require_officer_access)):
    guild_id = user["guild_id"]
    status_filter = request.query_params.get("status")

    rows = database.get_all_tasks(guild_id)
    if status_filter:
        rows = [r for r in rows if r[6] == status_filter]

    names_by_code = {code: name for _, code, name in database.get_all_user_mappings(guild_id)}
    unit_names = database.get_game_unit_names([r[2] for r in rows])

    tasks = [
        {
            "task_id": task_id,
            "ally_code": ally_code,
            "player_name": names_by_code.get(ally_code, ally_code),
            "unit_name": unit_names.get(base_id) or base_id,
            "target_label": TARGET_TYPE_LABELS.get(target_type, target_type),
            "target_value": target_value,
            "deadline": deadline,
            "status": status,
            "status_label": STATUS_LABELS.get(status, status),
            "status_badge": STATUS_BADGE.get(status, "badge-neutral"),
        }
        for task_id, ally_code, base_id, target_type, target_value, deadline, status in rows
    ]

    return templates.TemplateResponse(request, "tasks.html", {
        "user": user,
        "tasks": tasks,
        "status_filter": status_filter,
        "target_type_options": TARGET_TYPE_OPTIONS,
        "error": request.query_params.get("error"),
        "synced": request.query_params.get("synced"),
    })


@router.get("/api/players", response_class=JSONResponse)
async def players_search(q: str = "", user: dict = Depends(require_officer_access)):
    if not q or len(q.strip()) < 2:
        return []
    q_lower = q.strip().lower()
    rows = database.get_all_user_mappings(user["guild_id"])
    matches = [
        {"ally_code": ally_code, "name": name}
        for _, ally_code, name in rows
        if name and q_lower in name.lower()
    ]
    matches.sort(key=lambda m: m["name"].lower())
    return matches[:20]


@router.get("/api/units", response_class=JSONResponse)
async def units_search(q: str = "", user: dict = Depends(require_officer_access)):
    if not q or len(q.strip()) < 2:
        return []
    rows = database.search_game_units(q.strip(), limit=20)
    return [{"base_id": base_id, "name": name} for base_id, name in rows]


@router.post("/add", response_class=HTMLResponse)
async def task_add(
    ally_code: str = Form(...),
    base_id: str = Form(...),
    target_type: str = Form(...),
    target_value: str = Form(...),
    days_to_complete: int = Form(..., ge=1, le=365),
    user: dict = Depends(require_officer_access),
):
    guild_id = user["guild_id"]

    if target_type not in TARGET_TYPE_LABELS:
        return RedirectResponse(f"/tasks?{urlencode({'error': 'Некорректный тип цели.'})}", status_code=303)

    unit_name = database.get_game_unit_name(base_id)
    if not unit_name:
        return RedirectResponse(f"/tasks?{urlencode({'error': f'Юнит {base_id} не найден в справочнике.'})}", status_code=303)

    names_by_code = {code: name for _, code, name in database.get_all_user_mappings(guild_id)}
    if ally_code not in names_by_code:
        return RedirectResponse(f"/tasks?{urlencode({'error': 'Игрок не найден в составе гильдии — выберите из подсказок.'})}", status_code=303)

    deadline_date = (datetime.now() + timedelta(days=days_to_complete)).strftime("%Y-%m-%d")
    database.add_task(ally_code, base_id, target_type, target_value.strip(), deadline_date, str(user["discord_id"]), guild_id=guild_id)
    return RedirectResponse("/tasks", status_code=303)


@router.post("/sync-units", response_class=HTMLResponse)
async def sync_units_now(user: dict = Depends(require_officer_access)):
    comlink = _get_comlink()
    try:
        total = await sync_units(comlink)
    except Exception as e:
        return RedirectResponse(f"/tasks?{urlencode({'error': f'Ошибка синхронизации: {e}'})}", status_code=303)
    return RedirectResponse(f"/tasks?{urlencode({'synced': str(total)})}", status_code=303)
