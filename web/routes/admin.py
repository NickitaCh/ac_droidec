from pathlib import Path
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

import database
from command_catalog import COMMAND_GROUPS
from services import feature_flags, fun_features, tb_schedule
from services.guild_admin import (
    add_grant,
    add_guild,
    add_super_admin,
    add_web_credential,
    deactivate_guild,
    list_admins,
    list_guilds,
    list_web_credentials,
    remove_grant,
    remove_super_admin,
    remove_web_credential,
    set_web_credential_password,
)
from web.deps import require_super_admin

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent.parent / "templates"))


def _get_comlink():
    # См. web/routes/registration.py::_get_comlink — веб-процесс не поднимает
    # диска-клиента, строит свой SwgohComlink на тот же comlink-сайдкар.
    from swgoh_comlink import SwgohComlink
    return SwgohComlink(url="http://localhost:3000")


@router.get("/guilds", response_class=HTMLResponse)
async def guilds_page(request: Request, user: dict = Depends(require_super_admin)):
    return templates.TemplateResponse(request, "admin_guilds.html", {
        "user": user,
        "guilds": list_guilds(active_only=False),
        "error": request.query_params.get("error"),
    })


# =====================================================================
# Переключатель "войти как гильдия N" в шапке (base.html, только для
# супер-админов) — см. web/deps.py::_apply_guild_switch за тем, как это влияет
# на guild_id/tier во всех остальных роутах. Здесь только пишем/чистим
# session["impersonate_guild_id"] и возвращаем на ту же страницу.
# =====================================================================
@router.post("/switch-guild", response_class=RedirectResponse)
async def switch_guild(request: Request, guild_id: str = Form(...), user: dict = Depends(require_super_admin)):
    if guild_id == "__self__":
        request.session.pop("impersonate_guild_id", None)
    else:
        try:
            gid = int(guild_id)
        except ValueError:
            gid = None
        if gid is None or not any(g["id"] == gid for g in list_guilds(active_only=True)):
            return RedirectResponse(f"/admin/guilds?{urlencode({'error': 'Некорректная гильдия.'})}", status_code=303)
        request.session["impersonate_guild_id"] = gid
    referer = request.headers.get("referer") or "/"
    return RedirectResponse(referer, status_code=303)


@router.post("/guilds/add", response_class=HTMLResponse)
async def guilds_add(
    request: Request,
    ally_code: str = Form(...),
    discord_guild_id: str = Form(...),
    user: dict = Depends(require_super_admin),
):
    comlink = _get_comlink()
    result = await add_guild(comlink, ally_code, discord_guild_id)
    if not result.ok:
        return RedirectResponse(f"/admin/guilds?{urlencode({'error': result.error})}", status_code=303)
    return RedirectResponse("/admin/guilds", status_code=303)


@router.post("/guilds/{guild_id}/deactivate", response_class=HTMLResponse)
async def guilds_deactivate(guild_id: int, user: dict = Depends(require_super_admin)):
    deactivate_guild(guild_id)
    return RedirectResponse("/admin/guilds", status_code=303)


@router.get("/access", response_class=HTMLResponse)
async def access_page(request: Request, user: dict = Depends(require_super_admin)):
    data = list_admins()
    # Голые discord_id/guild_id нечитаемы для человека — резолвим в имена здесь,
    # а не в шаблоне (Jinja без доступа к БД). guild_names — по ВСЕМ гильдиям
    # (не только активным), т.к. грант может указывать на уже деактивированную.
    guild_names = {g["id"]: g["name"] for g in list_guilds(active_only=False)}
    super_admins = [
        {**a, "added_by_name": database.get_username_for_discord_id(a["added_by"])}
        for a in data["super_admins"]
    ]
    grants = [
        {
            **g,
            "granted_by_name": database.get_username_for_discord_id(g["granted_by"]),
            "guild_name": guild_names.get(g["guild_id"]),
        }
        for g in data["grants"]
    ]
    return templates.TemplateResponse(request, "admin_access.html", {
        "user": user,
        "super_admins": super_admins,
        "grants": grants,
        "guilds": list_guilds(active_only=True),
        "error": request.query_params.get("error"),
    })


