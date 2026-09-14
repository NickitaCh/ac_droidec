"""Веб-версия самообслуживания подписки — /subscribe (веб-аналог Discord-команды
`/гильдия_заявка`, cogs/guild_subscription.py). Существовала только в Discord —
онбординг-тексты (services/dashboard_data.py::access_status_message,
main.py::_access_denied_message) годами обещали "подключить и оплатить можно
на сайте", а реальной страницы для этого не было, только ссылка на корень
сайта — по прямому запросу пользователя 2026-09-14 заведена настоящая страница.

Два независимых режима на одной странице (два отдельных <form>, см. шаблон):
- "продлить" — залогиненный офицер уже подключённой/активной гильдии
  (user.tier=="officer", user.guild_id известен): просто период, гильдия уже
  есть — build_payment_link по существующему guild_id, ничего не заводим заново;
- "новая заявка" — все остальные (member/None-tier — гильдия ещё не подключена,
  либо подписка истекла и resolve_access её больше не видит, см. guild_resolver.py:
  _best_registration перебирает только активные гильдии): тот же путь, что у
  Discord-команды — services.guild_admin.add_guild(..., is_active=False), затем
  build_payment_link. Discord OAuth тут только identify-скоуп, список серверов
  пользователя не получить без отдельного разрешения — поэтому ID Discord-сервера
  вводится руками (тот же приём "ручной ID", что уже прижился в проекте, см.
  память feedback_curated_list_needs_manual_escape_hatch).

Доступ — Depends(get_current_user) (обязателен логин через Discord, чтобы знать,
кто инициировал), НЕ require_officer_access — по духу той же лёгкой проверки,
что и у самой Discord-команды (там вообще нет проверки роли/ранга, только
ALWAYS_ALLOWED_COMMANDS + inter.guild_id is not None)."""

from pathlib import Path

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

import database
from services.guild_admin import add_guild
from services.payments import PERIOD_CHOICES, build_payment_link
from web.deps import get_current_user

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent.parent / "templates"))

PERIOD_OPTIONS = [(30, "Месяц", PERIOD_CHOICES[30]), (365, "Год", PERIOD_CHOICES[365])]


def _get_comlink():
    # Как в остальных web/routes/*.py — веб-процесс не поднимает диска-клиента,
    # строит свой поверх того же comlink-сайдкара.
    from swgoh_comlink import SwgohComlink
    return SwgohComlink(url="http://localhost:3000")


def _renewal_guild(user: dict) -> dict | None:
    if user.get("tier") != "officer" or user.get("guild_id") is None:
        return None
    cfg = database.get_guild_config(user["guild_id"])
    if not cfg:
        return None
    return {"id": cfg["id"], "name": cfg["name"], "expires_at": cfg.get("subscription_expires_at")}


@router.get("/subscribe", response_class=HTMLResponse)
async def subscribe_form(request: Request, user: dict = Depends(get_current_user)):
    return templates.TemplateResponse(request, "subscribe.html", {
        "user": user,
        "period_options": PERIOD_OPTIONS,
        "renewal_guild": _renewal_guild(user),
        "result": None,
        "error": request.query_params.get("error"),
    })


@router.post("/subscribe", response_class=HTMLResponse)
async def subscribe_submit(
    request: Request,
    period: int = Form(...),
    ally_code: str = Form(default=""),
    discord_guild_id: str = Form(default=""),
    renew_guild_id: str = Form(default=""),
    user: dict = Depends(get_current_user),
):
    renewal_guild = _renewal_guild(user)
    context = {
        "user": user, "period_options": PERIOD_OPTIONS, "renewal_guild": renewal_guild,
        "result": None, "error": None,
    }

    if period not in PERIOD_CHOICES:
        context["error"] = "Некорректный период."
        return templates.TemplateResponse(request, "subscribe.html", context)

    if renew_guild_id and renewal_guild and str(renewal_guild["id"]) == renew_guild_id:
        guild_id, guild_name = renewal_guild["id"], renewal_guild["name"]
    else:
        clean_code = "".join(filter(str.isdigit, ally_code or ""))
        clean_discord_id = "".join(filter(str.isdigit, discord_guild_id or ""))
        if len(clean_code) != 9:
            context["error"] = "Код союзника должен состоять ровно из 9 цифр."
            return templates.TemplateResponse(request, "subscribe.html", context)
        if not clean_discord_id:
            context["error"] = "Укажите ID Discord-сервера вашей гильдии (ПКМ по иконке сервера → Копировать ID, нужен включённый режим разработчика в Discord)."
            return templates.TemplateResponse(request, "subscribe.html", context)

        comlink = _get_comlink()
        add_result = await add_guild(comlink, clean_code, clean_discord_id, is_active=False)
        if not add_result.ok:
            context["error"] = add_result.error
            return templates.TemplateResponse(request, "subscribe.html", context)
        guild_id, guild_name = add_result.guild_id, add_result.name

    link = await build_payment_link(guild_id, guild_name, period)
    if not link.ok:
        context["error"] = link.error
        return templates.TemplateResponse(request, "subscribe.html", context)

    context["result"] = {
        "guild_name": guild_name,
        "period_label": "1 месяц" if period == 30 else "1 год",
        "price": PERIOD_CHOICES[period],
        "url": link.url,
    }
    return templates.TemplateResponse(request, "subscribe.html", context)
