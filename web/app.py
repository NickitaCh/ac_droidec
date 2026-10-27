"""Веб-дашборд AC Droidec (FastAPI). Читает/пишет тот же guild_management.db,
что и сам бот, напрямую через database.py (без промежуточного API у бота) —
запускается на том же VPS, рядом с процессом бота.

Запуск локально (для разработки): из корня репозитория
    uvicorn web.app:app --reload --port 8000
(корень репозитория должен быть текущей директорией — иначе не найдутся
database.py/guild_resolver.py/services/, которые лежат рядом с cogs/, не внутри web/)."""

import os
import traceback
from pathlib import Path
from urllib.parse import quote

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, Request
from fastapi.exception_handlers import http_exception_handler
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.sessions import SessionMiddleware

load_dotenv()

from services import demo_mode
from web import audit, auth
from web.routes import admin, birthdays, conquest, raid_tickets, raids, dashboard, datacrons, gear_farm, guild_dashboard, mod_analysis, mod_ku, mod_optimizer, mod_progress, mod_scan, mod_search, omicron, payments, qa_checklist, registration, stat_builder, stat_forecast, stat_plates, steal_build, subscribe, support, tasks, telegram, tw_defense, tw_scout, unit_images

BASE_DIR = Path(__file__).resolve().parent

# Демо-режим: база demo.db для демо-сессий (только в веб-процессе, см. services/demo_mode.py).
demo_mode.install()

app = FastAPI(title="AC Droidec", dependencies=[Depends(audit.capture_request)])


@app.exception_handler(StarletteHTTPException)
async def _redirect_unauthenticated_to_login(request: Request, exc: StarletteHTTPException):
    """Прямой заход по ссылке (например /activity) без сессии раньше падал
    голой JSON-ошибкой 401 от deps.get_current_user — вместо этого шлём на
    /login с ?next=<исходный путь>, чтобы после входа (через Discord OAuth —
    web/auth.py::callback — или логин/пароль) вернуться туда же."""
    if exc.status_code == 401:
        next_path = request.url.path
        if request.url.query:
            next_path += f"?{request.url.query}"
        return RedirectResponse(f"/login?next={quote(next_path, safe='')}")
    if exc.status_code == 403:
        # Раньше require_officer_access на любой защищённой странице (не только /)
        # отдавал голый JSON {"detail": "..."} — теперь шлём на главную, она сама
        # рендерит персонализированный статус через access_status_message
        # (services/dashboard_data.py), не нужно дублировать текст тут.
        return RedirectResponse("/")
    return await http_exception_handler(request, exc)

@app.exception_handler(Exception)
async def _log_server_error(request: Request, exc: Exception):
    """Необработанная ошибка страницы: трассировка — в web_errors (её подмешивает журнал
    обращения /feedback, web/routes/support.py), пользователю — страница с номером ошибки и
    кнопкой «Сообщить о проблеме» вместо голого «Internal Server Error»."""
    import database
    discord_id = None
    try:
        discord_id = (request.session.get("user") or {}).get("discord_id")
    except Exception:
        pass
    trace = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    print(f"❌ [web] {request.method} {request.url.path}\n{trace}")
    try:
        error_id = database.log_web_error(discord_id, request.method, str(request.url.path) + (f"?{request.url.query}" if request.url.query else ""), trace)
    except Exception:
        error_id = None
    number = f" №{error_id}" if error_id else ""
    return HTMLResponse(_ERROR_PAGE.format(number=number), status_code=500)


_ERROR_PAGE = """<!doctype html><html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Ошибка — AC Droidec</title>
<link rel="stylesheet" href="/static/style.css"></head>
<body><main class="error-page"><h1>Что-то пошло не так</h1>
<p>На сервере произошла ошибка{number}. Она уже записана в журнал.</p>
<p><a class="button" href="/feedback">Сообщить о проблеме</a> <a class="button button-secondary" href="javascript:history.back()">Назад</a></p>
</main></body></html>"""


@app.on_event("startup")
async def _warm_stat_calc():
    """Калькулятор статов собирается долго (вся игровая база из Comlink) — начинаем сразу
    при старте в фоне, чтобы первая страница со статами/скоростью не ждала его сама."""
    from swgoh_comlink import SwgohComlink
    from services import stat_forecast as stat_forecast_service
    stat_forecast_service.start_background_build(SwgohComlink(url="http://localhost:3000"))

session_secret = os.getenv("WEB_SESSION_SECRET")
if not session_secret:
    # Только для локальной разработки без .env — на проде WEB_SESSION_SECRET обязателен,
    # иначе сессии (в т.ч. факт логина) не переживут перезапуск процесса.
    session_secret = "dev-insecure-secret-change-me"
    print("⚠️ [web] WEB_SESSION_SECRET не задан — использую небезопасный dev-секрет")
app.add_middleware(audit.AuditMiddleware)
app.add_middleware(demo_mode.DemoMiddleware)  # внутри SessionMiddleware — ему нужна сессия
app.add_middleware(SessionMiddleware, secret_key=session_secret)

static_dir = BASE_DIR / "static"
static_dir.mkdir(exist_ok=True)
app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

app.include_router(auth.router, tags=["auth"])
app.include_router(dashboard.router, tags=["dashboard"])
app.include_router(unit_images.router, tags=["unit-images"])
app.include_router(guild_dashboard.router, tags=["guild-dashboard"])
app.include_router(tw_defense.router, prefix="/tw/def", tags=["tw-defense"])
app.include_router(tw_scout.router, prefix="/tw/scout", tags=["tw-scout"])
app.include_router(registration.router, prefix="/registration", tags=["registration"])
app.include_router(admin.router, prefix="/admin", tags=["admin"])
app.include_router(stat_plates.router, prefix="/plates", tags=["stat-plates"])
app.include_router(birthdays.router, prefix="/birthdays", tags=["birthdays"])
app.include_router(tasks.router, prefix="/tasks", tags=["tasks"])
app.include_router(datacrons.router, prefix="/datacrons", tags=["datacrons"])
app.include_router(omicron.router, prefix="/omicron", tags=["omicron"])
app.include_router(stat_forecast.router, prefix="/stats-check", tags=["stats-check"])
app.include_router(stat_builder.router, prefix="/mod-builder", tags=["mod-builder"])
app.include_router(mod_optimizer.router, prefix="/mod-optimizer", tags=["mod-optimizer"])
app.include_router(mod_scan.router, prefix="/mod-scan", tags=["mod-scan"])
app.include_router(mod_search.router, prefix="/mod-search", tags=["mod-search"])
app.include_router(steal_build.router, prefix="/steal-build", tags=["steal-build"])
app.include_router(mod_analysis.router, prefix="/mod-analysis", tags=["mod-analysis"])
app.include_router(mod_progress.router, prefix="/mod-progress", tags=["mod-progress"])
app.include_router(mod_ku.router, prefix="/mod-ku", tags=["mod-ku"])
app.include_router(payments.router, prefix="/payments", tags=["payments"])
app.include_router(subscribe.router, tags=["subscribe"])
app.include_router(support.router, tags=["support"])
app.include_router(qa_checklist.router, prefix="/qa-checklist", tags=["qa-checklist"])
app.include_router(gear_farm.router, prefix="/farm", tags=["gear-farm"])
app.include_router(raid_tickets.router, prefix="/tickets", tags=["raid-tickets"])
app.include_router(raids.router, prefix="/raids", tags=["raids"])
app.include_router(conquest.router, prefix="/conquest", tags=["conquest"])
app.include_router(telegram.router, tags=["telegram"])
