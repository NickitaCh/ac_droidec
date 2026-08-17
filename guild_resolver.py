"""Резолв прав доступа (гильдия + уровень) по Discord ID — общая логика для cogs
бота и веб-дашборда. Источник правды — НЕ Discord-роли, а игровой ранг из
Comlink, закэшированный в user_mapping.member_level часовым ростер-кэшем
(cogs/violations.py::update_roster_cache): 4=лидер, 3=офицер, 2=рядовой
участник. Discord ID привязывается к ally_code через /регистрация
(user_registration, services/registration.py) — без регистрации доступа нет.

Три уровня (tier): "officer" (ранг 3/4, супер-админ, либо ручной грант уровня
officer) — полный доступ; "member" (ранг 2, либо ручной грант уровня member) —
доступ только к узкому списку команд (main.py::MEMBER_ACCESSIBLE_COMMANDS),
веб-дашборд недоступен; None — вообще никакого доступа.

Не импортирует disnake/main — работает с любым объектом, у которого можно
достать .id, либо с сырым discord_id (str/int), чтобы одинаково использоваться
и в cogs (inter.author), и в веб-роутах (discord_id из OAuth-сессии)."""

import database

_TIER_BY_LEVEL = {2: "member", 3: "officer", 4: "officer"}
_TIER_RANK = {"member": 1, "officer": 2}


def _discord_id(author_or_id) -> str:
    return str(getattr(author_or_id, "id", author_or_id))


def _tier_for_level(level: int | None) -> str | None:
    if level is None:
        return None
    return _TIER_BY_LEVEL.get(level)


def _best_registration(discord_id: str) -> dict | None:
    """Проходит по всем активным гильдиям, ищет привязанный (через /регистрация)
    ally_code этого discord_id и его закэшированный ранг в ростере той же
    гильдии. Если привязок несколько (альты в разных гильдиях) — побеждает
    более высокий tier, при равенстве — гильдия с меньшим id (тот же паттерн
    тай-брейка, что был у старого resolve_guild_id_from_roles)."""
    best = None
    for cfg in database.get_all_guild_configs():
        guild_id = cfg["id"]
        reg = database.get_user_registration(discord_id, guild_id=guild_id)
        if not reg:
            continue
        ally_code, _ingame_name = reg
        tier = _tier_for_level(database.get_member_level(guild_id, ally_code))
        if tier is None:
            # Зарегистрирован, но не найден в последнем ростер-кэше этой
            # гильдии (кикнут/вышел, либо кэш ещё не успел обновиться).
            continue
        rank = _TIER_RANK[tier]
        if best is None or rank > best["rank"] or (rank == best["rank"] and guild_id < best["guild_id"]):
            best = {"tier": tier, "guild_id": guild_id, "ally_code": ally_code, "rank": rank}
    return best


def resolve_access(author_or_id) -> dict:
    """Основная точка входа. Возвращает
    {"tier": "officer"|"member"|None, "guild_id": int|None,
     "is_super_admin": bool, "ally_code": str|None}.

    Порядок резолва:
      1. bot_admins -> tier="officer", is_super_admin=True (полный доступ везде;
         guild_id всё равно пытаемся определить через регистрацию/грант — для
         гильдие-скоупленных команд типа /статы_требования).
      2. user_registration + закэшированный ранг (user_mapping.member_level).
      3. Фолбэк, только если (2) ничего не дал — manual_access_grants (игроки
         вне участвующих гильдий, выданные супер-админом).
      4. Иначе tier=None, guild_id=None — доступа нет вообще."""
    discord_id = _discord_id(author_or_id)

    is_admin = database.is_bot_admin(discord_id)

    best = _best_registration(discord_id)
    if best is not None:
        tier = "officer" if is_admin else best["tier"]
        return {"tier": tier, "guild_id": best["guild_id"], "is_super_admin": is_admin, "ally_code": best["ally_code"]}

    grant = database.get_manual_grant(discord_id)
    if grant is not None:
        tier = "officer" if is_admin else grant["tier"]
        return {"tier": tier, "guild_id": grant["guild_id"], "is_super_admin": is_admin, "ally_code": grant["ally_code"]}

    if is_admin:
        return {"tier": "officer", "guild_id": None, "is_super_admin": True, "ally_code": None}

    return {"tier": None, "guild_id": None, "is_super_admin": False, "ally_code": None}


def resolve_guild_id(author) -> int | None:
    """Сигнатура не менялась — используется как раньше во всех cogs для скоупинга
    данных (warns/tasks/TB-отчёты/датакроны/статы и т.д.)."""
    return resolve_access(author)["guild_id"]


def is_officer_for_resolved_guild(author) -> bool:
    """Сигнатура не менялась — используется в @commands.check на офицерских
    сабкомандах (birthday.py, stat_requirements.py, datacron_requirements.py,
    guild_events.py, self_registration.py)."""
    return resolve_access(author)["tier"] == "officer"


def resolve_tier(author_or_id) -> str | None:
    """"officer" | "member" | None — используется глобальным гейтом в main.py."""
    return resolve_access(author_or_id)["tier"]


def is_super_admin(author_or_id) -> bool:
    """Прямая проверка bot_admins, без резолва гильдии/ранга — используется
    глобальным гейтом и /гильдия, /админы (cogs/admin_management.py)."""
    return database.is_bot_admin(_discord_id(author_or_id))
