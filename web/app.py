"""Веб-дашборд AC Droidec (FastAPI). Читает/пишет тот же guild_management.db,
что и сам бот, напрямую через database.py (без промежуточного API у бота) —
запускается на том же VPS, рядом с процессом бота.

Запуск локально (для разработки): из корня репозитория
    uvicorn web.app:app --reload --port 8000
(корень репозитория должен быть текущей директорией — иначе не найдутся
database.py/guild_resolver.py/services/, которые лежат рядом с cogs/, не внутри web/)."""

import os
from pathlib import Path
from urllib.parse import quote

from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.exception_handlers import http_exception_handler
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.sessions import SessionMiddleware

load_dotenv()

from web import auth
from web.routes import admin, birthdays, dashboard, datacrons, guild_dashboard, qa_checklist, registration, stat_builder, stat_forecast, stat_plates, tasks

BASE_DIR = Path(__file__).resolve().parent

app = FastAPI(title="AC Droidec")


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
    return await http_exception_handler(request, exc)

session_secret = os.getenv("WEB_SESSION_SECRET")
if not session_secret:
    # Только для локальной разработки без .env — на проде WEB_SESSION_SECRET обязателен,
    # иначе сессии (в т.ч. факт логина) не переживут перезапуск процесса.
    session_secret = "dev-insecure-secret-change-me"
    print("⚠️ [web] WEB_SESSION_SECRET не задан — использую небезопасный dev-секрет")
app.add_middleware(SessionMiddleware, secret_key=session_secret)

static_dir = BASE_DIR / "static"
static_dir.mkdir(exist_ok=True)
app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

app.include_router(auth.router, tags=["auth"])
app.include_router(dashboard.router, tags=["dashboard"])
app.include_router(guild_dashboard.router, tags=["guild-dashboard"])
app.include_router(registration.router, prefix="/registration", tags=["registration"])
app.include_router(admin.router, prefix="/admin", tags=["admin"])
app.include_router(stat_plates.router, prefix="/plates", tags=["stat-plates"])
app.include_router(birthdays.router, prefix="/birthdays", tags=["birthdays"])
app.include_router(tasks.router, prefix="/tasks", tags=["tasks"])
app.include_router(datacrons.router, prefix="/datacrons", tags=["datacrons"])
app.include_router(stat_forecast.router, prefix="/stats-check", tags=["stats-check"])
app.include_router(stat_builder.router, prefix="/mod-builder", tags=["mod-builder"])
app.include_router(qa_checklist.router, prefix="/qa-checklist", tags=["qa-checklist"])
