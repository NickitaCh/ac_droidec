"""FastAPI-зависимости для роутов, требующих залогиненного пользователя."""

from fastapi import Request, HTTPException


def get_current_user(request: Request) -> dict:
    user = request.session.get("user")
    if not user:
        raise HTTPException(status_code=401, detail="Не авторизованы — войдите через /login")
    return user


def require_guild_access(request: Request) -> dict:
    """Как get_current_user, но также требует, чтобы у пользователя резолвилась
    гильдия (есть member/officer роль хотя бы одной зарегистрированной гильдии)."""
    user = get_current_user(request)
    if user.get("guild_id") is None:
        raise HTTPException(status_code=403, detail="Не удалось определить вашу гильдию — нет нужной роли ни в одном зарегистрированном Discord-сервере")
    return user
