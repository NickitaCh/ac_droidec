"""Ссылка-приглашение бота на новый Discord-сервер — общая для бота (Discord-
ответы cogs/guild_subscription.py) и веб-дашборда (публичная посадочная
страница, web/templates/dashboard.html). Без зависимости от main.py/бота: сам
читает DISCORD_CLIENT_ID из .env, как и web/auth.py — этот модуль импортируется
и из процесса бота, и из процесса web.

Права в приглашении подобраны по тому, что бот реально делает (не "на всякий
случай"), и Discord сам создаёт роль бота с этими правами при добавлении —
никакого доп. кода на on_guild_join для САМОГО создания роли не нужно. Если
инвайтер всё же срежет права в диалоге Discord (или потом снимет их вручную) —
это ловит main.py::on_guild_join и просит их обратно, см. комментарий там."""

import os

import disnake
from services import bot_profiles

# Читается ЛЕНИВО (внутри build_invite_url, не как модульная константа) —
# main.py импортирует этот модуль ДО своего load_dotenv(), так что константа,
# посчитанная при импорте, навсегда осталась бы None в процессе бота, даже
# если .env реально содержит значение (реальный баг, найденный 2026-09-07:
# ошибка "не настроен DISCORD_CLIENT_ID" в личке бота при рабочем .env на
# VPS). web-процесс это не ловил, потому что web/app.py зовёт load_dotenv()
# раньше первого импорта роутов.

# view_channel/send_messages/embed_links/attach_files/read_message_history —
# базовые права на все embed-ответы и загрузку файлов (GuildEvents.send_as_file
# и т.п.) почти в каждом cogs/*.py. manage_roles — birthday.py выдаёт/снимает
# роль ко дню рождения (add_roles/remove_roles). manage_messages —
# antispam.py удаляет спам-сообщения. mention_everyone — пинги по
# ping_role_id/tb_order_role_id/antispam_alert_role_id могут указывать на роль,
# которая не отмечена "mentionable" в настройках самого Discord-сервера.
# moderate_members — antispam.py таймаутит подозрительные аккаунты.
REQUIRED_PERMISSIONS = disnake.Permissions(
    view_channel=True,
    send_messages=True,
    manage_messages=True,
    embed_links=True,
    attach_files=True,
    read_message_history=True,
    mention_everyone=True,
    manage_roles=True,
    moderate_members=True,
)


def build_invite_url(profile: str | None = None) -> str | None:
    profile = profile or bot_profiles.current_profile()
    client_id = os.getenv("PROD_DISCORD_CLIENT_ID" if profile == "prod" else "DISCORD_CLIENT_ID")
    if not client_id:
        return None
    return disnake.utils.oauth_url(
        client_id,
        permissions=required_permissions(profile),
        scopes=("bot", "applications.commands"),
    )


def missing_permissions(granted: disnake.Permissions) -> list[str]:
    """Человекочитаемые названия прав из REQUIRED_PERMISSIONS, которых нет в
    granted — используется main.py::on_guild_join, чтобы попросить конкретно
    недостающее, а не весь список заново."""
    missing = disnake.Permissions(required_permissions().value & ~granted.value)
    return [name.replace("_", " ") for name, value in missing if value]


def required_permissions(profile=None):
    permissions = disnake.Permissions(REQUIRED_PERMISSIONS.value)
    if (profile or bot_profiles.current_profile()) == "prod":
        permissions.manage_messages = False
        permissions.moderate_members = False
    return permissions
