import asyncio
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

import database
from cogs.birthday import next_birthday
from services import activity_diff, dashboard_data, datacron_catalog, discord_invite
from services.config_status import missing_summary
from web.deps import get_current_user_optional, require_guild_access
# Переиспользуем форматирование "когда был последний синк" из /activity вместо
# дублирования — та же панель статуса, тот же смысл, см. docstring там.
from web.routes.guild_dashboard import _sync_status_text
from web.routes.tasks import _target_label as _task_target_label

MSK = ZoneInfo("Europe/Moscow")

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent.parent / "templates"))


def _get_comlink():
    # Тот же паттерн отдельного Comlink-клиента, что и в остальных web/routes/*
    # (веб-процесс не поднимает бота) — см. registration.py::_get_comlink.
    from swgoh_comlink import SwgohComlink
    return SwgohComlink(url="http://localhost:3000")


async def _active_datacron_seasons(guild_id: int):
    """Все активные (незавершённые) сезоны из каталога, самый свежий первым —
    пустой список, если каталог ещё не собрался ни разу (Comlink недоступен на
    первом запуске). Тот же каталог, что и /datacrons (datacrons.py::datacrons_list)."""
    try:
        catalog = await datacron_catalog.get_catalog(_get_comlink())
    except Exception as e:
        print(f"⚠️ [/] Каталог датакронов недоступен для виджета дашборда: {e}")
        return []
    if not catalog or not catalog.get("seasons"):
        return []
    seasons = []
    for set_id in sorted(catalog["seasons"].keys(), reverse=True):
        data = catalog["seasons"][set_id]
        seasons.append({
            "set_id": set_id,
            "display_name": data["display_name"],
            "base_count": database.count_datacron_requirements_by_set(set_id, guild_id=guild_id),
            "focused_count": database.count_datacron_focused_requirements_by_set(set_id, guild_id=guild_id),
        })
    return seasons


def _next_birthdays(guild_id: int, limit: int = 10):
    registrations_by_id = {
        discord_id: ingame_name
        for discord_id, ally_code, ingame_name in database.get_all_main_registrations(guild_id)
    }
    today = date.today()
    rows = []
    for discord_id_str, day, month, year in database.get_all_birthdays(guild_id=guild_id):
        display_name = database.get_username_for_discord_id(discord_id_str) or registrations_by_id.get(discord_id_str) or discord_id_str
        rows.append({
            "display_name": display_name,
            "date_str": f"{day:02d}.{month:02d}",
            "next_birthday": next_birthday(day, month, year, today),
        })
    rows.sort(key=lambda r: r["next_birthday"])
    return rows[:limit]


def _tasks_summary(guild_id: int, limit: int = 8):
    """Счётчики по статусам + короткий список "требует внимания" (провалено — первым,
    затем активные с ближайшим дедлайном) для виджета на главной — та же логика форматирования
    цели, что и на самой /tasks (web/routes/tasks.py::_target_label), не дублируем. Архивные
    (COMPLETED/FAILED старше database.TASK_ARCHIVE_AFTER_DAYS дней) в счётчики не входят —
    тот же фильтр "текущих" задач, что на /tasks."""
    rows = database.get_all_tasks(guild_id)
    names_by_code = {code: name for _, code, name in database.get_all_user_mappings(guild_id)}
    unit_names = database.get_game_unit_names([r[2] for r in rows])

    counts = {"ACTIVE": 0, "COMPLETED": 0, "FAILED": 0}
    open_rows = []
    for (task_id, ally_code, base_id, target_type, target_value, deadline, status, _batch_id,
         _initial_value, _current_value, in_progress, _created_by, resolved_at) in rows:
        if database.is_task_archived(resolved_at):
            continue
        counts[status] = counts.get(status, 0) + 1
        if status in ("ACTIVE", "FAILED"):
            open_rows.append({
                "player_name": names_by_code.get(ally_code, ally_code),
                "unit_name": unit_names.get(base_id) or base_id,
                "target_label": _task_target_label(target_type, target_value),
                "deadline": deadline,
                "status": status,
                "in_progress": bool(in_progress),
            })

    open_rows.sort(key=lambda r: (r["status"] != "FAILED", r["deadline"]))
    return {"counts": counts, "rows": open_rows[:limit], "open_total": len(open_rows)}


@router.get("/oferta", response_class=HTMLResponse)
async def oferta(request: Request, user: dict | None = Depends(get_current_user_optional)):
    # Публичная оферта нужна отдельной страницей (не PDF стороннего сервиса) —
    # см. project_paid_guild_subscription_feature: сначала ссылались на PDF
    # Продамуса, теперь текст свой и не завязан на конкретного платёжного
    # агрегатора, чтобы не переписывать при смене ЮKassa/Robokassa/др.
    return templates.TemplateResponse(request, "oferta.html", {"user": user, "show_footer": True})


