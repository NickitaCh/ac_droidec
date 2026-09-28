"""Аудит действий офицеров/админов в вебе — пишет в database.audit_log любой
изменяющий запрос (не GET/HEAD/OPTIONS), страница — /admin/audit.

Две половинки, потому что ни одна не видит всего сама:
  * capture_request — глобальная FastAPI-зависимость (web/app.py). Работает ПОСЛЕ
    того, как FastAPI уже распарсил тело (form/json кэшируются на Request), поэтому
    повторное чтение безопасно. Резолвит автора и кладёт черновик записи в scope.
  * AuditMiddleware — чистый ASGI-мидлварь: видит итоговый HTTP-статус (в т.ч. после
    exception_handler'ов) и пишет запись уже со статусом.
"""

import json

from starlette.datastructures import UploadFile
from fastapi import Request

import database

_SKIP_METHODS = {"GET", "HEAD", "OPTIONS"}
# Пути, где POST ничего не меняет (превью/расчёты) или служебные.
_SKIP_PREFIXES = ("/login", "/logout", "/auth", "/payments/webhook", "/qa-checklist", "/mod-optimizer", "/static")
_SKIP_SUBSTRINGS = ("/api/", "preview", "candidates", "/units/search")
_SECRET_MARKERS = ("password", "token", "secret", "csrf")
_MAX_VALUE_LEN = 300

# Раздел по первому сегменту пути — чтобы в логе было понятно "где", без чтения URL.
SECTION_LABELS = {
    "admin": "Админка", "plates": "Плейты", "tasks": "Задачи", "datacrons": "Датакроны",
    "omicron": "Омикроны", "omicrons": "Омикроны", "registration": "Регистрация", "birthdays": "Дни рождения",
    "tb": "ТБ", "tw": "ВГ", "violations": "Нарушения", "settings": "Настройки", "mod-search": "Поиск модов",
    "mod-scan": "Скан модов", "mod-builder": "Калькулятор модов", "steal-build": "Кража билда",
    "mod-analysis": "Анализ модов", "subscribe": "Подписка", "activity": "Активность", "player": "Карточка игрока",
    "guild-access": "Доступ гильдии",
}


def section_for(path: str) -> str:
    first = path.strip("/").split("/", 1)[0]
    return SECTION_LABELS.get(first, first or "—")


def _clean_value(key: str, value):
    if any(m in key.lower() for m in _SECRET_MARKERS):
        return "***"
    if isinstance(value, UploadFile):
        return f"<файл {value.filename}>"
    if not isinstance(value, str):
        value = json.dumps(value, ensure_ascii=False) if isinstance(value, (dict, list)) else str(value)
    return value if len(value) <= _MAX_VALUE_LEN else value[:_MAX_VALUE_LEN] + "…"


async def _request_params(request: Request) -> dict:
    params = {}
    ctype = request.headers.get("content-type", "")
    try:
        if ctype.startswith(("application/x-www-form-urlencoded", "multipart/form-data")):
            form = await request.form()
            for key in form.keys():
                values = form.getlist(key)
                cleaned = [_clean_value(key, v) for v in values]
                params[key] = cleaned[0] if len(cleaned) == 1 else ", ".join(cleaned)
        elif ctype.startswith("application/json"):
            body = await request.json()
            if isinstance(body, dict):
                params = {k: _clean_value(k, v) for k, v in body.items()}
            else:
                params = {"body": _clean_value("body", body)}
    except Exception:  # аудит не должен ронять запрос
        pass
    if request.url.query:
        params["?"] = _clean_value("query", request.url.query)
    return params


async def capture_request(request: Request) -> None:
    if request.method in _SKIP_METHODS:
        return
    path = request.url.path
    if path.startswith(_SKIP_PREFIXES) or any(s in path for s in _SKIP_SUBSTRINGS):
        return
    if not request.session.get("user"):
        return
    try:
        from web.deps import get_current_user
        user = get_current_user(request)
    except Exception:
        return
    if user.get("tier") != "officer" and not user.get("is_super_admin"):
        return
    request.scope["audit_entry"] = {
        "action": f"{request.method} {path}",
        "guild_id": user.get("guild_id"),
        "actor_discord_id": user.get("discord_id"),
        "actor_name": user.get("username"),
        "is_super_admin": bool(user.get("is_super_admin")),
        "details": await _request_params(request),
    }


class AuditMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope.get("method") in _SKIP_METHODS:
            return await self.app(scope, receive, send)

        status = {"code": None}

        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                status["code"] = message["status"]
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        except Exception:
            status["code"] = status["code"] or 500
            raise
        finally:
            entry = scope.get("audit_entry")
            if entry:
                try:
                    database.log_audit("web", status=str(status["code"] or ""), **entry)
                except Exception as e:
                    print(f"⚠️ [audit] не удалось записать: {e}")
