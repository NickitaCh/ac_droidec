"""Временная страница: чек-лист по каждой странице веб-дашборда, чтобы офицеры
(изначально — под AC Ricardo) отмечали, что страница уже проверена/отполирована.
Список страниц хардкожен здесь (PAGES) — сама галочка/кто/когда хранится в БД
(database.qa_checklist_*), не в списке. Не привязано к нав-меню (base.html) —
доступна только по прямой ссылке /qa-checklist, убрать роут/шаблон, когда
чек-лист станет не нужен. Намеренно БЕЗ require_officer_access — страница
открыта без логина (по просьбе пользователя), чтобы её было проще кинуть
ссылкой и сразу отмечать; кто отметил — берётся из свободного текстового поля
на странице (сохраняется в localStorage браузера), а не из сессии.

Обновлено 2026-09-23 (по запросу пользователя, "сделай там все страницы, что
есть") — полная сверка со всеми web/routes/*.py + актуальным nav в base.html,
предыдущий список (22 записи, от 2026-08-24) заметно отстал от реального
состояния сайта (не было mod-search/mod-analysis/mod-scan/steal-build/tasks-в-
меню/player-card/tb-платунов/омикрон-приоритета/features и т.д.). Каждая
запись — (ключ, название, путь, группа); группа = один из GROUPS ниже,
определяется по реальной FastAPI-зависимости роута
(require_officer_access/require_guild_access/feature_flags.require_feature(...)
→ "officers"; голый get_current_user → "players"; get_current_user_optional
или совсем без Depends → "everyone"; require_super_admin → "super_admin"), а
"hidden" — отдельная ось поверх этого: страница НЕ выводится НИ ОДНОЙ ссылкой
нигде в интерфейсе (ни в sidebar-нав, ни как ссылка изнутри другой страницы) —
сейчас это только сам /qa-checklist и /steal-build (ссылку из меню убрали
2026-09-23, страница живёт только по прямой ссылке). Динамические
страницы-карточки (путь с {параметром}) не кликабельны в самой таблице (нет
конкретного примера id/кода) — реальный способ попасть на них не по URL руками,
а кликом с родительской страницы (это указано в названии)."""

from pathlib import Path

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

import database

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent.parent / "templates"))

GROUPS = [
    ("officers", "Доступно офицерам"),
    ("players", "Доступно игрокам"),
    ("everyone", "Доступно всем"),
    ("hidden", "Скрыто (нет ссылки нигде в интерфейсе)"),
    ("super_admin", "Доступно суперадмину"),
]