@router.get("/start", response_class=HTMLResponse)
async def start_guide(request: Request, user: dict | None = Depends(get_current_user_optional)):
    # Публичная (не officer-only) страница-онбординг — её главная аудитория как
    # раз те, у кого ещё нет доступа к веб-дашборду (обычный участник, или
    # офицер, который ещё не зарегистрировался/не логинился ни разу).
    return templates.TemplateResponse(request, "start.html", {"user": user})


@router.get("/commands", response_class=HTMLResponse)
async def command_docs(request: Request, user: dict | None = Depends(get_current_user_optional)):
    # ВАЖНО: не "/docs" — этот путь занят встроенным Swagger UI FastAPI
    # (docs_url по умолчанию), маршрут с тем же именем молча проигрывает ему.
    from services.command_docs import public_command_docs
    sections, access_labels = public_command_docs()
    return templates.TemplateResponse(request, "docs.html", {
        "user": user,
        "sections": sections,
        "access_labels": access_labels,
    })


@router.get("/", response_class=HTMLResponse)
async def home(request: Request, user: dict | None = Depends(get_current_user_optional)):
    guild_cfg = None
    widgets = None
    if user and user.get("guild_id") is not None:
        guild_cfg = database.get_guild_config(user["guild_id"])

    if user and user.get("tier") == "officer" and guild_cfg:
        guild_id = user["guild_id"]
        roster_rows = dashboard_data.get_roster(guild_id)
        tb_report = dashboard_data.get_tb_report(guild_id)
        activity_rows = dashboard_data.get_guild_activity(guild_id, limit=20)
        top_violators = dashboard_data.get_top_violators(guild_id, limit=10)
        widgets = {
            "roster_rows": roster_rows,
            "registered_count": sum(1 for r in roster_rows if r.registered),
            "tb_report": tb_report,
            "tw_rows": dashboard_data.get_recent_tw_results(guild_id, limit=10),
            "tw_stats": dashboard_data.get_tw_stats(guild_id),
            "top_violators": top_violators,
            "max_violator": top_violators[0].recent_total if top_violators else 0,
            "activity_rows": activity_rows,
            "datacron_seasons": await _active_datacron_seasons(guild_id),
            "birthdays": _next_birthdays(guild_id, limit=10),
            "tasks_summary": _tasks_summary(guild_id),
            "sync_status": _sync_status_text(dashboard_data.get_activity_sync_status(guild_id)),
        }

    error = request.query_params.get("error")
    return templates.TemplateResponse(request, "dashboard.html", {
        "user": user,
        "guild_cfg": guild_cfg,
        "widgets": widgets,
        "config_missing": missing_summary(guild_cfg) if widgets else None,
        "synced_now": request.query_params.get("synced"),
        "error": error,
        "access_status_message": dashboard_data.access_status_message(user) if user and user.get("tier") != "officer" else None,
        "bot_invite_url": discord_invite.build_invite_url() if not user else None,
        # Реквизиты/тарифы/оферта Prodamus нужны только на публичной посадочной
        # странице для неавторизованных (это она проходит проверку Prodamus) —
        # на остальных страницах сайта (в т.ч. этой же для авторизованных)
        # подвал не показываем, см. base.html.
        "show_footer": not user,
    })


@router.post("/sync", response_class=HTMLResponse)
async def home_sync(user: dict = Depends(require_guild_access)):
    """"Обновить сейчас" на главной — тот же синк активности/статов игроков, что
    и на /activity (web/routes/guild_dashboard.py::activity_sync), просто
    редиректит обратно на / вместо /activity. Не трогает ростер (имена/ранги) —
    тот и так обновляется ботом каждые 15 минут (cogs/violations.py), синкать
    его вручную незачем; здесь именно то, что реально отстаёт (гир/релики/
    звёзды/зеты/омикроны, автоцикл которых — часы, не минуты, см.
    database.PLAYER_STATS_SYNC_HOURS)."""
    guild_id = user["guild_id"]
    ally_codes = [code for _, code, _ in database.get_all_user_mappings(guild_id)]
    comlink = _get_comlink()
    today = datetime.now(MSK).date().isoformat()
    semaphore = asyncio.Semaphore(6)
    skill_tier_map = database.get_all_skill_tier_thresholds()

    async def sync_one(ally_code):
        async with semaphore:
            try:
                guild_ids = database.get_guild_ids_for_ally_code(ally_code) or {guild_id}
                _, added, _ = await activity_diff.sync_player(comlink, ally_code, guild_ids, today, skill_tier_map)
                return added
            except Exception as e:
                print(f"⚠️ [/sync] Не удалось обновить ростер {ally_code}: {e}")
                return 0

    results = await asyncio.gather(*(sync_one(ac) for ac in ally_codes))
    return RedirectResponse(f"/?synced={sum(results)}", status_code=303)