@router.post("/access/super-admins/add", response_class=HTMLResponse)
async def super_admins_add(
    discord_id: str = Form(...),
    username: str = Form(""),
    user: dict = Depends(require_super_admin),
):
    add_super_admin(discord_id, username or None, user["discord_id"])
    return RedirectResponse("/admin/access", status_code=303)


@router.post("/access/super-admins/{discord_id}/remove", response_class=HTMLResponse)
async def super_admins_remove(discord_id: str, user: dict = Depends(require_super_admin)):
    remove_super_admin(discord_id)
    return RedirectResponse("/admin/access", status_code=303)


@router.post("/access/grants/add", response_class=HTMLResponse)
async def grants_add(
    request: Request,
    discord_id: str = Form(...),
    ally_code: str = Form(...),
    tier: str = Form(...),
    guild_id: int = Form(...),
    user: dict = Depends(require_super_admin),
):
    comlink = _get_comlink()
    result = await add_grant(comlink, discord_id, ally_code, guild_id, tier, user["discord_id"])
    if not result.ok:
        return RedirectResponse(f"/admin/access?{urlencode({'error': result.error})}", status_code=303)
    return RedirectResponse("/admin/access", status_code=303)


@router.post("/access/grants/{discord_id}/remove", response_class=HTMLResponse)
async def grants_remove(discord_id: str, user: dict = Depends(require_super_admin)):
    remove_grant(discord_id)
    return RedirectResponse("/admin/access", status_code=303)


@router.get("/web-accounts", response_class=HTMLResponse)
async def web_accounts_page(request: Request, user: dict = Depends(require_super_admin)):
    # Голый discord_id нечитаем — резолвим в лучшее известное имя тем же
    # способом, что и остальные /admin-страницы (database.get_username_for_discord_id).
    accounts = [
        {
            **a,
            "player_name": database.get_username_for_discord_id(a["discord_id"]),
            "created_by_name": database.get_username_for_discord_id(a["created_by"]) if a["created_by"] else None,
        }
        for a in list_web_credentials()
    ]
    return templates.TemplateResponse(request, "admin_web_accounts.html", {
        "user": user,
        "accounts": accounts,
        "error": request.query_params.get("error"),
    })


@router.post("/web-accounts/add", response_class=HTMLResponse)
async def web_accounts_add(
    login: str = Form(...),
    discord_id: str = Form(...),
    password: str = Form(...),
    user: dict = Depends(require_super_admin),
):
    result = add_web_credential(login, discord_id, password, user["discord_id"])
    if not result.ok:
        return RedirectResponse(f"/admin/web-accounts?{urlencode({'error': result.error})}", status_code=303)
    return RedirectResponse("/admin/web-accounts", status_code=303)


@router.post("/web-accounts/{login}/password", response_class=HTMLResponse)
async def web_accounts_set_password(login: str, password: str = Form(...), user: dict = Depends(require_super_admin)):
    result = set_web_credential_password(login, password)
    if not result.ok:
        return RedirectResponse(f"/admin/web-accounts?{urlencode({'error': result.error})}", status_code=303)
    return RedirectResponse("/admin/web-accounts", status_code=303)


@router.post("/web-accounts/{login}/delete", response_class=HTMLResponse)
async def web_accounts_delete(login: str, user: dict = Depends(require_super_admin)):
    remove_web_credential(login)
    return RedirectResponse("/admin/web-accounts", status_code=303)


@router.get("/access-log", response_class=HTMLResponse)
async def access_log_page(request: Request, user: dict = Depends(require_super_admin)):
    return templates.TemplateResponse(request, "admin_access_log.html", {
        "user": user,
        "entries": database.get_web_access_log(limit=200),
    })


