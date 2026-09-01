"""Веб-only "Оптимизатор модов" (/mod-optimizer) — закрытая страница (см. чат-запрос
пользователя 2026-09-01): загружаешь CSV-экспорт всех своих модов (см. инструкцию на
странице — та же браузерная техника, что была применена вручную в чате: открыть
hotutils.com/mods/management и скачать mods_export.csv), отмечаешь плейты, которым должны
соответствовать твои персонажи, и получаешь для каждого персонажа плейта лучшее
достижимое распределение модов из ОБЩЕГО пула (включая уже надетые на других персонажах —
пользователь разрешил трогать чужие моды, лишь бы не ломать сеты). Сам подбор — в
services/mod_optimizer.py, эта страница только форма+рендер отчёта. Игрок — как и везде
в проекте, "не указан = моя регистрация" (database.get_user_registration).

Доступ ограничен до super_admin (сужено 2026-09-01 — первый реальный расчёт оказался
неточным, пользователь попросил спрятать страницу от рядовых офицеров, пока не доведена
до ума; см. чат за деталями, идея сама пока отложена)."""

from pathlib import Path

from fastapi import APIRouter, Depends, Request, UploadFile, File, Form
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

import database
import stat_engine
import services.stat_forecast as stat_forecast
import services.mod_optimizer as mod_optimizer
from web.deps import require_super_admin

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent.parent / "templates"))


def _fmt_stat(value, name: str) -> str:
    if value is None:
        return "—"
    suffix = "%" if name in stat_engine.PERCENT_STATS else ""
    if abs(value) >= 1000:
        return f"{value:,.0f}".replace(",", " ") + suffix
    text = f"{value:.2f}".rstrip("0").rstrip(".")
    return text + suffix


def _mod_summary(m: dict) -> str:
    p = m["primary"]
    p_text = f"{p['value']:g}{'%' if p['is_percent'] else ''} {p['name']}"
    return f"{m['set_name']} · {m['slot_name']} · {p_text}"


def _enrich_results(results: list[dict]) -> None:
    """Мутирует results на месте — добавляет отформатированные строки для шаблона, чтобы не
    тащить форматирование статов в Jinja."""
    for r in results:
        for req in r["passed"] + r["failed"]:
            req["current_fmt"] = _fmt_stat(req.get("current"), req["stat_name"])
            req["threshold_fmt"] = f"{req['operator']} {_fmt_stat(req['threshold'], req['stat_name'])}"
        r["mods_summary"] = [_mod_summary(m) for m in r.get("used_mods", [])]


def _get_comlink():
    from swgoh_comlink import SwgohComlink
    return SwgohComlink(url="http://localhost:3000")


def _base_context(user: dict, guild_id: int) -> dict:
    plates = database.get_all_stat_requirement_plates(guild_id=guild_id)
    self_reg = database.get_user_registration(user["discord_id"], guild_id=guild_id)
    return {
        "user": user,
        "plates": plates,
        "self_ally_code": self_reg[0] if self_reg else None,
        "self_player_name": self_reg[1] if self_reg else None,
        "selected_plates": [],
        "results": None,
        "error": None,
        "loading": False,
    }


@router.get("", response_class=HTMLResponse)
async def optimizer_form(request: Request, user: dict = Depends(require_super_admin)):
    context = _base_context(user, user["guild_id"])
    return templates.TemplateResponse(request, "mod_optimizer.html", context)


@router.post("", response_class=HTMLResponse)
async def optimizer_run(
    request: Request,
    plates: list[str] = Form(default=[]),
    force_refresh: bool = Form(default=False),
    mods_file: UploadFile = File(...),
    user: dict = Depends(require_super_admin),
):
    guild_id = user["guild_id"]
    context = _base_context(user, guild_id)
    context["selected_plates"] = plates

    if not plates:
        context["error"] = "Выберите хотя бы один плейт."
        return templates.TemplateResponse(request, "mod_optimizer.html", context)

    ally_code = context["self_ally_code"]
    if not ally_code:
        context["error"] = "Ваш аккаунт не привязан через /регистрация — без этого не получится узнать ваш ростер/релики."
        return templates.TemplateResponse(request, "mod_optimizer.html", context)

    raw = await mods_file.read()
    try:
        all_mods = mod_optimizer.parse_mods_csv(raw)
    except mod_optimizer.ModCsvError as e:
        context["error"] = f"Не удалось разобрать файл: {e}"
        return templates.TemplateResponse(request, "mod_optimizer.html", context)

    try:
        comlink = _get_comlink()
        stat_calc = await stat_forecast.get_stat_calc(comlink)
    except Exception:
        context["loading"] = True
        return templates.TemplateResponse(request, "mod_optimizer.html", context)

    results, plates_passed, solved_ok = await mod_optimizer.optimize_plates(
        comlink, stat_calc, plates, ally_code, all_mods, guild_id=guild_id, force_refresh=force_refresh,
    )
    _enrich_results(results)
    context["results"] = results
    context["mods_total"] = len(all_mods)
    context["player_name"] = context["self_player_name"]
    context["plates_passed"] = plates_passed
    context["plates_total"] = len(plates)
    context["solver_timed_out"] = not solved_ok
    return templates.TemplateResponse(request, "mod_optimizer.html", context)
