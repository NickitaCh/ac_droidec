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


VIEW_AS_LABELS = {"officer": "Офицер", "member": "Участник"}


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
    from services import bot_profiles
    profile = request.session.get("bot_profile")
    if profile not in bot_profiles.PROFILES:
        profile = bot_profiles.guild_profile(user.get("guild_id"))
    guilds = [g for g in list_guilds(active_only=True, all_profiles=True)
              if (g.get("bot_profile") or "test") == profile]
    result = {**user, "own_guild_id": user.get("guild_id"), "switchable_guilds": guilds,
              "bot_profile": profile,
              "own_guild_in_profile": any(g["id"] == user.get("guild_id") for g in guilds)}
    override = request.session.get("impersonate_guild_id")
    if override is not None and not any(g["id"] == override for g in guilds):
        override = None
    if not any(g["id"] == result.get("guild_id") for g in guilds):
        result["guild_id"] = guilds[0]["id"] if guilds else None
        result["tier"] = "officer"
    if override is not None and override != user.get("guild_id"):
        result["guild_id"] = override
        result["tier"] = "officer"
    # «Смотреть как» (/admin/view-as): супер-админ видит сайт глазами офицера или
    # участника — без супер-админских пунктов и страниц (они отдают 403, как настоящим).
    # real_super_admin остаётся True, чтобы сам переключатель и выбор гильдии работали.
    result["real_super_admin"] = True
    view_as = request.session.get("view_as")
    if view_as in VIEW_AS_LABELS and result.get("guild_id") is not None:
        result["view_as"] = view_as
        result["is_super_admin"] = False
        result["tier"] = view_as
    return result


def _with_subscription_status(user: dict) -> dict:
    """Офицерам — предупреждение о скором конце подписки (баннер в base.html);
    офицеру/лидеру гильдии с уже истёкшей подпиской — данные для попапа «Продлить»."""
    from services import subscription_status
    if user.get("tier") == "officer":
        return {**user, "subscription_warning": subscription_status.warning_for(user.get("guild_id")),
                "subscription": subscription_status.info_for(user.get("guild_id"))}
    if user.get("tier") == "member":
        # Участнику — только виджет «сколько осталось» (продлевают офицеры).
        return {**user, "subscription": subscription_status.info_for(user.get("guild_id"))}
    if user.get("tier") is None:
        return {**user, "expired_guild": subscription_status.expired_officer_guild(user["discord_id"])}
    return user


def get_current_user(request: Request) -> dict:
    user = request.session.get("user")
    if not user and request.session.get("demo"):
        # Демо-режим (services/demo_mode.py): офицер выдуманной гильдии, база — demo.db.
        from services import demo_mode
        from services import bot_profiles
        result = demo_mode.demo_user()
        if not bot_profiles.path_available(request.url.path, result["bot_profile"]):
            raise HTTPException(status_code=404, detail="Раздел недоступен.")
        return result
    if not user:
        raise HTTPException(status_code=401, detail="Не авторизованы — войдите через /login")
    result = _apply_guild_switch(request, _refresh_access(user))
    from services import bot_profiles
    profile = result.get("bot_profile")
    if profile not in bot_profiles.PROFILES:
        profile = bot_profiles.guild_profile(result["guild_id"]) if result.get("guild_id") is not None else request.session.get("login_bot_profile", bot_profiles.public_profile())
    result["bot_profile"] = profile
    result["prod_disabled_features"] = bot_profiles.PROD_DISABLED if profile == "prod" else ()
    if result.get("real_super_admin") and result.get("guild_id") is None and not request.url.path.startswith("/admin/") and request.url.path != "/logout":
        raise HTTPException(status_code=307, headers={"Location": "/admin/guilds"})
    if not bot_profiles.path_available(request.url.path, profile):
        raise HTTPException(status_code=404, detail="Раздел недоступен.")
    return _with_subscription_status(result)


def get_current_user_optional(request: Request) -> dict | None:
    """Как get_current_user, но None вместо 401 для незалогиненных — для страниц вроде
    главной, у которых есть гостевой вид. Залогиненным даёт то же самое (включая
    switchable_guilds для супер-админов), просто без обязательной авторизации."""
    if not request.session.get("user") and not request.session.get("demo"):
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


def require_member_access(request: Request) -> dict:
    """Участник или офицер подключённой гильдии — только для «Моего профиля»
    (карточка игрока на своих аккаунтах, 2026-10-14). Остальной сайт — офицерам."""
    user = get_current_user(request)
    if user.get("tier") not in ("officer", "member") or user.get("guild_id") is None:
        raise HTTPException(status_code=403, detail="Страница доступна участникам подключённых гильдий")
    return user


def own_accounts(user: dict) -> list:
    """[(ally_code, ingame_name, is_main), ...] — свои аккаунты из /регистрация в текущей гильдии."""
    import database
    return database.get_user_registrations(str(user["discord_id"]), guild_id=user["guild_id"])


def require_real_super_admin(request: Request) -> dict:
    """Супер-админ даже в режиме «смотреть как» — для самого переключателя и выбора гильдии."""
    user = get_current_user(request)
    if not user.get("real_super_admin"):
        raise HTTPException(status_code=403, detail="Доступ только для супер-админов")
    return user


def require_super_admin(request: Request) -> dict:
    user = get_current_user(request)
    if not user.get("is_super_admin"):
        raise HTTPException(status_code=403, detail="Доступ только для супер-админов")
    return user