# (ключ, название, путь, группа) — по одному на каждую реальную GET/HTML-страницу сайта.
PAGES = [
    # ---- Доступно офицерам (require_officer_access / require_guild_access / require_feature) ----
    ("activity", "Активность", "/activity", "officers"),
    ("activity_players", "Активность — по игрокам", "/activity/players", "officers"),
    ("activity_resources", "Активность — ресурсы (гир/релик)", "/activity/resources", "officers"),
    ("violations", "Нарушения", "/violations", "officers"),
    ("violations_player", "Нарушения — карточка игрока (клик из таблицы)", "/violations/{ally_code}", "officers"),
    ("tb", "ТБ — отчёт", "/tb", "officers"),
    ("tb_player", "ТБ — карточка игрока (клик из отчёта)", "/tb/player/{name}", "officers"),
    ("tb_compare", "ТБ — сравнение игрока (клик из отчёта)", "/tb/compare/{name}", "officers"),
    ("tb_plan", "ТБ — план планет", "/tb/plan", "officers"),
    ("tb_order_plans", "ТБ — планы ордера", "/tb/order-plans", "officers"),
    ("tb_platoons", "ТБ — взводы", "/tb/platoons", "officers"),
    ("tb_platoons_filters", "ТБ — взводы, фильтры автозаполнения (со страницы Взводы)", "/tb/platoons/filters", "officers"),
    ("tb_platoons_notify", "ТБ — взводы, рассылка в личку (со страницы Взводы)", "/tb/platoons/notify", "officers"),
    ("tw", "ВГ — отчёт", "/tw", "officers"),
    ("tasks", "Задачи", "/tasks", "officers"),
    ("datacrons", "Датакроны", "/datacrons", "officers"),
    ("datacrons_check", "Датакроны — проверить игрока", "/datacrons/check", "officers"),
    ("datacrons_season", "Датакроны — карточка сезона (клик из списка)", "/datacrons/{set_id}", "officers"),
    ("plates", "Моды — Плейты", "/plates", "officers"),
    ("plate_detail", "Моды — карточка плейта (клик из списка)", "/plates/{plate_name}", "officers"),
    ("stats_check", "Моды — Статы", "/stats-check", "officers"),
    ("stats_relic", "Моды — Статы (релик)", "/stats-check/relic", "officers"),
    ("mod_builder", "Моды — Калькулятор", "/mod-builder", "officers"),
    ("mod_search", "Моды — Поиск модов", "/mod-search", "officers"),
    ("mod_analysis", "Моды — Анализ модинга", "/mod-analysis", "officers"),
    ("mod_scan", "Моды — Скан модов (только AbsoluteChaos)", "/mod-scan", "officers"),
    ("omicron_phrases", "Омикроны — Фразы для омикронов", "/omicron/phrases", "officers"),
    ("omicrons_priority", "Омикроны — Приоритет для ВГ", "/omicrons/priority", "officers"),
    ("omicrons_report", "Омикроны — Отчёт по игрокам", "/omicrons/report", "officers"),
    ("omicrons_report_player", "Омикроны — отчёт, карточка игрока (клик из отчёта)", "/omicrons/report/{name}", "officers"),
    ("birthdays", "Дни рождения", "/birthdays", "officers"),
    ("registration", "Админ меню — Регистрация", "/registration", "officers"),
    ("guild_access", "Админ меню — Ручной доступ", "/access", "officers"),
    ("settings", "Админ меню — Настройки", "/settings", "officers"),
    ("player_card", "Карточка игрока (клик из ростера/отчётов)", "/player/{ally_code}", "officers"),
    # ---- Доступно игрокам (get_current_user — любой залогиненный, не только офицер) ----
    ("subscribe", "Подписка", "/subscribe", "players"),
    # ---- Доступно всем (без логина) ----
    ("home", "Главная", "/", "everyone"),
    ("start", "Начало работы", "/start", "everyone"),
    ("commands", "Документация (команды)", "/commands", "everyone"),
    ("oferta", "Публичная оферта", "/oferta", "everyone"),
    ("login_password", "Вход по логину и паролю", "/login/password", "everyone"),
    # ---- Скрыто (нет ссылки нигде в интерфейсе — только по прямой ссылке) ----
    ("qa_checklist", "QA-чеклист (эта страница)", "/qa-checklist", "hidden"),
    ("steal_build", "Спиздить билд", "/steal-build", "hidden"),
    # ---- Доступно суперадмину (require_super_admin) ----
    ("admin_guilds", "Админ — Гильдии", "/admin/guilds", "super_admin"),
    ("admin_access", "Админ — Админы", "/admin/access", "super_admin"),
    ("admin_web_accounts", "Админ — Логин/пароль", "/admin/web-accounts", "super_admin"),
    ("admin_access_log", "Админ — Лог входов", "/admin/access-log", "super_admin"),
    ("admin_command_usage", "Админ — Команды", "/admin/command-usage", "super_admin"),
    ("mod_optimizer", "Оптимизатор модов", "/mod-optimizer", "super_admin"),
    ("admin_fun", "Админ — Фан", "/admin/fun", "super_admin"),
    ("admin_tb_schedule", "Админ — ТБ-время", "/admin/tb-schedule", "super_admin"),
    ("admin_features", "Админ — Фичи по гильдиям", "/admin/features", "super_admin"),
]


@router.get("", response_class=HTMLResponse)
async def qa_checklist(request: Request):
    state = database.get_qa_checklist_state()
    rows = []
    for key, name, path, group in PAGES:
        entry = state.get(key, {})
        rows.append({
            "key": key,
            "name": name,
            "path": path,
            "group": group,
            "checked": entry.get("checked", False),
            "checked_by": entry.get("checked_by"),
            "checked_at": entry.get("checked_at"),
        })
    checked_count = sum(1 for r in rows if r["checked"])
    return templates.TemplateResponse(request, "qa_checklist.html", {
        "user": request.session.get("user"),
        "groups": GROUPS,
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
