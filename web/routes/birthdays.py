"""Веб-версия /др_добавить, /др_удалить, /др_список — CRUD списка дней рождения
гильдии. Ежедневное поздравление и выдача/снятие роли в Discord (06:00/00:00 МСК,
cogs/birthday.py::check_loop) читают live-состояние Discord-гильдии и поэтому
остаются только в боте — веб только редактирует сами даты.
parse_birthday/next_birthday — чистые функции без disnake-зависимостей, переиспользуются
из cogs.birthday напрямую (тот же приём, что PRIORITY_*/OPERATOR_CHOICES в stat_plates.py)."""

from datetime import date
from pathlib import Path
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

import database
from cogs.birthday import next_birthday, parse_birthday
from web.deps import require_officer_access

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent.parent / "templates"))


@router.get("", response_class=HTMLResponse)
async def birthdays_list(request: Request, user: dict = Depends(require_officer_access)):
    guild_id = user["guild_id"]
    all_bdays = database.get_all_birthdays(guild_id=guild_id)

    registrations_by_id = {
        discord_id: ingame_name
        for discord_id, ally_code, ingame_name in database.get_all_main_registrations(guild_id)
    }

    today = date.today()
    rows = []
    for discord_id_str, day, month, year in all_bdays:
        display_name = database.get_username_for_discord_id(discord_id_str) or registrations_by_id.get(discord_id_str)
        date_str = f"{day:02d}.{month:02d}" + (f".{year}" if year is not None else "")
        # Формат с дефисами, а не точками — под парсер parse_birthday, используется
        # только для предзаполнения формы редактирования на клиенте.
        edit_date_str = f"{day:02d}-{month:02d}" + (f"-{year}" if year is not None else "")
        rows.append({
            "discord_id": discord_id_str,
            "display_name": display_name,
            "date_str": date_str,
            "edit_date_str": edit_date_str,
            "next_birthday": next_birthday(day, month, year, today),
        })
    rows.sort(key=lambda r: r["next_birthday"])

    return templates.TemplateResponse(request, "birthdays.html", {
        "user": user,
        "rows": rows,
        "error": request.query_params.get("error"),
    })


@router.get("/api/players", response_class=JSONResponse)
async def players_search(q: str = "", user: dict = Depends(require_officer_access)):
    if not q or len(q.strip()) < 2:
        return []
    search = q.strip().lower()
    matches = [
        {"discord_id": discord_id, "name": ingame_name}
        for discord_id, ally_code, ingame_name in database.get_all_main_registrations(user["guild_id"])
        if search in (ingame_name or "").lower()
    ]
    return matches[:20]


@router.post("/add", response_class=HTMLResponse)
async def birthdays_add(
    discord_id_search: str = Form(""),
    discord_id_manual: str = Form(""),
    date_str: str = Form(...),
    user: dict = Depends(require_officer_access),
):
    # Форма предлагает два способа выбрать игрока — живой поиск по регистрациям
    # (основной путь) или ручной ввод ID (для незарегистрированных участников).
    # Поиск в приоритете, если оба почему-то заполнены.
    discord_id = (discord_id_search or discord_id_manual).strip()
    if not discord_id or not discord_id.isdigit():
        return RedirectResponse(f"/birthdays?{urlencode({'error': 'Укажите игрока через поиск либо его числовой Discord ID вручную.'})}", status_code=303)

    try:
        day, month, year = parse_birthday(date_str.strip())
    except ValueError as e:
        return RedirectResponse(f"/birthdays?{urlencode({'error': str(e)})}", status_code=303)

    database.add_birthday(discord_id, day, month, year, guild_id=user["guild_id"])
    return RedirectResponse("/birthdays", status_code=303)


@router.post("/{discord_id}/remove", response_class=HTMLResponse)
async def birthdays_remove(discord_id: str, user: dict = Depends(require_officer_access)):
    database.remove_birthday(discord_id, guild_id=user["guild_id"])
    return RedirectResponse("/birthdays", status_code=303)
