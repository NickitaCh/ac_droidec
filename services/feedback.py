"""Единый приём обращений с сайта, Discord и Telegram."""
import database
from services import discord_rest

KINDS = {"bug": "Ошибка", "dislike": "Не нравится", "idea": "Идея"}
PER_HOUR = 10
MAX_MESSAGE = 4000


def create(owner_id, username, guild_id, message, kind="idea", **extra):
    message = message.strip()
    if not message:
        raise ValueError("Опишите, что хотите сообщить.")
    if len(message) > MAX_MESSAGE:
        raise ValueError("Текст обращения — до 4000 символов.")
    if database.count_recent_bug_reports(str(owner_id)) >= PER_HOUR:
        raise ValueError("Слишком много обращений за час — попробуйте позже.")
    return database.create_bug_report(
        discord_id=str(owner_id), username=username, guild_id=guild_id,
        kind=kind if kind in KINDS else "idea", message=message,
        page_url=extra.pop("page_url", ""), log_text=extra.pop("log_text", None),
        user_agent=extra.pop("user_agent", None), **extra,
    )


async def notify(report_id):
    report = database.get_bug_report(report_id)
    from services.payments import SITE_URL
    await discord_rest.notify_project_owner(
        f"📨 Обращение №{report_id} · {KINDS.get(report['kind'], 'Идея')} · {report['username'] or report['discord_id']}\n"
        + report["message"][:600]
        + f"\n{SITE_URL}/admin/feedback#report-{report_id}"
    )
