"""Веб-управление задачами на прокачку (аналог /задачи в Discord) — список с
фильтром/группировкой по игроку, точечная и массовая постановка, редактирование
(значение цели + дедлайн) и удаление (по одному и целой группой), плюс предзаполнение
формы из гильдийского отчёта по плейту (кнопка "+ задача" на /stats-check).
Аудит выполнения остаётся только в боте (cogs/tasks.py::tasks_audit_loop); просрочка
и напоминания — там же (tasks_notify_loop) — веб лишь читает уже проставленный ботом
статус и настраивает время рассылки (guilds.tasks_notify_time, см. /settings)."""

import uuid
from datetime import datetime
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

STATUS_BADGE = {"ACTIVE": "badge-neutral", "COMPLETED": "badge-ok", "FAILED": "badge-danger"}


def _status_label(status: str, in_progress) -> str:
    """ACTIVE различается на "Назначено"/"В работе" по in_progress — см.
    database.update_task_progress / cogs/tasks.py::TasksCog._status_label (тот же
    паттерн, продублирован здесь по общей для этого файла конвенции — см.
    _target_label ниже, который так же дублирует cogs/tasks.py)."""
    if status == "COMPLETED":
        return "Выполнено"
    if status == "FAILED":
        return "Провалено"
    return "В работе" if in_progress else "Назначено"


def _progress_label(initial_value, current_value) -> str:
    if initial_value is None or current_value is None:
        return "—"
    if initial_value == current_value:
        return str(current_value)
    return f"{initial_value} → {current_value}"


def _get_comlink():
    # См. web/routes/registration.py::_get_comlink — веб-процесс не поднимает
    # диска-клиента, строит свой SwgohComlink на тот же сайдкар.
    from swgoh_comlink import SwgohComlink
    return SwgohComlink(url="http://localhost:3000")


def _fetch_unit_data(comlink, ally_code: str, base_id: str) -> dict | None:
    """Та же логика, что cogs/tasks.py::TasksCog._fetch_unit_data — синхронная
    версия (веб-процесс уже вызывает Comlink напрямую без to_thread, см.
    stat_forecast.py и другие роуты этого файла)."""
    try:
        player_data = comlink.get_player(ally_code)
    except Exception:
        return None
    roster = player_data.get('rosterUnit') or player_data.get('roster')
    if not roster:
        return None
    for u in roster:
        u_id = u.get('baseId') or u.get('definitionId', '').split(':')[0]
        if u_id == base_id:
            return u
    return None


def _current_progress_value(unit_data: dict, target_type: str, target_value: str) -> str:
    if target_type == 'stars':
        return str(unit_data.get('currentRarity', 0))
    if target_type == 'relic':
        return str(unit_data.get('relic', {}).get('currentTier', 0))
    if target_type == 'omicron':
        for skill in unit_data.get('skill', []):
            if skill.get('id') == target_value:
                return str(skill.get('tier', 0))
        return "0"
    return ""


def _is_target_completed(unit_data: dict, target_type: str, target_value: str, skill_thresholds: dict) -> bool:
    if target_type == 'stars':
        return unit_data.get('currentRarity', 0) >= int(target_value)
    if target_type == 'relic':
        current_relic_tier = unit_data.get('relic', {}).get('currentTier', 0)
        target_val_int = int(target_value)
        required_tier = target_val_int + 2 if target_val_int > 0 else 0
        return current_relic_tier >= required_tier
    if target_type == 'omicron':
        omicron_tier = skill_thresholds.get(target_value, (None, None))[1]
        if omicron_tier is None:
            return False
        return any(
            skill.get('id') == target_value and skill.get('tier', -1) >= omicron_tier
            for skill in unit_data.get('skill', [])
        )
    return False


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


def _validate_deadline(deadline: str) -> str | None:
    """None — ок, иначе текст ошибки. Дедлайн вводится датой (не количеством дней,
    см. Discord-тред "Гайд по АС Боту", 2026-09-04) — <input type="date"> в шаблоне
    уже даёт и календарь, и ручной ввод, нормализуя формат до ГГГГ-ММ-ДД."""
    deadline = deadline.strip()
    try:
        parsed = datetime.strptime(deadline, "%Y-%m-%d").date()
    except ValueError:
        return "Некорректная дата дедлайна (ГГГГ-ММ-ДД)."
    if parsed < datetime.now().date():
        return "Дедлайн не может быть в прошлом."
    return None