@router.get("/command-usage", response_class=HTMLResponse)
async def command_usage_page(request: Request, user: dict = Depends(require_super_admin)):
    # По умолчанию — сумма по всем гильдиям (как было исходно). ?guild_id=<id> —
    # только эта гильдия, ?guild_id=0 — sentinel-бакет "гильдия не определена"
    # (см. database.py::_ensure_command_usage_table) — по прямому запросу
    # пользователя 2026-09-14, супер-админам нужна не только общая статистика, но
    # и разбивка по гильдии.
    guild_id_param = request.query_params.get("guild_id")
    selected_guild_id: int | None = None
    if guild_id_param not in (None, "", "all"):
        try:
            selected_guild_id = int(guild_id_param)
        except ValueError:
            selected_guild_id = None

    if selected_guild_id is None:
        usage = database.get_command_usage_counts()
    else:
        usage = database.get_command_usage_counts_by_guild(selected_guild_id)

    known_names = {name for _, cmds in COMMAND_GROUPS for name, _ in cmds}
    groups = [
        {
            "title": title,
            "rows": [
                {
                    "name": name,
                    "description": description,
                    "count": usage.get(name, {}).get("count", 0),
                    "last_used_at": usage.get(name, {}).get("last_used_at"),
                }
                for name, description in cmds
            ],
        }
        for title, cmds in COMMAND_GROUPS
    ]
    # Команды, вызванные ботом, но отсутствующие в каталоге (забыли добавить в
    # command_catalog.py при новой команде, либо команда с тех пор удалена) —
    # отдельной группой в конце, чтобы расхождение было видно, а не потеряно.
    unknown_rows = [
        {"name": name, "description": None, "count": data["count"], "last_used_at": data["last_used_at"]}
        for name, data in usage.items()
        if name not in known_names
    ]
    if unknown_rows:
        groups.append({"title": "Не в каталоге (проверьте command_catalog.py)", "rows": unknown_rows})

    # Все подключённые гильдии, не только те, у кого уже есть залогированные вызовы —
    # супер-админ должен видеть весь список сразу (прямой запрос пользователя
    # 2026-09-14: "остальные подключённые? супер админы должны видеть все гильдии"),
    # выбор гильдии без вызовов просто покажет нули по всем командам.
    guild_options = [
        {"id": g["id"], "name": g["name"]}
        for g in database.get_all_guild_configs(active_only=True)
    ]
    if database.get_command_usage_counts_by_guild(0):
        guild_options.append({"id": 0, "name": "Гильдия не определена (ЛС и т.п.)"})

    return templates.TemplateResponse(request, "admin_command_usage.html", {
        "user": user,
        "groups": groups,
        "total_calls": sum(r["count"] for g in groups for r in g["rows"]),
        "guild_options": guild_options,
        "selected_guild_id": selected_guild_id,
    })


# =====================================================================
# Шуточные фичи бота (services/fun_features.py) — вкл/выкл, зеркало /фан.
# Сознательно НЕ добавлено в command_catalog.py/публичный /commands (в отличие
# от остальных команд) — та страница отдаётся вообще без авторизации (см.
# память проекта), а это розыгрыш конкретного человека, не стоит его спойлерить
# там же, где он сам может увидеть список.
# =====================================================================
@router.get("/fun", response_class=HTMLResponse)
async def fun_page(request: Request, user: dict = Depends(require_super_admin)):
    return templates.TemplateResponse(request, "admin_fun.html", {
        "user": user,
        "actions": fun_features.all_actions_with_status(),
    })


@router.post("/fun/{action_key}/enable", response_class=HTMLResponse)
async def fun_enable(action_key: str, user: dict = Depends(require_super_admin)):
    fun_features.set_enabled(action_key, True, user["discord_id"])
    return RedirectResponse("/admin/fun", status_code=303)


@router.post("/fun/{action_key}/disable", response_class=HTMLResponse)
async def fun_disable(action_key: str, user: dict = Depends(require_super_admin)):
    fun_features.set_enabled(action_key, False, user["discord_id"])
    return RedirectResponse("/admin/fun", status_code=303)


