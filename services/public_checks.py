"""Личные проверки по регистрации, без выдачи доступа к данным гильдии."""
import database
import guild_resolver


COMMANDS = {"юнит", "готовность", "регистрация_отмена"}
# Это область хранения личных регистраций, а не подключённая гильдия.
REGISTRATION_GUILD_ID = 0


def registered(author_or_id) -> bool:
    return database.discord_id_has_any_registration(guild_resolver._discord_id(author_or_id))


def account(author_or_id, alt: bool = False) -> dict | None:
    """Основной/второй аккаунт, включая регистрации вне подключённых гильдий."""
    access = guild_resolver.resolve_access(author_or_id)
    rows = database.get_all_registrations_for_discord_id(guild_resolver._discord_id(author_or_id))
    default_code = access["ally_code"]
    rows.sort(key=lambda r: (r[1] != default_code, r[0] != access["guild_id"], not r[3], r[0], r[1]))
    unique = {}
    for row in rows:
        unique.setdefault(row[1], row)
    rows = list(unique.values())
    if alt:
        rows = rows[1:]
    if not rows:
        return None
    gid, code, name, _main = rows[0]
    return {"guild_id": gid, "ally_code": code, "name": name}
