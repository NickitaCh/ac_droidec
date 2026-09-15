"""Веб-версия /статы_требования — интерактивное управление плейтами (наборами
норм статов) и требованиями внутри них. CRUD-логика целиком живёт в database.py
(create_stat_plate/rename_stat_plate/delete_stat_plate/add_stat_requirement/...),
этот роут только вызывает её и рендерит — без Comlink-валидации (в отличие от
registration.py/admin.py), поэтому отдельный services-слой не нужен.
Константы стат/оператор/приоритет переиспользуются из cogs, а не дублируются —
см. CLAUDE.md."""

from pathlib import Path
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

import database
from cogs.datacron_requirements import PRIORITY_CHOICES, PRIORITY_EMOJI, PRIORITY_LABELS, PRIORITY_REQUIRED
from cogs.stat_requirements import OPERATOR_CHOICES, STAT_CHOICES, STAT_OMICRON
from services import feature_flags

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent.parent / "templates"))

STAT_OPTIONS = [(c.name, c.value) for c in STAT_CHOICES]
OPERATOR_OPTIONS = [(c.name, c.value) for c in OPERATOR_CHOICES]
PRIORITY_OPTIONS = [(c.name, c.value) for c in PRIORITY_CHOICES]


def _fmt_value(value: float) -> str:
    return f"{value:g}"


def _unit_name(base_id: str) -> str:
    return database.get_game_unit_name(base_id) or base_id


def _omicron_options(base_id: str) -> list:
    """[{skill_id, label}, ...] — та же логика, что web/routes/tasks.py::_omicron_options /
    cogs/stat_requirements.py::_omicron_options_for_base, независимая копия (у каждого модуля
    свой собственный набор feature-гейтов вокруг вызовов, шарить нечего)."""
    skill_ids = database.get_all_unit_omicron_skills().get(base_id, [])
    if not skill_ids:
        return []
    info = database.get_skill_display_info(skill_ids)
    options = []
    for skill_id in skill_ids:
        name, _ability_id, ability_type, omicron_mode = info.get(skill_id, (skill_id, None, None, None))
        label = name or skill_id
        extra = " / ".join(p for p in (ability_type, omicron_mode) if p)
        if extra:
            label += f" ({extra})"
        options.append({"skill_id": skill_id, "label": label})
    return options


def _omicron_ability_name(skill_id: str) -> str:
    info = database.get_skill_display_info([skill_id])
    name, _ability_id, _ability_type, _omicron_mode = info.get(skill_id, (None, None, None, None))
    return name or skill_id


@router.get("", response_class=HTMLResponse)
async def plates_list(request: Request, user: dict = Depends(feature_flags.require_feature("stat_requirements"))):
    rows = database.get_all_stat_plates_detailed(guild_id=user["guild_id"])
    plates = [
        {"name": name, "description": description, "char_count": char_count, "req_count": req_count}
        for name, description, char_count, req_count in rows
    ]
    return templates.TemplateResponse(request, "plates.html", {
        "user": user,
        "plates": plates,
        "error": request.query_params.get("error"),
    })


@router.post("/create", response_class=HTMLResponse)
async def plates_create(
    request: Request,
    name: str = Form(...),
    description: str = Form(""),
    user: dict = Depends(feature_flags.require_feature("stat_requirements")),
):
    ok = database.create_stat_plate(name.strip(), description.strip() or None, user["discord_id"], guild_id=user["guild_id"])
    if not ok:
        return RedirectResponse(f"/plates?{urlencode({'error': f'Плейт «{name}» уже существует.'})}", status_code=303)
    return RedirectResponse(f"/plates/{name.strip()}", status_code=303)


@router.post("/{plate_name}/rename", response_class=HTMLResponse)
async def plates_rename(plate_name: str, new_name: str = Form(...), user: dict = Depends(feature_flags.require_feature("stat_requirements"))):
    new_name = new_name.strip()
    ok = database.rename_stat_plate(plate_name, new_name, guild_id=user["guild_id"])
    if not ok:
        error = f"Не удалось переименовать: плейт «{plate_name}» не найден либо «{new_name}» уже занято."
        return RedirectResponse(f"/plates?{urlencode({'error': error})}", status_code=303)
    return RedirectResponse(f"/plates/{new_name}", status_code=303)


@router.post("/{plate_name}/delete", response_class=HTMLResponse)
async def plates_delete(plate_name: str, user: dict = Depends(feature_flags.require_feature("stat_requirements"))):
    database.delete_stat_plate(plate_name, guild_id=user["guild_id"])
    return RedirectResponse("/plates", status_code=303)


@router.get("/api/units", response_class=JSONResponse)
async def units_search(q: str = "", user: dict = Depends(feature_flags.require_feature("stat_requirements"))):
    if not q or len(q.strip()) < 2:
        return []
    rows = database.search_game_units(q.strip(), limit=20)
    return [{"base_id": base_id, "name": name} for base_id, name in rows]


@router.get("/api/omicron-skills", response_class=JSONResponse)
async def omicron_skills(base_id: str = "", user: dict = Depends(feature_flags.require_feature("stat_requirements"))):
    if not base_id:
        return []
    return _omicron_options(base_id)