# =====================================================================
# ТБ-расписание, бот-вайд (не per-guild): якорь чётности "тегаемой" недели +
# DST-коррекция МСК-триггеров (см. services/tb_schedule.py за подробным
# объяснением, почему это не per-guild настройка). Смещение вводится как
# знаковое число минут, без готовых лейблов "лето/зима" — супер-админ сам
# определяет знак и величину, глядя на реальный сдвиг в игре.
# =====================================================================
@router.get("/tb-schedule", response_class=HTMLResponse)
async def tb_schedule_page(request: Request, user: dict = Depends(require_super_admin)):
    return templates.TemplateResponse(request, "admin_tb_schedule.html", {
        "user": user,
        "anchor_date": tb_schedule.get_week_anchor_date().isoformat(),
        "current_offset": tb_schedule.get_dst_offset_minutes(),
        "effective_offset": tb_schedule.effective_dst_offset_minutes(),
        "pending": tb_schedule.get_pending_dst_change(),
        "error": request.query_params.get("error"),
        "saved": request.query_params.get("saved"),
    })


@router.post("/tb-schedule/anchor", response_class=HTMLResponse)
async def tb_schedule_set_anchor(
    request: Request,
    anchor_date: str = Form(...),
    user: dict = Depends(require_super_admin),
):
    try:
        tb_schedule.set_week_anchor_date(anchor_date.strip(), user["discord_id"])
    except ValueError:
        return RedirectResponse(
            f"/admin/tb-schedule?{urlencode({'error': 'Дата должна быть в формате ГГГГ-ММ-ДД'})}", status_code=303
        )
    return RedirectResponse("/admin/tb-schedule?saved=1", status_code=303)


@router.post("/tb-schedule/dst/schedule", response_class=HTMLResponse)
async def tb_schedule_schedule_dst(
    request: Request,
    effective_date: str = Form(...),
    offset_minutes: int = Form(...),
    note: str = Form(""),
    user: dict = Depends(require_super_admin),
):
    if not (-180 <= offset_minutes <= 180):
        return RedirectResponse(
            f"/admin/tb-schedule?{urlencode({'error': 'Смещение должно быть в пределах ±180 минут'})}", status_code=303
        )
    try:
        tb_schedule.schedule_dst_change(effective_date.strip(), offset_minutes, user["discord_id"], note.strip())
    except ValueError:
        return RedirectResponse(
            f"/admin/tb-schedule?{urlencode({'error': 'Дата должна быть в формате ГГГГ-ММ-ДД'})}", status_code=303
        )
    return RedirectResponse("/admin/tb-schedule?saved=1", status_code=303)


@router.post("/tb-schedule/dst/cancel", response_class=HTMLResponse)
async def tb_schedule_cancel_dst(user: dict = Depends(require_super_admin)):
    tb_schedule.cancel_pending_dst_change()
    return RedirectResponse("/admin/tb-schedule", status_code=303)


# =====================================================================
# Фиче-тумблеры по гильдиям (services/feature_flags.py) — матрица гильдия×фича,
# один общий <form> на всю таблицу (по образцу /admin/fun, но сразу батчем на
# все ячейки, а не по одной кнопке на переключение — при 6+ гильдиях и 15
# фичах отдельная кнопка на каждую пару была бы неюзабельна).
# =====================================================================
@router.get("/features", response_class=HTMLResponse)
async def features_page(request: Request, user: dict = Depends(require_super_admin)):
    guilds = list_guilds(active_only=True)
    rows = feature_flags.matrix([g["id"] for g in guilds])
    categories = {}
    for row in rows:
        categories.setdefault(row["category"], []).append(row)
    return templates.TemplateResponse(request, "admin_features.html", {
        "user": user,
        "guilds": guilds,
        "categories": categories,
        "saved": request.query_params.get("saved"),
    })


@router.post("/features/save", response_class=HTMLResponse)
async def features_save(request: Request, user: dict = Depends(require_super_admin)):
    form = await request.form()
    checked = set(form.keys())  # "{guild_id}:{key}" только для отмеченных чекбоксов
    guilds = list_guilds(active_only=True)
    for guild in guilds:
        gid = guild["id"]
        for key in feature_flags.FEATURES:
            want_enabled = f"{gid}:{key}" in checked
            if feature_flags.is_enabled(gid, key) != want_enabled:
                feature_flags.set_enabled(gid, key, want_enabled, user["discord_id"])
    return RedirectResponse("/admin/features?saved=1", status_code=303)
