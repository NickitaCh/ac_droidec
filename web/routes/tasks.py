"""Веб-управление заданиями на прокачку (аналог /задания в Discord) — список с
фильтром/группировкой по игроку, точечная и массовая постановка, редактирование
(значение цели + дедлайн) и удаление (по одному и целой группой), плюс предзаполнение
формы из гильдийского отчёта по плейту (кнопка "+ задача" на /stats-check).
Аудит выполнения, уведомления и напоминания остаются только в боте
(cogs/tasks.py::tasks_audit_loop) — веб лишь читает уже проставленный ботом статус."""

import uuid
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


def _omicron_options(base_id: str) -> list:
    skill_ids = database.get_all_unit_omicron_skills().get(base_id, [])
    if not skill_ids:
        return []
    info = database.get_skill_display_info(skill_ids)
    options = []
    for skill_id in skill_ids:
        name, _ability_id, ability_type, _omicron_mode = info.get(skill_id, (skill_id, None, None, None))
        label = f"{name or skill_id}" + (f" ({ability_type})" if ability_type else "")
        options.append({"skill_id": skill_id, "label": label})
    return options


def _target_label(target_type: str, target_value: str) -> str:
    if target_type == "stars":
        return f"⭐ Звёзды {target_value}"
    if target_type == "relic":
        return f"♦️ Реликвия {target_value}"
    if target_type == "omicron":
        info = database.get_skill_display_info([target_value]).get(target_value)
        name = info[0] if info and info[0] else target_value
        return f"🧬 Омикрон: {name}"
    return target_value


def _validate_target(base_id: str, target_type: str, target_value: str) -> str | None:
    """None — ок, иначе текст ошибки для показа пользователю."""
    if target_type not in TARGET_TYPE_LABELS:
        return "Некорректный тип цели."
    target_value = target_value.strip()
    if not target_value:
        return "Не указано целевое значение."
    if target_type == "omicron":
        valid_skills = database.get_all_unit_omicron_skills().get(base_id, [])
        if target_value not in valid_skills:
            return "Для омикрона выберите конкретную способность из подсказки (не свободный текст)."
    elif not target_value.isdigit():
        return "Для звёзд/реликвии значение должно быть числом."
    return None