@router.get("/{plate_name}", response_class=HTMLResponse)
async def plate_detail(request: Request, plate_name: str, user: dict = Depends(feature_flags.require_feature("stat_requirements"))):
    guild_id = user["guild_id"]
    char_keys = database.get_stat_requirement_characters(plate_name, guild_id=guild_id)
    characters = []
    for base_id in char_keys:
        reqs = []
        for r in database.get_stat_requirements(plate_name, base_id, guild_id=guild_id):
            is_omicron = r[3] == STAT_OMICRON
            reqs.append({
                "id": r[0], "stat_name": r[3], "operator": r[4], "threshold": r[5],
                "threshold_fmt": _fmt_value(r[5]), "priority": r[6], "comment": r[8],
                "priority_label": PRIORITY_LABELS.get(r[6], r[6]), "priority_emoji": PRIORITY_EMOJI.get(r[6], ""),
                "is_omicron": is_omicron,
                "value_display": f"Омикрон: {_omicron_ability_name(r[11])} — разблокирован" if is_omicron else f"{r[3]} {r[4]} {_fmt_value(r[5])}",
            })
        characters.append({"base_id": base_id, "name": _unit_name(base_id), "requirements": reqs})
    characters.sort(key=lambda c: c["name"].lower())

    plate = database.get_stat_plate(plate_name, guild_id=guild_id)
    req_count = database.count_stat_requirements_by_plate(plate_name, guild_id=guild_id)

    return templates.TemplateResponse(request, "plate_detail.html", {
        "user": user,
        "plate_name": plate_name,
        "description": plate[1] if plate else None,
        "characters": characters,
        "req_count": req_count,
        "stat_options": STAT_OPTIONS,
        "operator_options": OPERATOR_OPTIONS,
        "priority_options": PRIORITY_OPTIONS,
        "priority_default": PRIORITY_REQUIRED,
        "error": request.query_params.get("error"),
    })


@router.post("/{plate_name}/requirements/add", response_class=HTMLResponse)
async def requirement_add(
    plate_name: str,
    base_id: str = Form(...),
    stat_name: str = Form(...),
    operator: str = Form(...),
    threshold: float = Form(...),
    priority: str = Form(PRIORITY_REQUIRED),
    comment: str = Form(""),
    user: dict = Depends(feature_flags.require_feature("stat_requirements")),
):
    guild_id = user["guild_id"]
    if plate_name not in database.get_all_stat_requirement_plates(guild_id=guild_id):
        return RedirectResponse(f"/plates?{urlencode({'error': f'Плейт «{plate_name}» не найден.'})}", status_code=303)

    char_name = _unit_name(base_id)
    raw_text = f"{char_name} {stat_name} {operator} {_fmt_value(threshold)}"
    database.add_stat_requirement(
        plate_name, base_id, stat_name, operator, threshold, priority, raw_text, comment.strip() or None,
        user["discord_id"], guild_id=guild_id,
    )
    return RedirectResponse(f"/plates/{plate_name}", status_code=303)


@router.post("/{plate_name}/requirements/add_omicron", response_class=HTMLResponse)
async def requirement_add_omicron(
    plate_name: str,
    base_id: str = Form(...),
    skill_id: str = Form(...),
    priority: str = Form(PRIORITY_REQUIRED),
    comment: str = Form(""),
    user: dict = Depends(feature_flags.require_feature("stat_requirements")),
):
    guild_id = user["guild_id"]
    if plate_name not in database.get_all_stat_requirement_plates(guild_id=guild_id):
        return RedirectResponse(f"/plates?{urlencode({'error': f'Плейт «{plate_name}» не найден.'})}", status_code=303)

    valid_skill_ids = {o["skill_id"] for o in _omicron_options(base_id)}
    if skill_id not in valid_skill_ids:
        return RedirectResponse(
            f"/plates/{plate_name}?{urlencode({'error': 'Выберите омикрон из списка — у персонажа нет такого.'})}", status_code=303
        )

    char_name = _unit_name(base_id)
    ability_name = _omicron_ability_name(skill_id)
    raw_text = f"{char_name} — омикрон «{ability_name}»"
    database.add_stat_requirement(
        plate_name, base_id, STAT_OMICRON, ">=", 1.0, priority, raw_text, comment.strip() or None,
        user["discord_id"], guild_id=guild_id, skill_id=skill_id,
    )
    return RedirectResponse(f"/plates/{plate_name}", status_code=303)


@router.post("/{plate_name}/requirements/{req_id}/edit", response_class=HTMLResponse)
async def requirement_edit(
    plate_name: str,
    req_id: int,
    operator: str = Form(None),
    threshold: float = Form(None),
    priority: str = Form(...),
    comment: str = Form(""),
    user: dict = Depends(feature_flags.require_feature("stat_requirements")),
):
    guild_id = user["guild_id"]
    row = database.get_stat_requirement(req_id, guild_id=guild_id)
    if not row:
        return RedirectResponse(f"/plates/{plate_name}?{urlencode({'error': f'Требование #{req_id} не найдено.'})}", status_code=303)
    _, row_plate, character_key, stat_name, cur_operator, cur_threshold, *_ = row
    # Оператор/значение у требования на омикрон захардкожены (см. cogs/stat_requirements.py::
    # stat_req_add_omicron) — форма для таких строк их вообще не присылает, оставляем как есть.
    new_operator = cur_operator if stat_name == STAT_OMICRON or operator is None else operator
    new_threshold = cur_threshold if stat_name == STAT_OMICRON or threshold is None else threshold
    database.update_stat_requirement(
        req_id, row_plate, character_key, stat_name, new_operator, new_threshold, priority, comment.strip() or None, guild_id=guild_id,
    )
    return RedirectResponse(f"/plates/{plate_name}", status_code=303)


@router.post("/{plate_name}/requirements/{req_id}/delete", response_class=HTMLResponse)
async def requirement_delete(plate_name: str, req_id: int, user: dict = Depends(feature_flags.require_feature("stat_requirements"))):
    database.delete_stat_requirement(req_id, guild_id=user["guild_id"])
    return RedirectResponse(f"/plates/{plate_name}", status_code=303)
