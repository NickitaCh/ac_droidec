from datetime import date
from pathlib import Path

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

import database
from cogs.birthday import next_birthday
from services import dashboard_data, datacron_catalog
from web.deps import get_current_user_optional
from web.routes.tasks import _target_label as _task_target_label

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
    цели, что и на самой /tasks (web/routes/tasks.py::_target_label), не дублируем."""
    rows = database.get_all_tasks(guild_id)
    names_by_code = {code: name for _, code, name in database.get_all_user_mappings(guild_id)}
    unit_names = database.get_game_unit_names([r[2] for r in rows])

    counts = {"ACTIVE": 0, "COMPLETED": 0, "FAILED": 0}
    open_rows = []
    for task_id, ally_code, base_id, target_type, target_value, deadline, status, _batch_id in rows:
        counts[status] = counts.get(status, 0) + 1
        if status in ("ACTIVE", "FAILED"):
            open_rows.append({
                "player_name": names_by_code.get(ally_code, ally_code),
                "unit_name": unit_names.get(base_id) or base_id,
                "target_label": _task_target_label(target_type, target_value),
                "deadline": deadline,
                "status": status,
            })

    open_rows.sort(key=lambda r: (r["status"] != "FAILED", r["deadline"]))
    return {"counts": counts, "rows": open_rows[:limit], "open_total": len(open_rows)}


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
        }

    error = request.query_params.get("error")
    return templates.TemplateResponse(request, "dashboard.html", {
        "user": user,
        "guild_cfg": guild_cfg,
        "widgets": widgets,
        "error": error,
    })
