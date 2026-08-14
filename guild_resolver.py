"""Резолв SWGOH-гильдии по Discord-ролям автора взаимодействия — общая логика
для cogs бота и (позже) веб-дашборда. Не импортирует disnake/main — работает
с любым объектом/списком, у которого можно достать сырые id ролей, чтобы
код был переиспользуем и в FastAPI-роутах (там ролей достают HTTP-запросом
к Discord, а не из disnake.Member)."""

import database


def _raw_role_ids(author) -> list:
    """Тот же паттерн, что и main.py::_check_allowed_role /
    cogs/self_registration.py::_is_officer (до их рефакторинга под этот модуль):
    берём сырые author._roles из пейлоада интеракции, а не author.roles —
    Interaction.guild может быть None для незакэшированных гильдий."""
    role_ids = getattr(author, "_roles", None)
    if role_ids is None:
        role_ids = [role.id for role in getattr(author, "roles", [])]
    return list(role_ids)


def resolve_guild_id_from_roles(role_ids) -> int | None:
    """Чистая версия резолва: на входе список/множество id ролей (int),
    без привязки к disnake.Member — переиспользуется веб-дашбордом, который
    получает роли пользователя отдельным HTTP-запросом к Discord."""
    role_id_set = {int(r) for r in role_ids}
    matches = []
    for cfg in database.get_all_guild_configs():
        try:
            member_role = int(cfg["member_role_id"])
            officer_role = int(cfg["officer_role_id"])
        except (TypeError, ValueError):
            continue
        if member_role in role_id_set or officer_role in role_id_set:
            matches.append(cfg["id"])
    if not matches:
        return None
    if len(matches) > 1:
        print(f"⚠️ [guild_resolver] Роли {sorted(role_id_set)} подходят под несколько гильдий {matches} — беру минимальный id")
    return min(matches)


def resolve_guild_id(author) -> int | None:
    return resolve_guild_id_from_roles(_raw_role_ids(author))


def resolve_officer_guild_id_from_roles(role_ids) -> int | None:
    """Как resolve_guild_id_from_roles, но матчит только officer_role_id (не
    member_role_id) — используется веб-дашбордом: доступ туда даём только
    офицерам гильдии, в отличие от слэш-команд бота, где member_role_id тоже
    достаточно (см. web/auth.py::callback)."""
    role_id_set = {int(r) for r in role_ids}
    matches = []
    for cfg in database.get_all_guild_configs():
        try:
            officer_role = int(cfg["officer_role_id"])
        except (TypeError, ValueError):
            continue
        if officer_role in role_id_set:
            matches.append(cfg["id"])
    if not matches:
        return None
    if len(matches) > 1:
        print(f"⚠️ [guild_resolver] Офицерские роли {sorted(role_id_set)} подходят под несколько гильдий {matches} — беру минимальный id")
    return min(matches)


def is_officer_for_resolved_guild(author) -> bool:
    guild_id = resolve_guild_id(author)
    if guild_id is None:
        return False
    cfg = database.get_guild_config(guild_id)
    if not cfg:
        return False
    try:
        officer_role = int(cfg["officer_role_id"])
    except (TypeError, ValueError):
        return False
    return officer_role in {int(r) for r in _raw_role_ids(author)}


def all_registered_role_ids() -> list:
    """Объединение всех member_role_id/officer_role_id по всем гильдиям —
    только для диагностики (например, аргумент commands.MissingAnyRole)."""
    ids = set()
    for cfg in database.get_all_guild_configs():
        for key in ("member_role_id", "officer_role_id"):
            try:
                ids.add(int(cfg[key]))
            except (TypeError, ValueError):
                pass
    return sorted(ids)
