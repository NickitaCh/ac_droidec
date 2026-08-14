"""FastAPI-зависимости для роутов, требующих залогиненного пользователя."""

from fastapi import Request, HTTPException


def get_current_user(request: Request) -> dict:
    user = request.session.get("user")
    if not user:
        raise HTTPException(status_code=401, detail="Не авторизованы — войдите через /login")
    return user


def require_guild_access(request: Request) -> dict:
    """Как get_current_user, но также требует, чтобы у пользователя резолвилась
    гильдия — веб-дашборд доступен только офицерам (officer_role_id гильдии),
    в отличие от слэш-команд бота, где хватает member_role_id (см. web/auth.py)."""
    user = get_current_user(request)
    if user.get("guild_id") is None:
        raise HTTPException(status_code=403, detail="Доступ к веб-дашборду есть только у офицеров гильдии — нет офицерской роли ни в одном зарегистрированном Discord-сервере")
    return user
