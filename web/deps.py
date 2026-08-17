"""FastAPI-зависимости для роутов, требующих залогиненного пользователя."""

from fastapi import Request, HTTPException


def get_current_user(request: Request) -> dict:
    user = request.session.get("user")
    if not user:
        raise HTTPException(status_code=401, detail="Не авторизованы — войдите через /login")
    return user


def require_officer_access(request: Request) -> dict:
    """Как get_current_user, но также требует tier="officer" — веб-дашборд
    доступен только офицерам/лидерам (игровой ранг из Comlink, либо супер-админ,
    либо ручной грант уровня officer — см. guild_resolver.resolve_access).
    ВАЖНО: проверяем именно user["tier"], а не user["guild_id"] — у MEMBER-уровня
    тоже резолвится guild_id (для скоупа команд бота), но веб им не положен."""
    user = get_current_user(request)
    if user.get("tier") != "officer":
        raise HTTPException(status_code=403, detail="Доступ к веб-дашборду есть только у офицеров/лидеров гильдии")
    return user


# Старое имя — оставлено алиасом, чтобы не трогать импорты в web/routes/*.py.
require_guild_access = require_officer_access


def require_super_admin(request: Request) -> dict:
    user = get_current_user(request)
    if not user.get("is_super_admin"):
        raise HTTPException(status_code=403, detail="Доступ только для супер-админов")
    return user
