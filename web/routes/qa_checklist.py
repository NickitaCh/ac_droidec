"""Временная страница: чек-лист по каждой странице веб-дашборда, чтобы офицеры
(изначально — под AC Ricardo) отмечали, что страница уже проверена/отполирована.
Список страниц хардкожен здесь (PAGES) — сама галочка/кто/когда хранится в БД
(database.qa_checklist_*), не в списке. Не привязано к нав-меню (base.html) —
доступна только по прямой ссылке /qa-checklist, убрать роут/шаблон, когда
чек-лист станет не нужен. Намеренно БЕЗ require_officer_access — страница
открыта без логина (по просьбе пользователя), чтобы её было проще кинуть
ссылкой и сразу отмечать; кто отметил — берётся из свободного текстового поля
на странице (сохраняется в localStorage браузера), а не из сессии."""

from pathlib import Path

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

import database

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent.parent / "templates"))

# (ключ, название, путь) — по одному на каждую реальную GET/HTML-страницу сайта.
PAGES = [
    ("home", "Главная", "/"),
    ("tb", "ТБ — отчёт", "/tb"),
    ("tb_plan", "ТБ — план", "/tb/plan"),
    ("tw", "ВГ — отчёт", "/tw"),
    ("violations", "Нарушения", "/violations"),
    ("activity", "Активность", "/activity"),
    ("datacrons", "Датакроны", "/datacrons"),
    ("datacrons_check", "Датакроны — проверить игрока", "/datacrons/check"),
    ("birthdays", "Дни рождения", "/birthdays"),
    ("registration", "Регистрация", "/registration"),
    ("settings", "Настройки", "/settings"),
    ("tasks", "Задачи (скрыта из меню)", "/tasks"),
    ("plates", "Моды — Плейты", "/plates"),
    ("stats_check", "Моды — Статы", "/stats-check"),
    ("stats_relic", "Моды — Статы (релик)", "/stats-check/relic"),
    ("mod_builder", "Моды — Конструктор", "/mod-builder"),
    ("admin_guilds", "Админ — Гильдии", "/admin/guilds"),
    ("admin_access", "Админ — Админы", "/admin/access"),
    ("admin_web_accounts", "Админ — Логин/пароль", "/admin/web-accounts"),
    ("admin_access_log", "Админ — Лог входов", "/admin/access-log"),
    ("admin_command_usage", "Админ — Команды", "/admin/command-usage"),
    ("omicron_phrases", "Омикроны — Фразы для омикронов", "/omicron/phrases"),
]


@router.get("", response_class=HTMLResponse)
async def qa_checklist(request: Request):
    state = database.get_qa_checklist_state()
    rows = []
    for key, name, path in PAGES:
        entry = state.get(key, {})
        rows.append({
            "key": key,
            "name": name,
            "path": path,
            "checked": entry.get("checked", False),
            "checked_by": entry.get("checked_by"),
            "checked_at": entry.get("checked_at"),
        })
    checked_count = sum(1 for r in rows if r["checked"])
    return templates.TemplateResponse(request, "qa_checklist.html", {
        "user": request.session.get("user"),
        "rows": rows,
        "checked_count": checked_count,
        "total": len(rows),
    })


@router.post("/{page_key}/toggle", response_class=RedirectResponse)
async def toggle_page(page_key: str, checked_by: str = Form(default="")):
    state = database.get_qa_checklist_state()
    currently_checked = state.get(page_key, {}).get("checked", False)
    database.set_qa_checklist_item(page_key, not currently_checked, checked_by.strip() or "Аноним")
    return RedirectResponse(url="/qa-checklist", status_code=303)
