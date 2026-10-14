"""Статус платной подписки гильдии для сайта (2026-10-14):
- warning_for — баннер офицерам и лидеру за 7 дней до окончания (с 3 дней — красный);
- expired_officer_guild — у офицера/лидера, чья гильдия была подключена, но подписка
  истекла (гильдия отключена, сайт его уже «не знает»): данные для попапа «Продлить».
Считается на каждый запрос в web/deps.py, сами запросы дешёвые (sqlite)."""

import math
from datetime import datetime, timezone

import database
from services.payments import PERIOD_CHOICES

WARN_DAYS = 7
URGENT_DAYS = 3
OFFICER_LEVELS = (3, 4)  # user_mapping.member_level: 3 — офицер, 4 — лидер


def _parse(expires_at: str | None) -> datetime | None:
    if not expires_at:
        return None
    try:
        return datetime.strptime(expires_at[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _prices() -> list[dict]:
    return [{"days": days, "label": "Месяц" if days == 30 else "Год", "price": price}
            for days, price in sorted(PERIOD_CHOICES.items())]


def info_for(guild_id: int | None) -> dict | None:
    """Срок подписки для виджета в меню: {"unlimited": True} у гильдий, подключённых
    вручную бессрочно, иначе {"days_left", "date", "urgent"}. None — гильдии нет/не активна."""
    if guild_id is None:
        return None
    cfg = database.get_guild_config(guild_id) or {}
    if not cfg.get("is_active"):
        return None
    expires = _parse(cfg.get("subscription_expires_at"))
    if expires is None:
        return {"unlimited": True}
    seconds_left = (expires - datetime.now(timezone.utc)).total_seconds()
    days_left = max(1, math.ceil(seconds_left / 86400))
    return {"unlimited": False, "days_left": days_left, "date": expires.strftime("%d.%m.%Y"),
            "urgent": days_left <= URGENT_DAYS}


def warning_for(guild_id: int | None) -> dict | None:
    """{"days_left", "date", "urgent"} — если до конца оплаченной подписки ≤ 7 дней.
    Бессрочные (ручные, без subscription_expires_at) гильдии не предупреждаются."""
    if guild_id is None:
        return None
    cfg = database.get_guild_config(guild_id) or {}
    expires = _parse(cfg.get("subscription_expires_at"))
    if expires is None or not cfg.get("is_active"):
        return None
    seconds_left = (expires - datetime.now(timezone.utc)).total_seconds()
    if seconds_left > WARN_DAYS * 86400:
        return None
    days_left = max(1, math.ceil(seconds_left / 86400))  # «осталось 6 дн.», а не 5 дн. 23 ч
    return {"days_left": days_left, "date": expires.strftime("%d.%m.%Y"), "urgent": days_left <= URGENT_DAYS}


def expired_officer_guild(discord_id: str) -> dict | None:
    """Гильдия с истёкшей подпиской, где этот пользователь (по последнему известному
    ростеру) был офицером или лидером. None — нет такой."""
    for guild_id, (ally_code, _name) in database.get_registrations_for_discord_id(str(discord_id)).items():
        cfg = database.get_guild_config(guild_id) or {}
        if cfg.get("is_active") or not cfg.get("subscription_expires_at"):
            continue
        if database.get_member_level(guild_id, ally_code) not in OFFICER_LEVELS:
            continue
        expires = _parse(cfg["subscription_expires_at"])
        return {"id": guild_id, "name": cfg.get("name"),
                "expired_date": expires.strftime("%d.%m.%Y") if expires else None,
                "prices": _prices()}
    return None