@router.get("", response_class=HTMLResponse)
async def tasks_list(request: Request, user: dict = Depends(require_officer_access)):
    guild_id = user["guild_id"]
    status_filter = request.query_params.get("status")
    view = request.query_params.get("view", "flat")

    rows = database.get_all_tasks(guild_id)
    names_by_code = {code: name for _, code, name in database.get_all_user_mappings(guild_id)}
    unit_names = database.get_game_unit_names([r[2] for r in rows])

    counts = {"ACTIVE": 0, "COMPLETED": 0, "FAILED": 0}
    for r in rows:
        counts[r[6]] = counts.get(r[6], 0) + 1

    filtered = [r for r in rows if not status_filter or r[6] == status_filter]

    tasks_ctx = [
        {
            "task_id": task_id, "ally_code": ally_code, "base_id": base_id,
            "player_name": names_by_code.get(ally_code, ally_code),
            "unit_name": unit_names.get(base_id) or base_id,
            "target_type": target_type, "target_value": target_value,
            "target_label": _target_label(target_type, target_value),
            "deadline": deadline, "status": status,
            "status_label": STATUS_LABELS.get(status, status),
            "status_badge": STATUS_BADGE.get(status, "badge-neutral"),
            "batch_id": batch_id,
        }
        for task_id, ally_code, base_id, target_type, target_value, deadline, status, batch_id in filtered
    ]

    # Омикрон-варианты для инлайн-редактирования — только для реально показанных задач
    # этого типа, не весь справочник разом.
    omicron_options_by_unit = {
        t["base_id"]: _omicron_options(t["base_id"])
        for t in tasks_ctx if t["target_type"] == "omicron"
    }

    # Активные группы (массовая постановка / из отчёта плейта) — для панели "отменить группу".
    batch_groups = {}
    for t in tasks_ctx:
        if t["batch_id"] and t["status"] == "ACTIVE":
            g = batch_groups.setdefault(t["batch_id"], {"count": 0, "players": []})
            g["count"] += 1
            g["players"].append(t["player_name"])
    batch_list = [{"batch_id": bid, **g} for bid, g in batch_groups.items()]

    players_view = []
    if view == "players":
        by_player = {}
        for t in tasks_ctx:
            if t["status"] in ("ACTIVE", "FAILED"):
                by_player.setdefault(t["ally_code"], {"name": t["player_name"], "tasks": []})["tasks"].append(t)
        players_view = sorted(by_player.values(), key=lambda p: (-len(p["tasks"]), p["name"].lower()))

    roster = sorted(names_by_code.items(), key=lambda kv: kv[1].lower())

    prefill = {
        "ally_code": request.query_params.get("prefill_ally", ""),
        "unit": request.query_params.get("prefill_unit", ""),
        "type": request.query_params.get("prefill_type", ""),
        "value": request.query_params.get("prefill_value", ""),
    }
    prefill["active"] = bool(prefill["ally_code"] or prefill["unit"])
    prefill["ally_name"] = names_by_code.get(prefill["ally_code"], prefill["ally_code"]) if prefill["ally_code"] else ""
    prefill["unit_name"] = (database.get_game_unit_name(prefill["unit"]) or prefill["unit"]) if prefill["unit"] else ""

    return templates.TemplateResponse(request, "tasks.html", {
        "user": user,
        "tasks": tasks_ctx,
        "status_filter": status_filter,
        "view": view,
        "counts": counts,
        "batch_list": batch_list,
        "players_view": players_view,
        "omicron_options_by_unit": omicron_options_by_unit,
        "target_type_options": TARGET_TYPE_OPTIONS,
        "roster": roster,
        "prefill": prefill,
        "error": request.query_params.get("error"),
        "notice": request.query_params.get("notice"),
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


@router.get("/api/omicron-skills", response_class=JSONResponse)
async def omicron_skills(base_id: str = "", user: dict = Depends(require_officer_access)):
    if not base_id:
        return []
    return _omicron_options(base_id)


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

    unit_name = database.get_game_unit_name(base_id)
    if not unit_name:
        return RedirectResponse(f"/tasks?{urlencode({'error': f'Юнит {base_id} не найден в справочнике.'})}", status_code=303)

    names_by_code = {code: name for _, code, name in database.get_all_user_mappings(guild_id)}
    if ally_code not in names_by_code:
        return RedirectResponse(f"/tasks?{urlencode({'error': 'Игрок не найден в составе гильдии — выберите из подсказок.'})}", status_code=303)

    err = _validate_target(base_id, target_type, target_value)
    if err:
        return RedirectResponse(f"/tasks?{urlencode({'error': err})}", status_code=303)

    deadline_date = (datetime.now() + timedelta(days=days_to_complete)).strftime("%Y-%m-%d")
    database.add_task(ally_code, base_id, target_type, target_value.strip(), deadline_date, str(user["discord_id"]), guild_id=guild_id)
    return RedirectResponse("/tasks", status_code=303)


@router.post("/add-bulk", response_class=HTMLResponse)
async def task_add_bulk(
    ally_codes: list = Form(default=[]),
    base_id: str = Form(...),
    target_type: str = Form(...),
    target_value: str = Form(...),
    days_to_complete: int = Form(..., ge=1, le=365),
    user: dict = Depends(require_officer_access),
):
    guild_id = user["guild_id"]

    unit_name = database.get_game_unit_name(base_id)
    if not unit_name:
        return RedirectResponse(f"/tasks?{urlencode({'error': f'Юнит {base_id} не найден в справочнике.'})}", status_code=303)

    names_by_code = {code: name for _, code, name in database.get_all_user_mappings(guild_id)}
    valid_codes = [c for c in ally_codes if c in names_by_code]
    if not valid_codes:
        return RedirectResponse(f"/tasks?{urlencode({'error': 'Не выбрано ни одного игрока из состава гильдии.'})}", status_code=303)

    err = _validate_target(base_id, target_type, target_value)
    if err:
        return RedirectResponse(f"/tasks?{urlencode({'error': err})}", status_code=303)

    deadline_date = (datetime.now() + timedelta(days=days_to_complete)).strftime("%Y-%m-%d")
    batch_id = uuid.uuid4().hex[:12]
    target_value = target_value.strip()
    for code in valid_codes:
        database.add_task(code, base_id, target_type, target_value, deadline_date, str(user["discord_id"]), guild_id=guild_id, batch_id=batch_id)

    return RedirectResponse(f"/tasks?{urlencode({'notice': f'Поставлено заданий: {len(valid_codes)}'})}", status_code=303)


@router.post("/{task_id}/edit", response_class=HTMLResponse)
async def task_edit(
    task_id: int,
    target_value: str = Form(...),
    deadline: str = Form(...),
    user: dict = Depends(require_officer_access),
):
    guild_id = user["guild_id"]
    task = database.get_task(task_id)
    if not task or task[7] != guild_id:
        return RedirectResponse(f"/tasks?{urlencode({'error': 'Задание не найдено.'})}", status_code=303)

    base_id, target_type = task[2], task[3]
    err = _validate_target(base_id, target_type, target_value)
    if err:
        return RedirectResponse(f"/tasks?{urlencode({'error': err})}", status_code=303)

    try:
        datetime.strptime(deadline, "%Y-%m-%d")
    except ValueError:
        return RedirectResponse(f"/tasks?{urlencode({'error': 'Некорректная дата дедлайна (ГГГГ-ММ-ДД).'})}", status_code=303)

    database.update_task(task_id, target_value=target_value.strip(), deadline=deadline)
    return RedirectResponse(f"/tasks?{urlencode({'notice': 'Задание обновлено'})}", status_code=303)


@router.post("/{task_id}/delete", response_class=HTMLResponse)
async def task_delete(task_id: int, user: dict = Depends(require_officer_access)):
    guild_id = user["guild_id"]
    task = database.get_task(task_id)
    if task and task[7] == guild_id:
        database.delete_task(task_id)
    return RedirectResponse(f"/tasks?{urlencode({'notice': 'Задание удалено'})}", status_code=303)


@router.post("/batch/{batch_id}/delete", response_class=HTMLResponse)
async def batch_delete(batch_id: str, user: dict = Depends(require_officer_access)):
    deleted = database.delete_tasks_by_batch(batch_id, user["guild_id"])
    return RedirectResponse(f"/tasks?{urlencode({'notice': f'Отменено заданий: {deleted}'})}", status_code=303)


@router.post("/sync-units", response_class=HTMLResponse)
async def sync_units_now(user: dict = Depends(require_officer_access)):
    comlink = _get_comlink()
    try:
        total = await sync_units(comlink)
    except Exception as e:
        return RedirectResponse(f"/tasks?{urlencode({'error': f'Ошибка синхронизации: {e}'})}", status_code=303)
    return RedirectResponse(f"/tasks?{urlencode({'synced': str(total)})}", status_code=303)