@router.get("", response_class=HTMLResponse)
async def tasks_list(request: Request, user: dict = Depends(require_officer_access)):
    guild_id = user["guild_id"]
    status_filter = request.query_params.get("status")
    view = request.query_params.get("view", "flat")

    rows = database.get_all_tasks(guild_id)
    names_by_code = {code: name for _, code, name in database.get_all_user_mappings(guild_id)}
    unit_names = database.get_game_unit_names([r[2] for r in rows])
    creator_ids = {r[11] for r in rows if r[11]}
    creator_names = {cid: (database.get_username_for_discord_id(cid) or cid) for cid in creator_ids}

    all_ctx = [
        {
            "task_id": task_id, "ally_code": ally_code, "base_id": base_id,
            "player_name": names_by_code.get(ally_code, ally_code),
            "unit_name": unit_names.get(base_id) or base_id,
            "target_type": target_type, "target_value": target_value,
            "target_label": _target_label(target_type, target_value),
            "deadline": deadline, "status": status,
            "status_label": _status_label(status, in_progress),
            "status_badge": STATUS_BADGE.get(status, "badge-neutral"),
            "progress": _progress_label(initial_value, current_value),
            "created_by_name": creator_names.get(created_by, "—"),
            "batch_id": batch_id,
            "archived": database.is_task_archived(resolved_at),
        }
        for (task_id, ally_code, base_id, target_type, target_value, deadline, status, batch_id,
             initial_value, current_value, in_progress, created_by, resolved_at) in rows
    ]

    # "Архив" — отдельная вкладка (COMPLETED/FAILED старше database.TASK_ARCHIVE_AFTER_DAYS
    # дней); везде остальное ("Список"/"По игрокам") — только текущие задачи, как и в
    # отчётах бота (см. Discord-тред "Гайд по АС Боту", пункт "Архив").
    if view == "archive":
        scope = [t for t in all_ctx if t["archived"]]
    else:
        scope = [t for t in all_ctx if not t["archived"]]

    counts = {"ACTIVE": 0, "COMPLETED": 0, "FAILED": 0}
    for t in scope:
        counts[t["status"]] = counts.get(t["status"], 0) + 1

    tasks_ctx = [t for t in scope if not status_filter or t["status"] == status_filter]

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
        "archive_after_days": database.TASK_ARCHIVE_AFTER_DAYS,
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
    deadline: str = Form(...),
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

    err = _validate_deadline(deadline)
    if err:
        return RedirectResponse(f"/tasks?{urlencode({'error': err})}", status_code=303)

    # Запрет дублей — см. cogs/tasks.py::task_add за тем же правилом/обоснованием.
    if database.get_active_task_for_unit(guild_id, ally_code, base_id):
        return RedirectResponse(
            f"/tasks?{urlencode({'error': f'Задача по юниту {unit_name} для {names_by_code[ally_code]} уже назначена.'})}",
            status_code=303,
        )

    target_value = target_value.strip()
    initial_value = None
    try:
        comlink = _get_comlink()
        unit_data = _fetch_unit_data(comlink, ally_code, base_id)
        if unit_data is not None:
            skill_thresholds = database.get_all_skill_tier_thresholds() if target_type == "omicron" else {}
            if _is_target_completed(unit_data, target_type, target_value, skill_thresholds):
                return RedirectResponse(
                    f"/tasks?{urlencode({'notice': f'{names_by_code[ally_code]}: цель уже выполнена, задача не создана.'})}",
                    status_code=303,
                )
            initial_value = _current_progress_value(unit_data, target_type, target_value)
    except Exception:
        pass  # Comlink недоступен — создаём без снимка initial_value, не блокируем постановку

    database.add_task(ally_code, base_id, target_type, target_value, deadline, str(user["discord_id"]),
                       guild_id=guild_id, initial_value=initial_value)
    return RedirectResponse("/tasks", status_code=303)


@router.post("/add-bulk", response_class=HTMLResponse)
async def task_add_bulk(
    ally_codes: list = Form(default=[]),
    base_id: str = Form(...),
    target_type: str = Form(...),
    target_value: str = Form(...),
    deadline: str = Form(...),
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

    err = _validate_deadline(deadline)
    if err:
        return RedirectResponse(f"/tasks?{urlencode({'error': err})}", status_code=303)

    batch_id = uuid.uuid4().hex[:12]
    target_value = target_value.strip()
    skill_thresholds = database.get_all_skill_tier_thresholds() if target_type == "omicron" else {}
    try:
        comlink = _get_comlink()
    except Exception:
        comlink = None

    created, skipped_dup, skipped_done = 0, 0, 0
    for code in valid_codes:
        # Дубль — уже есть активная задача на этот юнит для этого игрока (пропускаем
        # молча при массовой постановке, см. Ricardo 2026-09-04: "просто не добавлять
        # в таблицу дубли" — в отличие от точечной постановки, где это явная ошибка).
        if database.get_active_task_for_unit(guild_id, code, base_id):
            skipped_dup += 1
            continue

        initial_value = None
        if comlink is not None:
            try:
                unit_data = _fetch_unit_data(comlink, code, base_id)
                if unit_data is not None:
                    if _is_target_completed(unit_data, target_type, target_value, skill_thresholds):
                        skipped_done += 1
                        continue
                    initial_value = _current_progress_value(unit_data, target_type, target_value)
            except Exception:
                pass

        database.add_task(code, base_id, target_type, target_value, deadline, str(user["discord_id"]),
                           guild_id=guild_id, batch_id=batch_id, initial_value=initial_value)
        created += 1

    parts = [f"поставлено: {created}"]
    if skipped_dup:
        parts.append(f"пропущено (уже назначено): {skipped_dup}")
    if skipped_done:
        parts.append(f"пропущено (уже выполнено): {skipped_done}")
    return RedirectResponse(f"/tasks?{urlencode({'notice': ', '.join(parts)})}", status_code=303)


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
        return RedirectResponse(f"/tasks?{urlencode({'error': 'Задача не найдена.'})}", status_code=303)

    base_id, target_type = task[2], task[3]
    err = _validate_target(base_id, target_type, target_value)
    if err:
        return RedirectResponse(f"/tasks?{urlencode({'error': err})}", status_code=303)

    try:
        datetime.strptime(deadline, "%Y-%m-%d")
    except ValueError:
        return RedirectResponse(f"/tasks?{urlencode({'error': 'Некорректная дата дедлайна (ГГГГ-ММ-ДД).'})}", status_code=303)

    database.update_task(task_id, target_value=target_value.strip(), deadline=deadline)
    return RedirectResponse(f"/tasks?{urlencode({'notice': 'Задача обновлена'})}", status_code=303)


@router.post("/{task_id}/delete", response_class=HTMLResponse)
async def task_delete(task_id: int, user: dict = Depends(require_officer_access)):
    guild_id = user["guild_id"]
    task = database.get_task(task_id)
    if task and task[7] == guild_id:
        database.delete_task(task_id)
    return RedirectResponse(f"/tasks?{urlencode({'notice': 'Задача удалена'})}", status_code=303)


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
