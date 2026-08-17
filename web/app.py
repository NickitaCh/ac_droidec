"""Веб-дашборд AC Droidec (FastAPI). Читает/пишет тот же guild_management.db,
что и сам бот, напрямую через database.py (без промежуточного API у бота) —
запускается на том же VPS, рядом с процессом бота.

Запуск локально (для разработки): из корня репозитория
    uvicorn web.app:app --reload --port 8000
(корень репозитория должен быть текущей директорией — иначе не найдутся
database.py/guild_resolver.py/services/, которые лежат рядом с cogs/, не внутри web/)."""

import os
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

load_dotenv()

from web import auth
from web.routes import admin, dashboard, guild_dashboard, registration

BASE_DIR = Path(__file__).resolve().parent

app = FastAPI(title="AC Droidec")

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
