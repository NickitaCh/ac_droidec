"""FastAPI-зависимости для роутов, требующих залогиненного пользователя."""

from fastapi import Request, HTTPException

import guild_resolver


def _refresh_access(user: dict) -> dict:
    """Перерезолвливает tier/guild_id/is_super_admin живьём на каждый запрос,
    а не доверяет значению, записанному в сессию при логине.

    Раньше это было главной дырой в инциденте "кикнутый офицер ещё какое-то
    время мог пользоваться веб-панелью": tier клался в request.session один
    раз при OAuth-логине (web/auth.py) и потом просто читался как есть —
    SessionMiddleware (web/app.py) создан без max_age, то есть у Starlette
    это дефолтные 14 дней сессии, всё это время доступ не перепроверялся
    вообще. Теперь единственное окно устаревания — это интервал синка
    ростера (ViolationsCog.update_roster_cache, см. cogs/violations.py),
    а не срок жизни сессии."""
    fresh = guild_resolver.resolve_access(user["discord_id"])
    return {**user, "guild_id": fresh["guild_id"], "tier": fresh["tier"], "is_super_admin": fresh["is_super_admin"]}


def _apply_guild_switch(request: Request, user: dict) -> dict:
    """Даёт супер-админам временно "переключиться" на дашборд другой гильдии
    (см. /admin/switch-guild POST), не трогая исходную сессию логина —
    переключение живёт в отдельном session-ключе (impersonate_guild_id), так
    что выход из режима переключения не требует релогина. Список гильдий для
    переключателя в шапке (base.html) кладём прямо в user, а не через
    отдельный Jinja context processor — у каждого web/routes/*.py свой
    Jinja2Templates(...), общего request-scoped контекста для всех шаблонов нет,
    а user и так уже передаётся в контекст всех защищённых страниц."""
    if not user.get("is_super_admin"):
        return user
    from services.guild_admin import list_guilds
    # own_guild_id — что резолвится этому супер-админу по его собственной
    # регистрации/гранту (может быть None) — сохраняем отдельно, чтобы
    # переключатель в шапке (base.html) мог показать "— своя гильдия —" как
    # текущий выбор, даже после переключения guild_id на другую гильдию ниже.
    result = {**user, "own_guild_id": user.get("guild_id"), "switchable_guilds": list_guilds(active_only=True)}
    override = request.session.get("impersonate_guild_id")
    if override is not None and override != user.get("guild_id"):
        result["guild_id"] = override
        result["tier"] = "officer"
    return result


def get_current_user(request: Request) -> dict:
    user = request.session.get("user")
    if not user:
        raise HTTPException(status_code=401, detail="Не авторизованы — войдите через /login")
    return _apply_guild_switch(request, _refresh_access(user))


def get_current_user_optional(request: Request) -> dict | None:
    """Как get_current_user, но None вместо 401 для незалогиненных — для страниц вроде
    главной, у которых есть гостевой вид. Залогиненным даёт то же самое (включая
    switchable_guilds для супер-админов), просто без обязательной авторизации."""
    if not request.session.get("user"):
        return None
    return get_current_user(request)


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
