import asyncio
import io
import json
import os
import re
import time
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote, urlencode
from zoneinfo import ZoneInfo

import httpx
from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

import database
import guild_omicron_rules
import stat_engine
import tb_plan_reader
import tb_platoon_autofill
import tb_platoon_data
import tb_platoon_engine
import tb_platoon_filters
import tb_platoon_notify
from cogs.violations import WARNS_STRUCTURE
from services import activity_diff, channel_health, dashboard_data, discord_rest, feature_flags, omicron_priority, resource_spend, tb_schedule
from services import discord_preview, mod_grade, player_compare, tb_advisor, tb_forecast, tb_missions, tb_my, tb_order_store, tb_plan, tb_projection, tb_results
from services.config_status import config_warning_html
from services.guild_admin import add_guild_scoped_grant, list_grants_for_guild, remove_guild_scoped_grant
import services.stat_forecast as stat_forecast
from web.deps import own_accounts, require_guild_access, require_member_access
from web.routes import tasks as tasks_routes
# Переиспользуем построитель отчёта по датакронам одного игрока и кэш каталога сезонов
# из web/routes/datacrons.py вместо дублирования — та же логика сопоставления,
# что уже используется на /datacrons/check (см. модульный docstring того файла
# про переиспользование чистых функций вместо дублирования).
from web.routes.datacrons import (
    PRIORITY_EMOJI as DATACRON_PRIORITY_EMOJI,
    PRIORITY_LABELS as DATACRON_PRIORITY_LABELS,
    PRIORITY_ORDER as DATACRON_PRIORITY_ORDER,
    _build_player_report as _build_datacron_player_report,
    _safe_catalog as _safe_datacron_catalog,
)

MSK = ZoneInfo("Europe/Moscow")

# Независимая копия web/routes/stat_forecast.py::SCHEME_LABELS (тот же принцип, что
# MSK/COMLINK_URL дублируются по проекту, см. CLAUDE.md) — форсирует схему мод-билда
# персонажа на карточке игрока вместо авто-детекта.
SCHEME_LABELS = {"": "Авто", "1": "Вариант 1", "2": "Вариант 2"}


def _get_comlink():
    # См. web/routes/admin.py::_get_comlink / registration.py::_get_comlink — веб-процесс
    # не поднимает бота, строит свой SwgohComlink на тот же comlink-сайдкар.
    from swgoh_comlink import SwgohComlink
    return SwgohComlink(url="http://localhost:3000")


def _format_delta(delta: timedelta) -> str:
    total_minutes = round(delta.total_seconds() / 60)
    hours, minutes = divmod(abs(total_minutes), 60)
    parts = []
    if hours:
        parts.append(f"{hours} ч")
    parts.append(f"{minutes} мин")
    return " ".join(parts)


def _hours_label(hours: int) -> str:
    """Склонение "час"/"часа"/"часов" под PLAYER_STATS_SYNC_HOURS — значение конфигурируемое
    (сейчас 1), поэтому текст на /activity не может просто хардкодить "часов"."""
    n = abs(hours) % 100
    if 11 <= n <= 14:
        word = "часов"
    elif n % 10 == 1:
        word = "час"
    elif 2 <= n % 10 <= 4:
        word = "часа"
    else:
        word = "часов"
    return f"{hours} {word}"


def _sync_status_text(sync_status: dict) -> dict:
    """(last_sync, next_auto, sync_hours) -> человекочитаемые строки для панели на /activity."""
    last_sync_text = None
    if sync_status["last_sync"]:
        last_sync_text = sync_status["last_sync"].strftime("%d.%m.%Y %H:%M (МСК)")

    next_auto_text = None
    next_auto = sync_status["next_auto"]
    if next_auto:
        now = datetime.now(MSK)
        if next_auto <= now:
            next_auto_text = "вот-вот"
        else:
            next_auto_text = f"через {_format_delta(next_auto - now)}"

    return {
        "last_sync_text": last_sync_text,
        "next_auto_text": next_auto_text,
        "sync_hours_text": _hours_label(sync_status["sync_hours"]),
    }

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent.parent / "templates"))
# Шапка ТБ (_tb.html::tb_hero): прежние 4 вкладки, если гильдия включила запасной старый вид.
templates.env.globals["tb_classic_nav"] = lambda user: bool((user or {}).get("guild_id")) and \
    feature_flags.is_enabled(user["guild_id"], "tb_classic_nav")

# Подписи веток — намеренно те же самые, что в choices Discord-команды тб_отчет план
# (cogs/guild_events.py::tb_set_plan) для консистентности офицерского UX между ботом и
# вебом. Не путать с TB_CONFLICT_LABELS (cogs/guild_events.py) — тот словарь используется
# только как fallback-подпись в отчётах на чтение и расходится с этими ("01"->Dark там,
# "01"->Light здесь) — это существовавшее расхождение в самом боте, не веб-баг.
# Порядок и подписи — как cogs/guild_events.py::TB_CONFLICT_LABELS (01 = тёмная ветка:
# Мустафар/Джеонозис…, 02 = смешанная, 03 = светлая), колонками на странице «План».
TB_PLAN_CONFLICT_OPTIONS = [
    {"value": "01", "label": "Тёмная"},
    {"value": "02", "label": "Смешанная"},
    {"value": "03", "label": "Светлая"},
    {"value": "bonus", "label": "Бонус"},
]

# Тот же паттерн, что и cogs/tb_order_image.py::LINK_RE — продублирован, а не
# импортирован, чтобы веб-процесс (без доступа к Discord-клиенту) не тянул за
# собой cogs/guild_events.py целиком ради одной регулярки (см. коммент выше про
# TB_PLAN_CONFLICT_OPTIONS — здесь тот же принцип дублирования ради развязки).
TB_THREAD_LINK_RE = re.compile(r"discord(?:app)?\.com/channels/(\d+)/(\d+)")


def _extract_thread_id(value: str) -> int | None:
    value = (value or "").strip()
    m = TB_THREAD_LINK_RE.search(value)
    if m:
        return int(m.group(2))
    if value.isdigit():
        return int(value)
    return None


def _tw_guides_sync_status(guild_id: int) -> dict | None:
    """Последний синк базы контр из форума гайдов (пишет cogs/tw_counters.py) и есть ли
    невыполненная заявка с сайта. None — форум гайдов у гильдии не задан."""
    cfg = database.get_guild_config(guild_id) or {}
    if not cfg.get("tw_guide_forum_channel_id"):
        return None
    try:
        result = json.loads(database.get_bot_state("tw_sync_result", guild_id=guild_id) or "{}")
    except ValueError:
        result = {}
    if result.get("finished_at"):
        finished = datetime.strptime(result["finished_at"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        result["finished_text"] = finished.astimezone(MSK).strftime("%d.%m %H:%M МСК")
    request = database.get_bot_state("tw_sync_request", guild_id=guild_id)
    pending = bool(request) and request != database.get_bot_state("tw_sync_request_done", guild_id=guild_id)
    return {"result": result, "pending": pending}


@router.get("/tw", response_class=HTMLResponse)
async def tw_report(request: Request, user: dict = Depends(feature_flags.require_feature("tw_order"))):
    rows = dashboard_data.get_recent_tw_results(user["guild_id"], limit=50)
    wins = sum(1 for r in rows if r.result == "win")
    return templates.TemplateResponse(request, "tw_report.html", {
        "user": user,
        "rows": rows,
        "wins": wins,
        "tw_stats": dashboard_data.get_tw_stats(user["guild_id"]),
        "guides_sync": _tw_guides_sync_status(user["guild_id"]),
    })


@router.post("/tw/guides-sync", response_class=HTMLResponse)
async def tw_guides_sync(user: dict = Depends(feature_flags.require_feature("tw_order"))):
    """Аналог /вг_гайды_синхронизация: заявка боту, он подхватит её за ~20 секунд
    (cogs/tw_counters.py::web_sync_requests_loop)."""
    database.set_bot_state("tw_sync_request", datetime.now(timezone.utc).isoformat(), guild_id=user["guild_id"])
    return RedirectResponse("/tw", status_code=303)


def _parse_event_id(request: Request) -> int | None:
    raw = request.query_params.get("event_id")
    return int(raw) if raw and raw.isdigit() else None


@router.get("/tb", response_class=HTMLResponse)
async def tb_report(request: Request, user: dict = Depends(feature_flags.require_feature("tb_reports"))):
    report = dashboard_data.get_tb_report(user["guild_id"], event_id=_parse_event_id(request))
    max_summary = report.latest[0].summary if report and report.latest else 0
    max_trend_total = max((t for _, t in report.event_totals), default=0) if report else 0
    # Выбывшие игроки остаются в разрезе конкретной ТБ, но из «Истории по игрокам»
    # (сводка за несколько ТБ) уезжают на отдельную вкладку ?departed=1.
    show_departed = request.query_params.get("departed") == "1"
    history_rows = [r for r in report.history if r.departed == show_departed] if report else []
    departed_count = sum(1 for r in report.history if r.departed) if report else 0
    breakdown = tb_results.event_breakdown(report.selected_event_id) if report else None
    return templates.TemplateResponse(request, "tb_report.html", {
        "breakdown": breakdown,
        "event_stars": database.get_tb_event_stars([report.selected_event_id]).get(report.selected_event_id) if report else None,
        "planet_ru": _planet_ru,
        "compact": dashboard_data._compact_number,
        "user": user,
        "report": report,
        "show_departed": show_departed,
        "history_rows": history_rows,
        "departed_count": departed_count,
        "max_summary": max_summary,
        "max_trend_total": max_trend_total,
    })


@router.get("/tb/player/{name}", response_class=HTMLResponse)
async def tb_player(name: str, request: Request, user: dict = Depends(feature_flags.require_feature("tb_reports"))):
    report = dashboard_data.get_tb_player_report(user["guild_id"], name, event_id=_parse_event_id(request))
    if report is None:
        raise HTTPException(status_code=404, detail=f"Нет сохранённых данных ТБ для игрока «{name}»")
    return templates.TemplateResponse(request, "tb_player.html", {"user": user, "report": report})


@router.get("/tb/compare/{name}", response_class=HTMLResponse)
async def tb_compare_player(name: str, request: Request, user: dict = Depends(feature_flags.require_feature("tb_reports"))):
    compare = dashboard_data.get_tb_player_compare(user["guild_id"], name)
    if compare is None:
        raise HTTPException(status_code=404, detail=f"Нет сохранённых данных ТБ для игрока «{name}»")
    return templates.TemplateResponse(request, "tb_compare_player.html", {"user": user, "compare": compare})


def _tb_plan_grid(guild_id: int) -> tuple[list, bool]:
    """Текущий план ТБ сеткой: строка = этап 1-6, колонки = ветки (TB_PLAN_CONFLICT_OPTIONS)."""
    current = database.get_tb_planet_names(guild_id)
    has_bonus = any(conflict_key == "bonus" for _, conflict_key in current)
    grid = []
    for phase in range(1, 7):
        cells = {opt["value"]: current.get((str(phase), opt["value"])) for opt in TB_PLAN_CONFLICT_OPTIONS}
        grid.append({"phase": phase, "cells": cells, "empty": not any(cells.values())})
    return grid, has_bonus


@router.get("/tb/order", response_class=HTMLResponse)
async def tb_order(request: Request, user: dict = Depends(feature_flags.require_feature("tb_plan_order"))):
    """Вкладка «Ордер» (2026-10-26, раньше — «План»): ветки ордера в Discord, активный ордер,
    ордер из картинки и сетка «какая планета на каком этапе» для разбора объявлений."""
    guild_id = user["guild_id"]
    guild_cfg = database.get_guild_config(guild_id) or {}
    grid, has_bonus = _tb_plan_grid(guild_id)
    active_id = int(guild_cfg["tb_active_plan_id"]) if guild_cfg.get("tb_active_plan_id") else None
    discord_guild_id = guild_cfg.get("discord_guild_id")
    plans = []
    for p in database.get_tb_saved_plans(guild_id):
        plans.append({
            **p,
            "active": p["id"] == active_id,
            "thread_link": f"https://discord.com/channels/{discord_guild_id}/{p['thread_id']}" if discord_guild_id and p["thread_id"] else None,
        })

    # Шаг «Ордер» плана (?plan=<id плана>): по умолчанию — активный план.
    calc_plans = tb_plan.list_plans(guild_id)
    step_raw = request.query_params.get("plan", "")
    step_id = int(step_raw) if step_raw.isdigit() else tb_plan.active_plan_id(guild_id, calc_plans)
    step_plan = next((p for p in calc_plans if p["id"] == step_id), None)

    preview = None
    preview_plan = None
    preview_error = None
    preview_plan_id = request.query_params.get("preview_plan_id")
    if preview_plan_id:
        try:
            preview_plan = database.get_tb_saved_plan(int(preview_plan_id))
        except ValueError:
            preview_plan = None
        if preview_plan is None or preview_plan["guild_id"] != guild_id:
            preview_error = "План не найден."
            preview_plan = None
        else:
            preview, preview_error = await tb_plan_reader.fetch_order_preview(preview_plan)

    editor = await _order_editor(request, guild_id, guild_cfg, plans, step_plan)

    return templates.TemplateResponse(request, "tb_order.html", {
        "user": user,
        "editor": editor,
        "grid": grid,
        "has_bonus": has_bonus,
        "conflict_options": TB_PLAN_CONFLICT_OPTIONS,
        "plans": plans,
        "active_plan": next((p for p in plans if p["active"]), None),
        "step_plan": step_plan,
        "step_order_id": step_plan.get("order_plan_id") if step_plan else None,
        "error": request.query_params.get("error"),
        "saved": request.query_params.get("saved"),
        "config_warning": config_warning_html(guild_cfg, "tb_plan_order"),
        "preview": preview,
        "preview_plan": preview_plan,
        "preview_error": preview_error,
    })


async def _order_editor(request: Request, guild_id: int, guild_cfg: dict, plans: list, step_plan: dict | None) -> dict | None:
    """Редактор ордера по этапам (services/tb_order_store.py): какой ордер (?order=, иначе ордер
    шага плана, иначе активный, иначе последний), какой этап (?phase=, иначе идущий этап ВИ)."""
    if not plans:
        return None
    raw = request.query_params.get("order", "")
    order = next((p for p in plans if raw.isdigit() and p["id"] == int(raw)), None)
    if order is None and step_plan and step_plan.get("order_plan_id"):
        order = next((p for p in plans if p["id"] == step_plan["order_plan_id"]), None)
    order = order or next((p for p in plans if p["active"]), None) or plans[0]
    current = tb_order_store.current_phase()
    raw_phase = request.query_params.get("phase", "")
    phase = int(raw_phase) if raw_phase.isdigit() and 1 <= int(raw_phase) <= 6 else (current or 1)
    blocks, error = await tb_order_store.resolve_blocks({**order, "guild_id": guild_id})
    block = blocks.get(phase) or {}
    row = block.get("row") or {}
    from tg_bot import outbox
    return {
        "order": order, "phase": phase, "current": current, "error": error,
        "phases": [{"phase": ph, "has": ph in blocks, "source": (blocks.get(ph) or {}).get("source")} for ph in range(1, 7)],
        "text": block.get("text") or "", "source": block.get("source"), "link": block.get("link"),
        "tg_text": row.get("tg_text") or "",
        "sent_discord": row.get("sent_discord_at"), "sent_tg": row.get("sent_tg_at"),
        "source_labels": tb_order_store.SOURCE_LABELS,
        "channel_set": bool(guild_cfg.get("tb_plan_channel_id")),
        "role_set": bool(guild_cfg.get("tb_order_role_id")),
        "tg_chats": len(outbox.chats_for(guild_id, "tb_order")),
        "notice": request.query_params.get("order_msg"), "order_error": request.query_params.get("order_err"),
    }


def _order_back(order_id: int, phase: int, msg: str = None, err: str = None) -> RedirectResponse:
    q = {"order": order_id, "phase": phase, **({"order_msg": msg} if msg else {}), **({"order_err": err} if err else {})}
    return RedirectResponse(f"/tb/order?{urlencode(q)}#order-editor", status_code=303)


def _own_order(guild_id: int, order_id: int) -> dict:
    order = database.get_tb_saved_plan(order_id)
    if order is None or order["guild_id"] != guild_id:
        raise HTTPException(status_code=404)
    return order


@router.post("/tb/order/{order_id}/phase/{phase}/save")
async def tb_order_phase_save(order_id: int, phase: int, text: str = Form(default=""), tg_text: str = Form(default=""),
                              user: dict = Depends(feature_flags.require_feature("tb_plan_order"))):
    """Правка этапа на сайте: дальше бот берёт этот текст, а не тред."""
    _own_order(user["guild_id"], order_id)
    if not 1 <= phase <= 6:
        raise HTTPException(status_code=404)
    if not text.strip():
        return _order_back(order_id, phase, err="Текст этапа пустой — нечего сохранять.")
    tb_order_store.save_phase(order_id, phase, text, "edited", by=f"web:{user['discord_id']}",
                              tg_text=tg_text, keep_tg=False)
    return _order_back(order_id, phase, msg=f"Этап {phase} сохранён — бот опубликует этот текст.")


@router.post("/tb/order/{order_id}/phase/{phase}/import")
async def tb_order_phase_import(order_id: int, phase: int, link: str = Form(...),
                                user: dict = Depends(feature_flags.require_feature("tb_plan_order"))):
    """Перенос этапа из готового сообщения Discord по ссылке."""
    _own_order(user["guild_id"], order_id)
    if not 1 <= phase <= 6:
        raise HTTPException(status_code=404)
    try:
        text = await tb_order_store.fetch_message(link)
    except (RuntimeError, httpx.HTTPError) as e:
        return _order_back(order_id, phase, err=str(e))
    mismatch = tb_order_store.phase_mismatch(text, phase)
    if mismatch:
        return _order_back(order_id, phase, err=mismatch)
    tb_order_store.save_phase(order_id, phase, text, "message", by=f"web:{user['discord_id']}", link=link.strip())
    return _order_back(order_id, phase, msg=f"Этап {phase} перенесён из сообщения.")


@router.post("/tb/order/{order_id}/phase/{phase}/send")
async def tb_order_phase_send(order_id: int, phase: int, where: str = Form(...),
                              user: dict = Depends(feature_flags.require_feature("tb_plan_order"))):
    """Ручная отправка этапа в канал Discord (с упоминанием роли, как автопост) или в Telegram."""
    order = _own_order(user["guild_id"], order_id)
    blocks, _err = await tb_order_store.resolve_blocks(order)
    if phase not in blocks:
        return _order_back(order_id, phase, err=f"У этапа {phase} нет текста.")
    if not tb_order_store.get_phases(order_id).get(phase):  # текст из треда без копии — сохранить копию
        tb_order_store.save_phase(order_id, phase, blocks[phase]["text"], "thread", by="thread")
    try:
        if where == "discord":
            msg = await tb_order_store.send_discord(user["guild_id"], order_id, phase)
        elif where == "tg":
            msg = tb_order_store.send_telegram(user["guild_id"], order_id, phase)
        else:
            raise HTTPException(status_code=400)
    except (RuntimeError, httpx.HTTPError) as e:
        return _order_back(order_id, phase, err=str(e))
    return _order_back(order_id, phase, msg=msg)


@router.post("/tb/order/preview")
async def tb_order_preview_render(request: Request, user: dict = Depends(feature_flags.require_feature("tb_plan_order"))):
    """Предпросмотр «как будет в мессенджере»: {kind: discord|tg, text} → сообщения в HTML.
    Discord — та же нарезка, что у отправки (с упоминанием роли); Telegram — тот же рендер,
    которым шлёт бот (tg_bot/notifier.py::render)."""
    body = await request.json()
    text = str(body.get("text") or "")[:20000]
    if body.get("kind") == "tg":
        from tg_bot import notifier
        chunks = notifier.render(text) if text.strip() else []
        return JSONResponse({"messages": chunks, "chars": len(text), "limit": 4096})
    role_id = (database.get_guild_config(user["guild_id"]) or {}).get("tb_order_role_id")
    chunks = tb_order_store.discord_chunks(text, role_id) if text.strip() else []
    names = {str(role_id): "роль ордера"} if role_id else {}
    return JSONResponse({"messages": [discord_preview.render(c, names) for c in chunks],
                         "chars": len(text), "limit": tb_order_store.DISCORD_LIMIT})


@router.post("/tb/order/import-new")
async def tb_order_import_new(request: Request, user: dict = Depends(feature_flags.require_feature("tb_plan_order"))):
    """Новый ордер из готовых сообщений Discord: ссылка на сообщение на каждый этап (пустые —
    можно добавить потом в редакторе). Треда не нужно."""
    form = await request.form()
    guild_id = user["guild_id"]
    name = str(form.get("name") or "").strip()[:80]
    if not name:
        return RedirectResponse(f"/tb/order?{urlencode({'error': 'Укажите название ордера'})}#import-new", status_code=303)
    texts, errors = {}, []
    for phase in range(1, 7):
        link = str(form.get(f"link{phase}") or "").strip()
        if not link:
            continue
        try:
            text = await tb_order_store.fetch_message(link)
        except (RuntimeError, httpx.HTTPError) as e:
            errors.append(f"этап {phase}: {e}")
            continue
        mismatch = tb_order_store.phase_mismatch(text, phase)
        if mismatch:
            errors.append(f"этап {phase}: {mismatch}")
        else:
            texts[phase] = (text, link)
    if errors or not texts:
        msg = "; ".join(errors) if errors else "Не указано ни одной ссылки."
        return RedirectResponse(f"/tb/order?{urlencode({'error': msg})}#import-new", status_code=303)
    if database.get_tb_saved_plan_by_name(guild_id, name):
        return RedirectResponse(f"/tb/order?{urlencode({'error': f'Ордер «{name}» уже есть — выберите другое название'})}#import-new", status_code=303)
    stars = str(form.get("stars") or "").strip()
    order_id = database.save_tb_plan(guild_id, name, "", int(stars) if stars.isdigit() else None,
                                     created_by=f"web:{user['discord_id']}")
    for phase, (text, link) in texts.items():
        tb_order_store.save_phase(order_id, phase, text, "message", by=f"web:{user['discord_id']}", link=link)
    if form.get("make_active"):
        database.update_guild_config(guild_id, tb_active_plan_id=order_id)
    return _order_back(order_id, min(texts), msg=f"Ордер «{name}» создан: перенесено этапов — {len(texts)} из 6.")


@router.get("/tb/order/api/stage-messages")
async def tb_plan_stage_messages(user: dict = Depends(feature_flags.require_feature("tb_plan_order"))):
    """Тексты этапов из активного плана ордера — то, что бот опубликует на этап
    (попап по клику на строку плана)."""
    guild_cfg = database.get_guild_config(user["guild_id"]) or {}
    plan_id = guild_cfg.get("tb_active_plan_id")
    plan = database.get_tb_saved_plan(int(plan_id)) if plan_id else None
    if plan is None or plan["guild_id"] != user["guild_id"]:
        return JSONResponse({"error": "Активный план ордера не выбран — выберите его в блоке «Планы ордера»."})
    preview, error = await tb_plan_reader.fetch_order_preview(plan)
    if error:
        return JSONResponse({"error": error})
    return JSONResponse({"plan": plan["name"], "stages": {str(p["phase"]): p["block"] for p in preview}})


@router.post("/tb/order/planet", response_class=HTMLResponse)
async def tb_order_planet_save(
    phase: int = Form(...),
    conflict_key: str = Form(...),
    planet_name: str = Form(...),
    user: dict = Depends(feature_flags.require_feature("tb_plan_order")),
):
    planet_name = planet_name.strip()
    valid_conflicts = {opt["value"] for opt in TB_PLAN_CONFLICT_OPTIONS}
    if not planet_name:
        return RedirectResponse(f"/tb/order?{urlencode({'error': 'Название планеты не может быть пустым'})}", status_code=303)
    if not (1 <= phase <= 6):
        return RedirectResponse(f"/tb/order?{urlencode({'error': 'Некорректный номер этапа (1-6)'})}", status_code=303)
    if conflict_key not in valid_conflicts:
        return RedirectResponse(f"/tb/order?{urlencode({'error': 'Некорректная ветка'})}", status_code=303)
    database.set_tb_planet_name(str(phase), conflict_key, planet_name, source="manual", guild_id=user["guild_id"])
    return RedirectResponse("/tb/order?saved=1", status_code=303)


@router.get("/tb/order-plans", response_class=HTMLResponse)
async def tb_order_plans(request: Request, user: dict = Depends(feature_flags.require_feature("tb_plan_order"))):
    # Старые ссылки/закладки на «Планы ордера» ведут на вкладку «Ордер».
    query = f"?{request.url.query}" if request.url.query else ""
    return RedirectResponse(f"/tb/order{query}", status_code=307)


@router.post("/tb/order-plans/select", response_class=HTMLResponse)
async def tb_order_plans_select(plan_id: int = Form(...), user: dict = Depends(feature_flags.require_feature("tb_plan_order"))):
    plan = database.get_tb_saved_plan(plan_id)
    if plan is None or plan["guild_id"] != user["guild_id"]:
        return RedirectResponse(f"/tb/order?{urlencode({'error': 'План не найден'})}", status_code=303)
    database.update_guild_config(user["guild_id"], tb_active_plan_id=plan_id)
    return RedirectResponse("/tb/order?saved=1", status_code=303)


@router.post("/tb/order-plans/delete", response_class=HTMLResponse)
async def tb_order_plans_delete(name: str = Form(...), user: dict = Depends(feature_flags.require_feature("tb_plan_order"))):
    database.delete_tb_saved_plan(user["guild_id"], name)
    return RedirectResponse("/tb/order?saved=1", status_code=303)


@router.post("/tb/order-plans/save", response_class=HTMLResponse)
async def tb_order_plans_save_manual(
    name: str = Form(...),
    thread_link: str = Form(...),
    stars: int = Form(...),
    user: dict = Depends(feature_flags.require_feature("tb_plan_order")),
):
    # Разовый перенос (2026-10-28): тексты 6 этапов копируются на сайт, тред к ордеру не
    # привязывается и дальше не нужен (services/tb_order_store.py::import_thread).
    name = name.strip()
    if not name:
        return RedirectResponse(f"/tb/order?{urlencode({'error': 'Название не может быть пустым'})}", status_code=303)
    thread_id = _extract_thread_id(thread_link)
    if thread_id is None:
        return RedirectResponse(
            f"/tb/order?{urlencode({'error': 'Не удалось распознать ссылку на тред'})}", status_code=303
        )
    try:
        order_id = await tb_order_store.import_thread(user["guild_id"], thread_id, name[:80], stars,
                                                      f"web:{user['discord_id']}")
    except (RuntimeError, httpx.HTTPError) as e:
        return RedirectResponse(f"/tb/order?{urlencode({'error': str(e)})}", status_code=303)
    return _order_back(order_id, 1, msg=f"Ордер «{name}» перенесён из треда — 6 этапов. Тред больше не нужен.")


# =====================================================================
# КОНСТРУКТОР ВЗВОДОВ ТБ: для планет, которые реально задействованы в выбранном
# сохранённом плане ордера (по умолчанию — активный, guilds.tb_active_plan_id, но
# можно переключиться на любой другой через ?plan_id=), показывает 6 операций,
# подсказки юнитов на каждую (tb_platoon_data.ROTE_PLATOON_SUGGESTIONS, см. память
# project_tb_platoon_unit_lists_rote_2026-08-27), минимальную реликвию по планете
# (tb_platoon_data.ROTE_MIN_RELIC_BY_PLANET) и назначение конкретного игрока гильдии
# на конкретный донат-слот (database.*_tb_platoon_assignment*, аналог "Assign
# Players" в HotUtils) — кандидаты на слот считаются из живого кэша ростера гильдии
# (database.get_player_unit_owners_bulk + stat_engine.get_current_relic_level).
#
# План хранит только discord thread_id (см. cogs/tb_order_image.py) — самого разбора
# "какая планета на каком этапе" нигде в БД не остаётся, только опубликованные ботом
# сообщения-блоки в этом треде. Разбор (чтение через Discord REST + регексы + перевод
# RU->EN названий планет) вынесен в tb_plan_reader.py 2026-08-29 — тот же разбор
# понадобился tb_platoon_autofill.py, который дёргается и с веб-роута, и из
# cogs/tb_order_image.py (см. план "Автозаполнение взводов ТБ + фильтры"), дублировать
# его в третий раз не стали.
# =====================================================================

_PLANET_NAME_TO_RU = tb_plan_reader.PLANET_NAME_TO_RU
_RU_TO_PLANET_NAME = tb_plan_reader.RU_TO_PLANET_NAME

# Ветка (conflict-код в comlink zoneId, "power_zone_tb3_mixed_phaseNN_conflictNN_...") для
# каждой планеты — дублирует cogs/guild_events.py::TB_PLANET_CONFLICT (тот же принцип
# развязки веб-процесса от cogs/*, что и у _TB_PLAN_HEADER_RE выше), но ключами здесь взяты
# английские названия из tb_platoon_data.py (а не русские из офицерских анонсов) — сразу
# нужный вид для сборки zoneId в EchoBase/HotUtils-экспорте взводов (см. tb_platoons_export
# ниже). Код фиксирован per-планета (не меняется от этапа к этапу) — подтверждено сверкой
# сгенерированного zoneId с реальным экспортом HotUtils: 15 обычных планет — по файлу с
# этапа 1 (Mustafar=conflict01, набор юнитов совпал с ROTE_PLATOON_SUGGESTIONS[("Mustafar",1)]
# как мультимножество); Zeffo/Mandalore — по файлу-примеру с бонус-зонами (этап "4/M4/Z4",
# 2026-08-28): их unitBaseId по каждому platoonDefinitionId сверены с ROTE_PLATOON_SUGGESTIONS
# для обеих бонус-планет (Zeffo: conflict01_bonus, 47/72 юнитов совпало против 5/72 у
# Mandalore на той же зоне; Mandalore: conflict03_bonus, 48/71 против 4/71 у Zeffo).
# Death Star/Hoth/Scarif (появляются только на этапе 6, до которого гильдия ещё не доходила,
# поэтому их вообще нет в TB_PLANET_CONFLICT) — сняты 2026-08-28 напрямую со страницы
# https://echobase.app/platoonAssigner/tb/RiseOfTheEmpire/phase/6 для гильдии AbsoluteChaos
# (guildId=718294) через DOM (table[cgid] на каждую зону несёт готовый zoneId), составы
# донат-слотов дополнительно сверены с ROTE_PLATOON_SUGGESTIONS — совпадение сильное для всех
# трёх. ВАЖНО: цифра conflict-кода здесь НЕ соответствует "интуитивной" тёмная/смешанная/
# светлая теме планеты в порядке 01/02/03 — Scarif (светлая по геймплею) оказался на
# conflict01, Death Star (тёмная) — на conflict02, Hoth (смешанная) — на conflict03; не
# пытаться выводить conflict-код новых планет из темы юнитов, только снимать живьём.
_PLANET_CONFLICT_CODE = {
    "Mustafar": "01", "Geonosis": "01", "Dathomir": "01", "Haven-class Medical Station": "01", "Malachor": "01",
    "Scarif": "01",
    "Corellia": "02", "Felucia": "02", "Tatooine": "02", "Kessel": "02", "Vandor": "02",
    "Death Star": "02",
    "Coruscant": "03", "Bracca": "03", "Kashyyyk": "03", "Lothal": "03", "Ring of Kafrene": "03",
    "Hoth": "03",
    "Zeffo": "01", "Mandalore": "03",
}
_BONUS_PLANETS = {"Zeffo", "Mandalore"}

# Неподтверждённая деталь, НЕ проблема на практике (см. пояснение пользователя 2026-08-28:
# планету можно зачищать и расставлять на ней взводы практически на любом этапе): в файле-
# примере "4/M4/Z4" бонус-зона Зеффо стояла на phase03, хотя весь файл был снят на этапе 4 (три
# обычные зоны и Мандалор — на phase04). Наша сборка всё равно берёт цифру фазы как round_num
# (номер анонсированного этапа) — точно верно для первого появления планеты на этапе (проверено
# на этапе 1); расхождение выше не мешает реальному использованию, оставлено как техническая
# заметка на случай, если импорт в HotUtils когда-нибудь всё же на этом споткнётся.


def _resolve_viewed_plan(guild_id: int, plan_id_param: str | None) -> tuple[dict | None, str | None]:
    """Какой сохранённый план показывать в конструкторе взводов: явный ?plan_id= (любой
    план гильдии, не обязательно активный — переключение в конструкторе только просмотровое,
    не трогает guilds.tb_active_plan_id), либо, если параметра нет, активный план как раньше."""
    if plan_id_param:
        try:
            plan_id = int(plan_id_param)
        except ValueError:
            return None, "Некорректный ID плана."
        plan = database.get_tb_saved_plan(plan_id)
        if not plan or plan["guild_id"] != guild_id:
            return None, "План не найден."
        return plan, None

    guild_cfg = database.get_guild_config(guild_id) or {}
    active_id = guild_cfg.get("tb_active_plan_id")
    if not active_id:
        return None, "Нет активного ордера: опубликуйте его из плана (вкладка «План» → «Опубликовать ордер») или выберите на вкладке «Ордер»."
    plan = database.get_tb_saved_plan(int(active_id))
    if not plan or plan["guild_id"] != guild_id:
        return None, "Активный ордер не найден — выберите его заново на вкладке «Ордер»."
    return plan, None


# Тонкая обёртка вокруг tb_plan_reader.fetch_plan_planets — сохраняет прежнее локальное
# имя _fetch_plan_planets(plan), чтобы не трогать все вызовы ниже по файлу.
_fetch_plan_planets = tb_plan_reader.fetch_plan_planets


# Потолок на число кандидатов, показываемых под одним слотом — без него страница
# распухала до десятков тысяч <form> разом (частый юнит донатят треть гильдии), из-за
# чего переключение этапа/раскрытие слота ощутимо тормозило (жалоба пользователя
# 2026-08-28). Топ-N по релику — снизу всё равно никто не выбирает донора №25.
PLATOON_CANDIDATES_LIMIT = 20


@router.get("/tb/missions", response_class=HTMLResponse)
async def tb_missions_page(request: Request, tab: str = "covert",
                           user: dict = Depends(feature_flags.require_feature("tb_reports"))):
    events = database.get_recent_tb_events(guild_id=user["guild_id"])  # старые -> новые
    if tab not in ("covert", "rates", "strikes"):
        tab = "covert"
    ctx = {"user": user, "tab": tab, "events": events, "selected_event_id": None}
    if events:
        event_ids = [e[0] for e in events]
        selected = _parse_event_id(request)
        selected = selected if selected in event_ids else event_ids[-1]
        ctx["selected_event_id"] = selected
        # Все три вкладки считаются сразу — переключение без перезагрузки (скрипт в шаблоне).
        ctx["covert"], ctx["rates"], ctx["strikes"] = await asyncio.gather(
            asyncio.to_thread(tb_missions.covert_event, user["guild_id"], selected, selected == event_ids[-1]),
            asyncio.to_thread(tb_missions.covert_rates, user["guild_id"], events),
            asyncio.to_thread(tb_missions.strikes_event, user["guild_id"], selected),
        )
    return templates.TemplateResponse(request, "tb_missions.html", ctx)


# (guild_id, bonus) -> (время, результат): разбор складов гильдии — ~10 с, а склады
# обновляются раз в час, поэтому пересчитываем не чаще раза в 15 минут.
_PROJECTION_CACHE: dict = {}
PROJECTION_CACHE_SECONDS = 900


@router.get("/tb/projection", response_class=HTMLResponse)
async def tb_projection_page(user: dict = Depends(feature_flags.require_feature("tb_plan_order"))):
    # Проекция (готовность взводов, ОЗ, БЗ и прогноз звёзд) переехала в карточки «Плана».
    return RedirectResponse("/tb/plan", status_code=307)


async def _projection(guild_id: int, bonus: bool) -> tuple:
    """(время расчёта, проекция) — общий 15-минутный кэш для /tb/projection и /tb/goal."""
    key = (guild_id, bonus)
    cached = _PROJECTION_CACHE.get(key)
    if not cached or time.time() - cached[0] >= PROJECTION_CACHE_SECONDS:
        # ~50 игроков × ~300 юнитов из player_unit_cache — секунды, поэтому в потоке.
        cached = (time.time(), await asyncio.to_thread(tb_projection.build, guild_id, bonus))
        _PROJECTION_CACHE[key] = cached
    return cached


def _planet_ru(planet: str) -> str:
    return (_PLANET_NAME_TO_RU.get(planet) or planet).capitalize()


# =====================================================================
# ПЛАН ТБ — калькулятор (services/tb_plan.py): планы по этапам, расчёт «хватит ли залива,
# боёв и взводов», готовность (взводы, ОЗ, БЗ) по планетам дня, публикация ордера в Discord.
# Входы расчёта — та же 15-минутная проекция (_projection), поэтому правка плана — доли секунды.
# =====================================================================

def _plan_readiness(r: dict) -> tuple[dict, dict]:
    """({планета: {"covert": [...], "strikes": [...]}}, {base_id: узкое место из проекции})."""
    by_planet: dict = {}
    bottlenecks: dict = {}
    for ph in r.get("phases") or []:
        for c in ph.get("covert") or []:
            by_planet.setdefault(c["planet"], {"covert": [], "strikes": []})["covert"].append(c)
        for st in ph.get("strikes") or []:
            by_planet.setdefault(st["planet"], {"covert": [], "strikes": []})["strikes"].append(st)
        for b in ph.get("bottlenecks") or []:
            bottlenecks.setdefault(b["base_id"], b)
    return by_planet, bottlenecks


def _plan_shortages(result: dict, inp: dict, bottlenecks: dict) -> None:
    """В каждый этап плана — каких юнитов не хватает во взводах его планет (после раздачи
    tb_forecast._fill_platoons), с теми, кто «в шаге», из проекции."""
    ships = set(inp["platoons"].get("ships") or ())
    all_ids = {bid for ph in result["phases"] for row in ph["planets"] for bid, _n in row["short_slots"] if bid}
    names = database.get_game_unit_names(sorted(all_ids))
    for ph in result["phases"]:
        agg: dict = {}
        for row in ph["planets"]:
            for bid, need in row["short_slots"]:
                if not bid:
                    continue
                a = agg.setdefault((bid, need), {"base_id": bid, "need": need, "short": 0, "planets": [],
                                                 "is_ship": bid in ships, "name": names.get(bid, bid),
                                                 "step": (bottlenecks.get(bid) or {}).get("step") or []})
                a["short"] += 1
                if row["planet"] not in a["planets"]:
                    a["planets"].append(row["planet"])
        ph["shortages"] = sorted(agg.values(), key=lambda a: (-a["short"], a["name"].lower()))


def _plan_platoon_conflicts(guild_id: int, readiness: dict, order_id: int | None) -> dict:
    """Готовность к боям с учётом взводов опубликованного ордера плана: кто из «могут зайти»
    отдаёт во взводы того же этапа юниты, без которых бой ему не пройти (в HotUtils «Suggest
    CM teams» подбирает отряды, сохраняя юниты взводов). Юниты во взводах обновляются каждый
    этап, поэтому сравниваем с донатами этапа, на котором открывается планета боя. Строки
    проекции в кэше не трогаем — возвращаем копию readiness."""
    if not order_id:
        return readiness
    assignments = database.get_tb_platoon_assignments(guild_id, order_id)
    if not assignments:
        return readiness
    unit_names = {n for planet, op, _i in assignments for n in tb_platoon_data.ROTE_PLATOON_SUGGESTIONS.get((planet, op)) or []}
    name_to_base_id = database.resolve_unit_display_names(sorted(unit_names)) if unit_names else {}
    donated: dict = {}  # этап -> {ally_code: {base_id}}
    for (planet, op, idx), a in assignments.items():
        units = tb_platoon_data.ROTE_PLATOON_SUGGESTIONS.get((planet, op)) or []
        base_id = name_to_base_id.get(units[idx]) if idx < len(units) else None
        if base_id and a.get("round_num"):
            donated.setdefault(a["round_num"], {}).setdefault(a["ally_code"], set()).add(base_id)
    if not donated:
        return readiness
    names = database.get_player_names(guild_id)
    all_ids = {b for per in donated.values() for s in per.values() for b in s}
    unit_ru = database.get_game_unit_names(sorted(all_ids))
    out = {}
    for planet, rows in readiness.items():
        strikes = []
        for st in rows.get("strikes") or []:
            per = donated.get(st.get("phase")) or {}
            conflicts = []
            for code, units in per.items():
                hit = tb_forecast.platoon_conflict(st, code, units)
                if hit:
                    conflicts.append((names.get(code, code), ", ".join(unit_ru.get(b, b) for b in hit)))
            strikes.append({**st, "platoon_conflict": sorted(conflicts, key=lambda x: x[0].lower())})
        out[planet] = {**rows, "strikes": strikes}
    return out


async def _plan_inputs(guild_id: int) -> dict:
    _built_at, r = await _projection(guild_id, True)
    fc = r.get("forecast")
    if not fc:
        raise HTTPException(status_code=409, detail="Нет данных для расчёта: нужны склады игроков и игровые данные ТБ.")
    return fc["inputs"]


def _plan_or_404(guild_id: int, plan_id: int) -> dict:
    plan = tb_plan.get_plan(guild_id, plan_id)
    if plan is None:
        raise HTTPException(status_code=404, detail="План не найден")
    return plan


def _plan_back(plan_id: int, day: int | None = None, **query) -> RedirectResponse:
    qs = urlencode({"id": plan_id, **{k: v for k, v in query.items() if v}})
    return RedirectResponse(f"/tb/plan?{qs}" + (f"#day-{day}" if day else ""), status_code=303)


def _plan_save(guild_id: int, plan_id: int, inp: dict, data: dict) -> None:
    total = tb_plan.evaluate(inp, data)["total"]
    tb_plan.update_plan(guild_id, plan_id, data=data, stars=total)


def _parse_number(raw: str) -> float | None:
    raw = (raw or "").strip().replace(" ", "").replace(",", ".")
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


@router.get("/tb/plan", response_class=HTMLResponse)
async def tb_plan_page(request: Request, id: int = 0,
                       user: dict = Depends(feature_flags.require_feature("tb_plan_order"))):
    guild_id = user["guild_id"]
    try:
        import_errors = await tb_plan.import_orders(guild_id)
    except Exception as e:  # перенос старых ордеров не должен ронять страницу плана
        import_errors = [f"Не удалось перенести ордера в планы: {e}"]
    plans = tb_plan.list_plans(guild_id)
    active_id = tb_plan.active_plan_id(guild_id, plans)
    plan = next((p for p in plans if p["id"] == (id or active_id)), None) or (plans[0] if plans else None)
    _built_at, r = await _projection(guild_id, True)
    fc = r.get("forecast")
    result = None
    readiness, bottlenecks = _plan_readiness(r)
    if plan and fc:
        result = await asyncio.to_thread(tb_plan.evaluate, fc["inputs"], plan["data"])
        _plan_shortages(result, fc["inputs"], bottlenecks)
    guild_cfg = database.get_guild_config(guild_id) or {}
    active_order_id = int(guild_cfg["tb_active_plan_id"]) if guild_cfg.get("tb_active_plan_id") else None
    order = database.get_tb_saved_plan(plan["order_plan_id"]) if plan and plan.get("order_plan_id") else None
    if order and order["guild_id"] == guild_id:
        readiness = await asyncio.to_thread(_plan_platoon_conflicts, guild_id, readiness, order["id"])
    return templates.TemplateResponse(request, "tb_plan.html", {
        "user": user,
        "plans": plans,
        "plan": plan,
        "result": result,
        "r": r,
        "fc": fc,
        "readiness": readiness,
        "bonus_planets": sorted(((fc or {}).get("inputs") or {}).get("zone_data", {}).get("bonus") or {}),
        "order": order if order and order["guild_id"] == guild_id else None,
        "order_active": bool(order and order["id"] == active_order_id),
        "active_id": active_id,
        "goal_enabled": feature_flags.is_enabled(guild_id, "tb_star_goal"),
        "msg": request.query_params.get("msg"),
        "error": request.query_params.get("error") or "; ".join(import_errors) or None,
        "today": datetime.now(MSK).strftime("%d.%m"),
        "planet_ru": _planet_ru,
        "compact": dashboard_data._compact_number,
    })


@router.post("/tb/plan/new", response_class=HTMLResponse)
async def tb_plan_new(name: str = Form(default=""), copy_id: int = Form(default=0),
                      user: dict = Depends(feature_flags.require_feature("tb_plan_order"))):
    guild_id = user["guild_id"]
    inp = await _plan_inputs(guild_id)
    if copy_id:
        src = _plan_or_404(guild_id, copy_id)
        data, name = src["data"], name.strip() or f"{src['name']} (копия)"
    else:
        data = await asyncio.to_thread(tb_plan.suggest, inp, {})
        name = name.strip() or f"План {datetime.now(MSK).strftime('%d.%m')}"
    total = tb_plan.evaluate(inp, data)["total"]
    plan_id = tb_plan.create_plan(guild_id, name[:80], data, total, f"web:{user['discord_id']}")
    return _plan_back(plan_id)


@router.post("/tb/plan/{plan_id}/stars", response_class=HTMLResponse)
async def tb_plan_stars(plan_id: int, phase: int = Form(...), planet: str = Form(...), stars: int = Form(...),
                        user: dict = Depends(feature_flags.require_feature("tb_plan_order"))):
    guild_id = user["guild_id"]
    plan = _plan_or_404(guild_id, plan_id)
    inp = await _plan_inputs(guild_id)
    if planet not in (inp["zone_data"].get("planets") or {}) or not 1 <= phase <= 6:
        return _plan_back(plan_id, phase, error="Нет такой планеты или этапа")
    data = await asyncio.to_thread(tb_plan.set_stars, inp, plan["data"], phase, planet, stars)
    _plan_save(guild_id, plan_id, inp, data)
    return _plan_back(plan_id, phase)


@router.post("/tb/plan/{plan_id}/preload", response_class=HTMLResponse)
async def tb_plan_preload(plan_id: int, phase: int = Form(...), planet: str = Form(...), amount: str = Form(default=""),
                          user: dict = Depends(feature_flags.require_feature("tb_plan_order"))):
    """До скольких млн копить планету на этапе (пусто — «сколько останется»); этапы после
    него подбираются заново."""
    guild_id = user["guild_id"]
    plan = _plan_or_404(guild_id, plan_id)
    inp = await _plan_inputs(guild_id)
    value = _parse_number(amount)
    data = dict(plan["data"])
    pre = {k: dict(v) for k, v in (data.get("preload") or {}).items()}
    if value is None or value <= 0:
        pre.get(str(phase), {}).pop(planet, None)
    else:
        pre.setdefault(str(phase), {})[planet] = value * 1e6
    data["preload"] = {k: v for k, v in pre.items() if v}
    data = await asyncio.to_thread(tb_plan.suggest, inp, data, phase)
    data["preload"] = {k: v for k, v in pre.items() if v and int(k) <= phase}
    _plan_save(guild_id, plan_id, inp, data)
    return _plan_back(plan_id, phase)


@router.post("/tb/plan/{plan_id}/expect", response_class=HTMLResponse)
async def tb_plan_expect(plan_id: int, planet: str = Form(...), pct: str = Form(default=""), phase: int = Form(default=0),
                         user: dict = Depends(feature_flags.require_feature("tb_plan_order"))):
    """Сколько % боёв планеты ждём (пусто — по истории). План не меняется, меняется расчёт."""
    guild_id = user["guild_id"]
    plan = _plan_or_404(guild_id, plan_id)
    inp = await _plan_inputs(guild_id)
    value = _parse_number(pct)
    data = dict(plan["data"])
    expect = dict(data.get("expect") or {})
    if value is None:
        expect.pop(planet, None)
    else:
        expect[planet] = max(0.0, min(100.0, value))
    data["expect"] = expect
    _plan_save(guild_id, plan_id, inp, data)
    return _plan_back(plan_id, phase or None)


@router.post("/tb/plan/{plan_id}/settings", response_class=HTMLResponse)
async def tb_plan_settings(plan_id: int, deploy_pct: str = Form(default=""), bonus: list[str] = Form(default=[]),
                           user: dict = Depends(feature_flags.require_feature("tb_plan_order"))):
    guild_id = user["guild_id"]
    plan = _plan_or_404(guild_id, plan_id)
    inp = await _plan_inputs(guild_id)
    data = dict(plan["data"])
    value = _parse_number(deploy_pct)
    data["deploy_pct"] = None if value is None else max(10.0, min(100.0, value))
    data["bonus"] = [b for b in bonus if b in (inp["zone_data"].get("bonus") or {})]
    _plan_save(guild_id, plan_id, inp, data)
    return _plan_back(plan_id, msg="Настройки сохранены. Чтобы подобрать звёзды под них — «Подобрать заново».")


@router.post("/tb/plan/{plan_id}/platoons", response_class=HTMLResponse)
async def tb_plan_platoons(plan_id: int, phase: int = Form(...), planet: str = Form(...), off: str = Form(default=""),
                           user: dict = Depends(feature_flags.require_feature("tb_plan_order"))):
    """Взводы на планете этапа не ставим (off=1) / ставим. Звёзды плана не меняются — меняется
    расчёт (операции этой планеты на этапе не закрываются), в ордере будет «Взводы: нет»."""
    guild_id = user["guild_id"]
    plan = _plan_or_404(guild_id, plan_id)
    inp = await _plan_inputs(guild_id)
    data = dict(plan["data"])
    skip = {k: [p for p in v if p != planet] for k, v in (data.get("no_platoons") or {}).items()}
    if off:
        skip.setdefault(str(phase), []).append(planet)
    data["no_platoons"] = {k: v for k, v in skip.items() if v}
    _plan_save(guild_id, plan_id, inp, data)
    return _plan_back(plan_id, phase)


@router.post("/tb/plan/{plan_id}/resuggest", response_class=HTMLResponse)
async def tb_plan_resuggest(plan_id: int, from_day: int = Form(default=1),
                            user: dict = Depends(feature_flags.require_feature("tb_plan_order"))):
    guild_id = user["guild_id"]
    plan = _plan_or_404(guild_id, plan_id)
    inp = await _plan_inputs(guild_id)
    from_day = max(1, min(6, from_day))
    data = await asyncio.to_thread(tb_plan.suggest, inp, plan["data"], from_day - 1)
    _plan_save(guild_id, plan_id, inp, data)
    return _plan_back(plan_id, from_day, msg=f"Этапы {from_day}–6 подобраны заново.")


@router.post("/tb/plan/{plan_id}/rename", response_class=HTMLResponse)
async def tb_plan_rename(plan_id: int, name: str = Form(...),
                         user: dict = Depends(feature_flags.require_feature("tb_plan_order"))):
    _plan_or_404(user["guild_id"], plan_id)
    if name.strip():
        tb_plan.update_plan(user["guild_id"], plan_id, name=name.strip()[:80])
    return _plan_back(plan_id)


@router.post("/tb/plan/{plan_id}/delete", response_class=HTMLResponse)
async def tb_plan_delete(plan_id: int, user: dict = Depends(feature_flags.require_feature("tb_plan_order"))):
    _plan_or_404(user["guild_id"], plan_id)
    tb_plan.delete_plan(user["guild_id"], plan_id)
    return RedirectResponse("/tb/plan", status_code=303)


@router.post("/tb/plan/{plan_id}/activate", response_class=HTMLResponse)
async def tb_plan_activate(plan_id: int, user: dict = Depends(feature_flags.require_feature("tb_plan_order"))):
    """Активный план = активный ордер (guilds.tb_active_plan_id): по нему бот публикует этапы,
    взводы по умолчанию — тоже его."""
    plan = _plan_or_404(user["guild_id"], plan_id)
    order = database.get_tb_saved_plan(plan["order_plan_id"]) if plan.get("order_plan_id") else None
    if not order or order["guild_id"] != user["guild_id"]:
        return _plan_back(plan_id, error="Сначала опубликуйте ордер плана — активным становится опубликованный план.")
    database.update_guild_config(user["guild_id"], tb_active_plan_id=order["id"])
    return _plan_back(plan_id, msg="План активный: бот публикует по нему этапы, взводы открываются по нему.")


@router.post("/tb/plan/{plan_id}/publish", response_class=HTMLResponse)
async def tb_plan_publish(plan_id: int, name: str = Form(default=""), make_active: str = Form(default=""),
                          user: dict = Depends(feature_flags.require_feature("tb_plan_order"))):
    """Ордер из плана → ветка Discord (6 этапов, как у «ордера из картинки») → план ордера
    (database.tb_saved_plans), по желанию — активный."""
    guild_id = user["guild_id"]
    plan = _plan_or_404(guild_id, plan_id)
    inp = await _plan_inputs(guild_id)
    result = tb_plan.evaluate(inp, plan["data"])
    try:
        order_id, message = await tb_plan.publish(guild_id, result, (name.strip() or plan["name"])[:80],
                                                  f"web:{user['discord_id']}")
    except (tb_plan.PublishError, httpx.HTTPError) as e:
        return _plan_back(plan_id, error=str(e))
    tb_plan.update_plan(guild_id, plan_id, order_plan_id=order_id)
    if make_active:
        database.update_guild_config(guild_id, tb_active_plan_id=order_id)
        message += " Ордер выбран активным — бот будет публиковать его по этапам."
    return _plan_back(plan_id, msg=message)


_GOAL_CACHE: dict = {}


@router.get("/tb/goal", response_class=HTMLResponse)
async def tb_goal_page(request: Request, stars: int = 0, bonus: int = 1,
                       user: dict = Depends(feature_flags.require_feature("tb_star_goal"))):
    """«Цель» ВИ-проекции: сколько звёзд нужно → варианты, как их набрать (services/tb_goal.py).
    Расчёт ~10 с, кэшируется вместе с проекцией."""
    from services import tb_goal
    built_at, r = await _projection(user["guild_id"], bool(bonus))
    fc = r.get("forecast")
    goal = None
    if fc and stars:
        stars = max(1, min(stars, fc["max"]))
        key = (user["guild_id"], bool(bonus), stars, built_at)
        if key not in _GOAL_CACHE:
            for old in [k for k in _GOAL_CACHE if k[3] != built_at]:
                del _GOAL_CACHE[old]
            _GOAL_CACHE[key] = await asyncio.to_thread(tb_goal.variants, fc["inputs"], stars, fc)
        goal = _GOAL_CACHE[key]
    return templates.TemplateResponse(request, "tb_goal.html", {
        "user": user,
        "fc": fc,
        "goal": goal,
        "stars": stars or (fc["total"] + 1 if fc else 0),
        "bonus": bool(bonus),
        "planet_ru": _planet_ru,
        "compact": dashboard_data._compact_number,
    })


@router.get("/tb/platoons", response_class=HTMLResponse)
async def tb_platoons(request: Request, user: dict = Depends(feature_flags.require_feature("tb_plan_order"))):
    guild_id = user["guild_id"]
    plan, error = _resolve_viewed_plan(guild_id, request.query_params.get("plan_id"))

    entries: list[dict] = []
    if plan is not None:
        entries, error = await _fetch_plan_planets(plan)

    # Группируем по ЭТАПУ (round), не по планете — по прямому запросу пользователя
    # 2026-08-28 (страница раньше листала одну планету за раз через выпадашку, из-за
    # чего было не видно все планеты конкретного этапа сразу). Один этап реально
    # содержит 2-4 планеты одновременно (тёмная/смешанная/светлая ветка + иногда ещё и
    # бонус-зона Зеффо/Мандалор — см. TB_PLANET_CONFLICT в cogs/guild_events.py), и
    # именно это нужно видеть разом, а не листать планета-за-планетой. Планета,
    # стоящая на нескольких этапах подряд (гильдия не выбрала все звёзды за этап),
    # теперь просто попадает в блок каждого из этих этапов отдельно — дублирование
    # разумно: на разных этапах это всё ещё "то же самое, что нужно фармить сейчас".
    by_round: dict[int, list[dict]] = {}
    seen_in_round = set()
    for e in entries:
        key = (e["round"], e["planet"] or f"raw:{e['raw']}")
        if key in seen_in_round:
            continue
        seen_in_round.add(key)
        by_round.setdefault(e["round"], []).append(e)

    # Выбор этапа теперь СЕРВЕРНЫЙ (?round=), не клиентский тумблер видимости — на всех
    # 6 этапах разом кандидаты на слоты давали десятки тысяч элементов в DOM разом
    # (жалоба "очень долго меняется этап" 2026-08-28); считаем и рендерим только один
    # выбранный этап за раз, остальные — просто пункты в выпадашке ниже.
    round_numbers = sorted(by_round)
    try:
        selected_round_num = int(request.query_params.get("round", ""))
    except ValueError:
        selected_round_num = None
    if selected_round_num not in round_numbers:
        selected_round_num = round_numbers[0] if round_numbers else None

    round_options = []
    for n in round_numbers:
        names = [
            ("⚠ " if e["planet"] is None else "") + (e["planet"] or e["raw"])
            for e in by_round[n]
        ]
        round_options.append({"number": n, "label": f"Этап {n} — " + ", ".join(names)})

    selected_entries = by_round.get(selected_round_num, []) if selected_round_num is not None else []

    # Уникальные названия юнитов ТОЛЬКО выбранного этапа -> base_id -> живые владельцы в
    # гильдии (один батч-запрос вместо N на каждый слот, см. коммент над
    # get_player_unit_owners_bulk в database.py).
    unit_names_this_round = {
        name
        for e in selected_entries
        if e["planet"] and not e.get("no_platoons")
        for operation in range(1, 7)
        for name in (tb_platoon_data.ROTE_PLATOON_SUGGESTIONS.get((e["planet"], operation)) or [])
    }
    name_to_base_id = database.resolve_unit_display_names(list(unit_names_this_round)) if unit_names_this_round else {}
    mappings = database.get_all_user_mappings(guild_id)
    ally_codes = [ally_code for _discord_id, ally_code, _name in mappings]
    player_name_by_ally = _player_labels(guild_id)
    base_ids = sorted({bid for bid in name_to_base_id.values() if bid})
    # Корабли не имеют реликвии (см. tb_platoon_engine.SHIP_MIN_STARS) — донат-требование
    # для них 7★, не порог реликвии этапа.
    unit_types = database.get_unit_types(base_ids) if base_ids else {}
    owners_raw = database.get_player_unit_owners_bulk(ally_codes, base_ids) if base_ids else []
    owners_by_base_id: dict[str, list[dict]] = {}
    for row in owners_raw:
        owners_by_base_id.setdefault(row["base_id"], []).append({
            "ally_code": row["ally_code"],
            "name": player_name_by_ally.get(row["ally_code"], row["ally_code"]),
            "relic": stat_engine.get_current_relic_level(row["unit"]),
            "stars": row["unit"].get("currentRarity", 0),
        })
    for base_id, owners in owners_by_base_id.items():
        is_ship = unit_types.get(base_id) == "ship"
        owners.sort(key=lambda o: -(o["stars"] if is_ship else o["relic"]))
        del owners[PLATOON_CANDIDATES_LIMIT:]

    assignments = database.get_tb_platoon_assignments(guild_id, plan["id"]) if plan else {}

    # Фильтры автозаполнения (tb_platoon_filters.py) применяются и здесь, к ручной
    # расстановке — единая точка правды, см. tb_platoon_engine.py. Ошибки разбора текста
    # молча игнорируются на этой странице (страница /tb/platoons/filters не даёт сохранить
    # текст с ошибками — значит то, что лежит в БД, уже валидно; пустой текст парсится в
    # пустой ParsedRules без ошибок).
    filter_rules, _filter_errors = tb_platoon_filters.parse_rules(
        database.get_tb_platoon_filter_rules(guild_id), guild_id,
    )

    # В рамках ОДНОГО этапа игрок не может задонатить одного и того же юнита дважды —
    # прямой запрос пользователя 2026-08-28 ("если 0-0-0 стоит на Мустафаре на 1 этапе
    # на 3 операции, второй раз его на 1 этапе поставить нельзя"). Считаем по всем
    # назначениям ПЛАНЕТ, показанных на выбранном этапе, разом: (ally_code, base_id) ->
    # где уже стоит. Планета, растянутая на 2+ этапа (не зачищена целиком), теперь всегда
    # входит в selected_entries того этапа, на котором её сейчас смотрят — её назначения
    # (уже не привязанные к round_num, см. database.py::_ensure_tb_platoon_assignments_table)
    # подхватываются здесь так же, как назначения любой другой планеты этого этапа.
    planets_this_round = {e["planet"] for e in selected_entries if e["planet"] and not e.get("no_platoons")}
    used_pairs_this_round = tb_platoon_engine.compute_used_pairs(assignments, planets_this_round, name_to_base_id, selected_round_num)

    # Лимит "не больше 10 юнитов на планету от игрока" — по этапу ПРОСМОТРА (round_num
    # первого назначения слота, не по планете целиком), см. tb_platoon_engine.py.
    round_counts = tb_platoon_engine.compute_round_counts(assignments, selected_round_num) if selected_round_num else {}

    hold_flags = database.get_tb_platoon_holds(guild_id, plan["id"]) if plan else {}

    # Автоматическое "держим": планета, которая ещё встречается на более позднем этапе
    # этого же плана, ещё не зачищена целиком — по прямому запросу пользователя 2026-08-29
    # ("на 3 этапе есть Датомир и на 4 этапе есть Датомир — значит на 3 этапе взводы не
    # нужно заполнять полностью"), без ручного тумблера. Тот же расчёт использует
    # tb_platoon_autofill.py — здесь он только для бейджа "держим" на странице, сама
    # логика неполного заполнения — в автозаполнении.
    planet_last_round: dict[str, int] = {}
    for rn, es in by_round.items():
        for e2 in es:
            if e2["planet"]:
                planet_last_round[e2["planet"]] = max(planet_last_round.get(e2["planet"], rn), rn)

    # Сводка "сколько юнитов не можем поставить на этапе" — незаполненные слоты, у которых
    # прямо сейчас НЕТ ни одного подходящего кандидата (никто не проходит по релику/★,
    # либо исключён фильтром/лимитом/занят в другом месте этого этапа) — не просто "ещё не
    # назначено вручную". Считается по всем планетам выбранного этапа разом.
    unplaceable_count = 0

    planet_blocks = []
    for planet_idx, e in enumerate(selected_entries):
        # Минимум фиксирован ЗА ПЛАНЕТОЙ (tb_platoon_data.ROTE_MIN_RELIC_BY_PLANET), не за
        # этапом гильдии — см. комментарий там же.
        min_relic = tb_platoon_data.ROTE_MIN_RELIC_BY_PLANET.get(e["planet"]) if e["planet"] else None
        min_relic_label = f"R{min_relic}+" if min_relic is not None else None
        operations = []
        # Планета с явным "Взводы: нет" в тексте ордера (см. tb_plan_reader.py::
        # fetch_plan_planets) — конструктор её пропускает целиком, ни одного слота не
        # строится и не заполняется автозаполнением (прямая жалоба пользователя: план
        # прямым текстом говорит "не бьём Малакор на 6 этапе", а страница всё равно
        # предлагала 90 слотов и автозаполнение их занимало).
        for operation in range(1, 7) if e["planet"] and not e.get("no_platoons") else []:
            unit_names = tb_platoon_data.ROTE_PLATOON_SUGGESTIONS.get((e["planet"], operation)) if e["planet"] else None
            slots = []
            for slot_index, unit_name in enumerate(unit_names or []):
                base_id = name_to_base_id.get(unit_name)
                owners = owners_by_base_id.get(base_id, []) if base_id else []
                assignment = tb_platoon_engine.visible_assignment(
                    assignments.get((e["planet"], operation, slot_index)), selected_round_num,
                )
                here = (e["planet"], operation, slot_index)
                slot_owners = tb_platoon_engine.slot_candidates(
                    owners=owners, base_id=base_id, here=here, used_pairs=used_pairs_this_round,
                    min_relic=min_relic, round_num=selected_round_num, planet=e["planet"],
                    filter_rules=filter_rules, round_counts=round_counts,
                    is_ship=unit_types.get(base_id) == "ship",
                )
                # Исключение юнита/категории (флот/пешка) теперь считается один раз внутри
                # slot_candidates (владельцы уже приходят с excluded_by_filter=True) — раньше
                # здесь был отдельный патч поверх owners, дублирующий ту же проверку.
                if not assignment and not any(tb_platoon_engine.is_eligible(o) for o in slot_owners):
                    unplaceable_count += 1
                unit_excluded = bool(base_id and (
                    filter_rules.is_unit_excluded(base_id)
                    or filter_rules.is_category_excluded("ship" if unit_types.get(base_id) == "ship" else "character")
                ))
                assigned_round_num = assignment["round_num"] if assignment else None
                slots.append({
                    "index": slot_index,
                    "unit": unit_name,
                    "base_id": base_id,
                    "owners": slot_owners,
                    "unit_excluded": unit_excluded,
                    "assigned_name": player_name_by_ally.get(assignment["ally_code"], assignment["ally_code"]) if assignment else None,
                    "assigned_ally_code": assignment["ally_code"] if assignment else None,
                    # "Донат сделан НА БОЛЕЕ РАННЕМ этапе, чем сейчас смотрим" — планета,
                    # растянутая на 2+ этапа (см. tb_platoon_engine.visible_assignment):
                    # слот уже виден заполненным на этом этапе, но заполнен он был раньше —
                    # прямой запрос пользователя 2026-08-31 "нужно обозначить, что по плану
                    # эти юниты уже размещены" (затемнение), не путать со свежим назначением
                    # на ЭТОМ этапе.
                    "carried_over": assigned_round_num is not None and assigned_round_num < selected_round_num,
                    "assigned_round_num": assigned_round_num,
                    "anchor": f"slot-{selected_round_num}-{planet_idx}-{operation}-{slot_index}",
                })
            # Незаназначенные слоты — первыми в списке операции, т.к. на позднем этапе
            # большинство слотов обычно уже заблокировано (перенесено с более раннего
            # этапа) и реально требующие внимания слоты тонут среди них — прямой запрос
            # пользователя 2026-08-31. Заполненные на ЭТОМ этапе — следом, перенесённые
            # (carried_over) — в самом конце. slot_index (и anchor/forms) не меняется,
            # сортировка влияет только на порядок отображения.
            slots.sort(key=lambda s: 0 if not s["assigned_ally_code"] else (2 if s["carried_over"] else 1))
            operations.append({
                "number": operation,
                "slots": slots,
                "filled": sum(1 for s in slots if s["assigned_ally_code"]),
                "total": len(slots),
            })
        auto_held = bool(e["planet"] and planet_last_round.get(e["planet"], selected_round_num) > selected_round_num)
        planet_blocks.append({
            "name": e["planet"] or e["raw"],
            "unresolved": e["planet"] is None,
            "no_platoons": bool(e.get("no_platoons")),
            "operations": operations,
            "min_relic_label": min_relic_label,
            "held": auto_held or bool(e["planet"] and hold_flags.get((selected_round_num, e["planet"]))),
            "auto_held": auto_held,
            # Бейдж в свёрнутом виде заголовка планеты (см. .platoon-planet-block ->
            # <details>) — по прямому запросу пользователя 2026-08-31 "видно из кучи
            # текста", чтобы не нужно было разворачивать блок, чтобы понять, чего не
            # хватает.
            "unfilled_count": sum(1 for op in operations for s in op["slots"] if not s["assigned_ally_code"]),
        })

    saved_plans = database.get_tb_saved_plans(guild_id)
    guild_cfg = database.get_guild_config(guild_id) or {}
    active_plan_id = int(guild_cfg["tb_active_plan_id"]) if guild_cfg.get("tb_active_plan_id") else None

    # Вид "по игрокам" — та же построенная planet_blocks, просто перегруппированная
    # по assigned_ally_code вместо планеты/операции — по прямому запросу пользователя
    # 2026-09-14 ("кто какого персонажа и куда ставит"), только для выбранного этапа.
    # Переключатель вида не трогает БД и не пересчитывает slot_candidates.
    player_rows: dict[str, dict] = {}
    for pblock in planet_blocks:
        if pblock["unresolved"] or pblock["no_platoons"]:
            continue
        for op in pblock["operations"]:
            for slot in op["slots"]:
                if not slot["assigned_ally_code"]:
                    continue
                # "entries", не "items" — коллизия с dict.items() ловится в Jinja
                # молча: {{ pl.items }} резолвится в getattr(pl, "items") (bound-метод
                # словаря), а не в ключ "items", и падает TypeError при len() только в
                # рантайме шаблона (найдено 2026-09-14 прямо на проде, см. трейсбек).
                row = player_rows.setdefault(slot["assigned_ally_code"], {
                    "name": slot["assigned_name"],
                    "ally_code": slot["assigned_ally_code"],
                    "entries": [],
                })
                row["entries"].append({
                    "planet": pblock["name"],
                    "operation": op["number"],
                    "unit": slot["unit"],
                    "base_id": slot["base_id"],
                    "carried_over": slot["carried_over"],
                    "assigned_round_num": slot["assigned_round_num"],
                    "anchor": slot["anchor"],
                })
    player_blocks = sorted(player_rows.values(), key=lambda p: p["name"])
    for p in player_blocks:
        p["entries"].sort(key=lambda i: (i["planet"], i["operation"]))

    view = request.query_params.get("view", "planet")
    if view not in ("planet", "player"):
        view = "planet"

    return templates.TemplateResponse(request, "tb_platoons.html", {
        "user": user,
        "round_options": round_options,
        "selected_round_num": selected_round_num,
        "planets": planet_blocks,
        "players": player_blocks,
        "view": view,
        "error": error,
        "plan": plan,
        "step_plan": tb_plan.plan_for_order(guild_id, plan["id"]) if plan else None,
        "saved_plans": saved_plans,
        "active_plan_id": active_plan_id,
        "autofill_summary": request.query_params.get("autofill_summary"),
        "autofill_warning": request.query_params.get("autofill_warning"),
        "unplaceable_count": unplaceable_count,
    })


@router.post("/tb/platoons/assign", response_class=HTMLResponse)
async def tb_platoons_assign(
    plan_id: int = Form(...),
    round_num: int = Form(...),
    planet: str = Form(...),
    operation: int = Form(...),
    slot_index: int = Form(...),
    ally_code: str = Form(...),
    anchor: str = Form(""),
    user: dict = Depends(feature_flags.require_feature("tb_plan_order")),
):
    guild_id = user["guild_id"]
    plan = database.get_tb_saved_plan(plan_id)
    if not plan or plan["guild_id"] != guild_id:
        return RedirectResponse(f"/tb/platoons?{urlencode({'error': 'План не найден'})}", status_code=303)
    database.set_tb_platoon_assignment(
        guild_id, plan_id, round_num, planet, operation, slot_index, ally_code.strip(),
        assigned_by=f"web:{user['discord_id']}",
    )
    suffix = f"#{anchor}" if anchor else ""
    return RedirectResponse(f"/tb/platoons?plan_id={plan_id}&round={round_num}{suffix}", status_code=303)


@router.post("/tb/platoons/unassign", response_class=HTMLResponse)
async def tb_platoons_unassign(
    plan_id: int = Form(...),
    round_num: int = Form(...),
    planet: str = Form(...),
    operation: int = Form(...),
    slot_index: int = Form(...),
    anchor: str = Form(""),
    user: dict = Depends(feature_flags.require_feature("tb_plan_order")),
):
    guild_id = user["guild_id"]
    plan = database.get_tb_saved_plan(plan_id)
    if not plan or plan["guild_id"] != guild_id:
        return RedirectResponse(f"/tb/platoons?{urlencode({'error': 'План не найден'})}", status_code=303)
    database.clear_tb_platoon_assignment(guild_id, plan_id, planet, operation, slot_index)
    suffix = f"#{anchor}" if anchor else ""
    return RedirectResponse(f"/tb/platoons?plan_id={plan_id}&round={round_num}{suffix}", status_code=303)


_AUTOFILL_REASON_LABELS = {
    "no_owner": "нет владельцев в гильдии",
    "no_eligible_owner": "нет подходящих доноров",
    "unit_excluded": "юнит исключён фильтром",
    "unit_not_resolved": "юнит не распознан",
}


@router.post("/tb/platoons/autofill", response_class=HTMLResponse)
async def tb_platoons_autofill_route(
    plan_id: int = Form(...),
    round_num: int = Form(...),
    user: dict = Depends(feature_flags.require_feature("tb_plan_order")),
):
    """Заполняет все ещё пустые слоты ВСЕГО плана (все распознанные этапы сразу, не
    только открытый) — tb_platoon_autofill.py. round_num здесь — только чтобы вернуть
    пользователя на тот же этап после редиректа, на сам алгоритм не влияет."""
    guild_id = user["guild_id"]
    plan = database.get_tb_saved_plan(plan_id)
    if not plan or plan["guild_id"] != guild_id:
        return RedirectResponse(f"/tb/platoons?{urlencode({'error': 'План не найден'})}", status_code=303)

    try:
        result = await tb_platoon_autofill.autofill_plan(guild_id, plan_id)
    except (ValueError, RuntimeError) as e:
        return RedirectResponse(
            f"/tb/platoons?plan_id={plan_id}&round={round_num}&{urlencode({'error': str(e)})}", status_code=303,
        )

    reasons: dict[str, int] = {}
    for o in result.unfilled:
        if o.reason == "held_back":
            continue
        reasons[o.reason] = reasons.get(o.reason, 0) + 1

    summary = f"Автозаполнение: {result.filled_slots}/{result.total_slots} слотов занято"
    if result.held_back:
        summary += f", {result.held_back} намеренно не добито («держим»)"
    if reasons:
        parts = [f"{_AUTOFILL_REASON_LABELS.get(reason, reason)}: {count}" for reason, count in reasons.items()]
        summary += " — не хватает (" + ", ".join(parts) + ")"

    params = {"autofill_summary": summary}
    if result.blocked_by_exclude_rule:
        # Отдельный видный алерт (не просто строчка в общей сводке) — прямой запрос
        # пользователя 2026-09-02: если правило "exclude player [...] unit [...]" — ЕДИНСТВЕННАЯ
        # причина, по которой слот не заполнен (кто-то стал бы подходящим донором, если бы не
        # оно), предупредить явно, с указанием какого игрока и юнита это касается — см.
        # tb_platoon_autofill.py::AutofillResult.blocked_by_exclude_rule.
        by_unit: dict[str, set] = {}
        for item in result.blocked_by_exclude_rule:
            by_unit.setdefault(item.unit, set()).update(item.blocked_players)
        details = [f"{unit} (мог(ли) задонатить: {', '.join(sorted(players))})" for unit, players in sorted(by_unit.items())]
        params["autofill_warning"] = (
            f"⚠ Правило-исключение блокирует {len(result.blocked_by_exclude_rule)} слот(ов), "
            "которые иначе были бы заполнены: " + "; ".join(details)
        )

    suffix = urlencode(params)
    return RedirectResponse(f"/tb/platoons?plan_id={plan_id}&round={round_num}&{suffix}", status_code=303)


@router.post("/tb/platoons/hold", response_class=HTMLResponse)
async def tb_platoons_hold_route(
    plan_id: int = Form(...),
    round_num: int = Form(...),
    planet: str = Form(...),
    held: str = Form(...),
    user: dict = Depends(feature_flags.require_feature("tb_plan_order")),
):
    guild_id = user["guild_id"]
    plan = database.get_tb_saved_plan(plan_id)
    if not plan or plan["guild_id"] != guild_id:
        return RedirectResponse(f"/tb/platoons?{urlencode({'error': 'План не найден'})}", status_code=303)
    database.set_tb_platoon_hold(guild_id, plan_id, round_num, planet, held == "1", set_by=f"web:{user['discord_id']}")
    return RedirectResponse(f"/tb/platoons?plan_id={plan_id}&round={round_num}", status_code=303)


@router.post("/tb/platoons/clear", response_class=HTMLResponse)
async def tb_platoons_clear_route(
    plan_id: int = Form(...),
    round_num: int = Form(...),
    scope: str = Form(...),  # "operation" / "planet" / "round" / "plan"
    planet: str = Form(""),
    operation: int = Form(0),
    user: dict = Depends(feature_flags.require_feature("tb_plan_order")),
):
    """Кнопки «очистить» на /tb/platoons — по операции/планете/этапу/всему плану. "Этап"
    чистит все планеты, показанные на этом этапе (включая перенесённые с прошлого этапа —
    они целиком общие с ним, см. database.py::_ensure_tb_platoon_assignments_table), не
    только те, что впервые появились именно тут."""
    guild_id = user["guild_id"]
    plan = database.get_tb_saved_plan(plan_id)
    if not plan or plan["guild_id"] != guild_id:
        return RedirectResponse(f"/tb/platoons?{urlencode({'error': 'План не найден'})}", status_code=303)

    if scope == "operation" and planet and operation:
        database.clear_tb_platoon_assignments_for_operation(guild_id, plan_id, planet, operation)
    elif scope == "planet" and planet:
        database.clear_tb_platoon_assignments_for_planet(guild_id, plan_id, planet)
    elif scope == "round":
        entries, fetch_error = await tb_plan_reader.fetch_plan_planets(plan)
        if not fetch_error:
            for p in {e["planet"] for e in entries if e["planet"] and e["round"] == round_num}:
                database.clear_tb_platoon_assignments_for_planet(guild_id, plan_id, p)
    elif scope == "plan":
        database.clear_tb_platoon_assignments_for_plan(guild_id, plan_id)

    return RedirectResponse(f"/tb/platoons?plan_id={plan_id}&round={round_num}", status_code=303)


@router.get("/tb/platoons/api/units", response_class=JSONResponse)
async def tb_platoons_units_search(q: str = "", user: dict = Depends(feature_flags.require_feature("tb_plan_order"))):
    """Подстрочный поиск юнита по имени (RU/EN) для интерактивного автодополнения на
    /tb/platoons/filters (см. web/static/dashboard.js — контекст "unit") — по прямому
    запросу пользователя 2026-08-31. Тот же database.search_game_units, что и
    web/routes/stat_plates.py::units_search, отображаемое имя (cached_name) точно
    резолвится обратно в base_id в tb_platoon_filters.parse_rules."""
    if not q or len(q.strip()) < 2:
        return []
    rows = database.search_game_units(q.strip(), limit=20)
    return [{"base_id": base_id, "name": name} for base_id, name in rows]


@router.get("/tb/platoons/api/slot", response_class=JSONResponse)
async def tb_platoons_slot_candidates_api(request: Request, user: dict = Depends(feature_flags.require_feature("tb_plan_order"))):
    """Полный список кандидатов на один слот взвода (планета/операция/индекс), без
    урезания до PLATOON_CANDIDATES_LIMIT и включая игроков, которые юнитом вообще не
    владеют — для попапа выбора игрока на /tb/platoons (по прямому запросу пользователя
    2026-09-14: "видеть ВСЕХ игроков, с явной отметкой, кто уже поставил/может/не может").
    Раньше этот же список рендерился инлайн прямо в узкой колонке .platoon-op-card, топ-20
    по релику — здесь лимита нет: гильдия небольшая (~50 игроков), полный список безопасен
    для одного JSON-ответа по требованию, в отличие от рендера сразу всех слотов страницы."""
    guild_id = user["guild_id"]
    try:
        plan_id = int(request.query_params.get("plan_id", ""))
        round_num = int(request.query_params.get("round", ""))
        operation = int(request.query_params.get("operation", ""))
        slot_index = int(request.query_params.get("slot_index", ""))
    except ValueError:
        raise HTTPException(400, "Некорректные параметры слота.")
    planet = request.query_params.get("planet", "")
    if not planet:
        raise HTTPException(400, "Не указана планета.")

    plan = database.get_tb_saved_plan(plan_id)
    if not plan or plan["guild_id"] != guild_id:
        raise HTTPException(404, "План не найден.")

    entries, fetch_error = await _fetch_plan_planets(plan)
    if fetch_error:
        raise HTTPException(400, fetch_error)

    seen = set()
    round_entries = []
    for e in entries:
        if e["round"] != round_num:
            continue
        key = e["planet"] or f"raw:{e['raw']}"
        if key in seen:
            continue
        seen.add(key)
        round_entries.append(e)

    unit_names_this_round = {
        name
        for e in round_entries
        if e["planet"] and not e.get("no_platoons")
        for op in range(1, 7)
        for name in (tb_platoon_data.ROTE_PLATOON_SUGGESTIONS.get((e["planet"], op)) or [])
    }
    name_to_base_id = database.resolve_unit_display_names(list(unit_names_this_round)) if unit_names_this_round else {}

    unit_names = tb_platoon_data.ROTE_PLATOON_SUGGESTIONS.get((planet, operation)) or []
    if not (0 <= slot_index < len(unit_names)):
        raise HTTPException(404, "Слот не найден.")
    unit_name = unit_names[slot_index]
    base_id = name_to_base_id.get(unit_name)

    mappings = database.get_all_user_mappings(guild_id)
    ally_codes = [ally_code for _discord_id, ally_code, _name in mappings]
    player_name_by_ally = _player_labels(guild_id)

    unit_types = database.get_unit_types([base_id]) if base_id else {}
    is_ship = unit_types.get(base_id) == "ship"

    owners = []
    if base_id:
        owners_raw = database.get_player_unit_owners_bulk(ally_codes, [base_id])
        for row in owners_raw:
            owners.append({
                "ally_code": row["ally_code"],
                "name": player_name_by_ally.get(row["ally_code"], row["ally_code"]),
                "relic": stat_engine.get_current_relic_level(row["unit"]),
                "stars": row["unit"].get("currentRarity", 0),
            })
    # Без .sort()+del[:LIMIT], как на основной странице — тут нужен полный список.

    assignments = database.get_tb_platoon_assignments(guild_id, plan["id"])
    filter_rules, _errs = tb_platoon_filters.parse_rules(database.get_tb_platoon_filter_rules(guild_id), guild_id)
    planets_this_round = {e["planet"] for e in round_entries if e["planet"] and not e.get("no_platoons")}
    used_pairs_this_round = tb_platoon_engine.compute_used_pairs(assignments, planets_this_round, name_to_base_id, round_num)
    round_counts = tb_platoon_engine.compute_round_counts(assignments, round_num)
    min_relic = tb_platoon_data.ROTE_MIN_RELIC_BY_PLANET.get(planet)
    here = (planet, operation, slot_index)

    candidates = tb_platoon_engine.slot_candidates(
        owners=owners, base_id=base_id, here=here, used_pairs=used_pairs_this_round,
        min_relic=min_relic, round_num=round_num, planet=planet,
        filter_rules=filter_rules, round_counts=round_counts, is_ship=is_ship,
    )
    for c in candidates:
        c["owns_unit"] = True

    have_ally_codes = {c["ally_code"] for c in candidates}
    for ally_code in ally_codes:
        if ally_code in have_ally_codes:
            continue
        count_here = round_counts.get((ally_code, planet), 0)
        candidates.append({
            "ally_code": ally_code, "name": player_name_by_ally.get(ally_code, ally_code),
            "relic": 0, "stars": 0, "owns_unit": False,
            "meets_min": False, "used_elsewhere": False, "used_at_label": None,
            "excluded_by_filter": bool(base_id) and (
                filter_rules.is_unit_excluded(base_id)
                or filter_rules.is_category_excluded("ship" if is_ship else "character")
                or filter_rules.is_player_excluded(ally_code)
                or filter_rules.is_player_unit_excluded(ally_code, base_id, round_num)
            ),
            "at_cap": count_here >= tb_platoon_engine.MAX_UNITS_PER_PLANET_PER_ROUND,
            "count_here": count_here,
            "is_ship": is_ship,
            "is_priority": filter_rules.is_player_priority(ally_code),
        })

    assignment = tb_platoon_engine.visible_assignment(assignments.get((planet, operation, slot_index)), round_num)
    assigned_ally_code = assignment["ally_code"] if assignment else None

    # Последний вход (часовой синк состава): в попапе «не заходил N дн.», как в HotUtils.
    activity = database.get_member_activity(guild_id)
    now = time.time()
    for c in candidates:
        last = (activity.get(c["ally_code"]) or (None, None))[0]
        c["idle_hours"] = int((now - last) // 3600) if last else None
        c["inactive_rule"] = c["ally_code"] in filter_rules.inactive_codes
        if c["ally_code"] == assigned_ally_code:
            c["status"] = "assigned"
        elif tb_platoon_engine.is_eligible(c):
            c["status"] = "eligible"
        else:
            c["status"] = "ineligible"

    candidates.sort(key=lambda c: c["name"])

    return {
        "unit": unit_name,
        "min_relic": min_relic,
        "is_ship": is_ship,
        "assigned_ally_code": assigned_ally_code,
        "candidates": candidates,
    }


@router.get("/tb/platoons/filters", response_class=HTMLResponse)
async def tb_platoons_filters_page(request: Request, user: dict = Depends(feature_flags.require_feature("tb_plan_order"))):
    guild_id = user["guild_id"]
    rules_text = database.get_tb_platoon_filter_rules(guild_id)
    parsed, errors = tb_platoon_filters.parse_rules(rules_text, guild_id)
    return templates.TemplateResponse(request, "tb_platoon_filters.html", {
        "user": user,
        "rules_text": rules_text,
        "described": tb_platoon_filters.describe_rules(parsed) if not errors else [],
        "errors": errors,
        "saved": False,
    })


@router.post("/tb/platoons/filters", response_class=HTMLResponse)
async def tb_platoons_filters_save(
    request: Request,
    rules_text: str = Form(""),
    user: dict = Depends(feature_flags.require_feature("tb_plan_order")),
):
    guild_id = user["guild_id"]
    parsed, errors = tb_platoon_filters.parse_rules(rules_text, guild_id)
    if not errors:
        database.set_tb_platoon_filter_rules(guild_id, rules_text, updated_by=f"web:{user['discord_id']}")
    return templates.TemplateResponse(request, "tb_platoon_filters.html", {
        "user": user,
        "rules_text": rules_text,
        "described": tb_platoon_filters.describe_rules(parsed) if not errors else [],
        "errors": errors,
        "saved": not errors,
    })


# =====================================================================
# Приоритет омикронов для ВГ — план "Приоритеты омикронов для ВГ"
# (~/.claude/plans/lively-noodling-moler.md), для Димы (гильд-админ). Изначально было три
# отдельные страницы; по прямому запросу пользователя 2026-09-02 приоритет (drag-list) и
# правила (textarea) сведены в одну страницу /omicrons/priority, двумя колонками —
# /omicrons/priority/rules теперь только редиректит туда же (не удалён совсем, чтобы не
# ломать старые закладки/ссылки). /omicrons/report (гильд-wide + по игроку) — отдельно.
# =====================================================================
def _omicron_priority_context(guild_id: int, rules_text: str = None) -> dict:
    """Общий контекст для GET/POST /omicrons/priority — обе колонки строятся всегда разом,
    т.к. они теперь на одной странице (сохранение одной колонки должно тут же показать
    актуальное состояние обеих)."""
    if rules_text is None:
        rules_text = database.get_guild_omicron_requirement_rules(guild_id)
    parsed, errors = guild_omicron_rules.parse_rules(rules_text, guild_id)
    return {
        "rows": omicron_priority.priority_list_display(guild_id),
        # limit с большим запасом — это весь ВГ-каталог для выпадающего списка (modes по
        # умолчанию = только "ВГ", см. database.search_omicron_catalog_for_priority), не
        # постраничный поиск.
        "catalog_options": database.search_omicron_catalog_for_priority("", guild_id, limit=1000),
        "rules_text": rules_text,
        "described": guild_omicron_rules.describe_rules(parsed) if not errors else [],
        "errors": errors,
    }


@router.get("/omicrons/priority", response_class=HTMLResponse)
async def omicrons_priority_page(request: Request, user: dict = Depends(feature_flags.require_feature("omicron"))):
    guild_id = user["guild_id"]
    return templates.TemplateResponse(request, "omicron_priority.html", {
        "user": user,
        **_omicron_priority_context(guild_id),
        "saved_priority": False,
        "saved_rules": False,
    })


@router.post("/omicrons/priority", response_class=HTMLResponse)
async def omicrons_priority_save(
    request: Request,
    skill_ids: str = Form(""),
    user: dict = Depends(feature_flags.require_feature("omicron")),
):
    guild_id = user["guild_id"]
    ordered = [s for s in skill_ids.split(",") if s.strip()]
    database.set_guild_omicron_priority(guild_id, ordered)
    return templates.TemplateResponse(request, "omicron_priority.html", {
        "user": user,
        **_omicron_priority_context(guild_id),
        "saved_priority": True,
        "saved_rules": False,
    })


@router.get("/omicrons/api/search", response_class=JSONResponse)
async def omicrons_priority_search(q: str = "", all: str = "", user: dict = Depends(feature_flags.require_feature("omicron"))):
    """Поиск омикронов для добавления в приоритетный список — по умолчанию только "ВГ" (по
    прямому запросу пользователя 2026-09-02 "оставить только те, что ВГшные", сузили с
    исходного ВГ+ТБ), ?all=1 снимает фильтр по игровому режиму (см. план). Сама страница
    /omicrons/priority теперь рендерит весь каталог сразу выпадающим списком (см.
    _omicron_priority_context), этот эндпоинт оставлен на случай, если понадобится текстовый
    поиск по каталогу где-то ещё."""
    guild_id = user["guild_id"]
    modes = None if all == "1" else ("ВГ",)
    return database.search_omicron_catalog_for_priority(q.strip(), guild_id, modes=modes)


@router.get("/omicrons/api/abilities", response_class=JSONResponse)
async def omicrons_priority_ability_search(unit: str = "", q: str = "", user: dict = Depends(feature_flags.require_feature("omicron"))):
    """Автодополнение названия способности после "Юнит: " внутри скобок [...] на
    /omicrons/priority (см. web/static/dashboard.js, textarea .omicron-rules-textarea) — нужно
    только когда у юнита несколько омикрон-способностей (guild_omicron_rules._resolve_omicron_target
    требует уточнения в этом случае). unit резолвится точно (database.resolve_unit_display_names,
    как в guild_omicron_rules.parse_rules), q — подстрочный поиск по названию способности."""
    unit = unit.strip()
    if not unit:
        return []
    base_id = database.resolve_unit_display_names([unit]).get(unit)
    if not base_id:
        return []
    skill_ids = database.get_all_unit_omicron_skills().get(base_id, [])
    if not skill_ids:
        return []
    display = database.get_skill_display_info(skill_ids)
    q_lower = q.strip().lower()
    names = [info[0] for info in display.values() if info[0] and (not q_lower or q_lower in info[0].lower())]
    names.sort()
    return [{"name": n} for n in names]


@router.get("/omicrons/priority/rules", response_class=RedirectResponse)
async def omicrons_priority_rules_page_redirect():
    """Правила теперь показаны прямо на /omicrons/priority (правая колонка) — редирект
    сохранён ради старых закладок/ссылок на прежний отдельный URL."""
    return RedirectResponse("/omicrons/priority", status_code=308)


@router.post("/omicrons/priority/rules", response_class=HTMLResponse)
async def omicrons_priority_rules_save(
    request: Request,
    rules_text: str = Form(""),
    user: dict = Depends(feature_flags.require_feature("omicron")),
):
    guild_id = user["guild_id"]
    parsed, errors = guild_omicron_rules.parse_rules(rules_text, guild_id)
    if not errors:
        database.set_guild_omicron_requirement_rules(guild_id, rules_text, updated_by=f"web:{user['discord_id']}")
    return templates.TemplateResponse(request, "omicron_priority.html", {
        "user": user,
        **_omicron_priority_context(guild_id, rules_text=rules_text),
        "saved_priority": False,
        "saved_rules": not errors,
    })


@router.get("/omicrons/report", response_class=HTMLResponse)
async def omicrons_report(request: Request, user: dict = Depends(feature_flags.require_feature("omicron"))):
    guild_id = user["guild_id"]
    rows = omicron_priority.missing_omicrons_report(guild_id)
    for row in rows:
        row["player_url"] = f"/omicrons/report/{quote(row['name'])}"

    skill_filter_options = omicron_priority.missing_omicrons_by_skill(guild_id)
    modes = sorted({e["omicron_mode"] for e in skill_filter_options if e["omicron_mode"]})

    return templates.TemplateResponse(request, "omicron_report.html", {
        "user": user,
        "rows": rows,
        "has_priority": bool(database.get_guild_omicron_priority(guild_id)),
        "skill_filter_options": skill_filter_options,
        "modes": modes,
    })


def _player_labels(guild_id: int) -> dict:
    """{ally_code: имя} для отображения сохранённых назначений — выбывший из гильдии
    помечается, чтобы офицер видел, что слот надо переназначить."""
    names = database.get_player_names(guild_id)
    for code in database.get_departed_players(guild_id):
        if code in names:
            names[code] = f"{names[code]} (выбыл)"
    return names


@router.get("/omicrons/report/{name}", response_class=HTMLResponse)
async def omicrons_report_player(name: str, request: Request, user: dict = Depends(feature_flags.require_feature("omicron"))):
    guild_id = user["guild_id"]
    hit = next(
        ((ally_code, ingame_name) for _discord_id, ally_code, ingame_name in database.get_all_user_mappings(guild_id)
         if (ingame_name or "").strip().lower() == name.strip().lower()),
        None,
    )
    if not hit:
        raise HTTPException(status_code=404, detail=f"Игрок «{name}» не найден в гильдии")
    ally_code, ingame_name = hit
    missing = omicron_priority.missing_omicrons_for_player(ally_code, guild_id)
    return templates.TemplateResponse(request, "omicron_report_player.html", {
        "user": user,
        "player_name": ingame_name,
        "missing": missing,
        "has_priority": bool(database.get_guild_omicron_priority(guild_id)),
    })


# =====================================================================
# Экспорт расставленных взводов в формат HotUtils/EchoBase Platoon Assigner — по прямому
# запросу пользователя 2026-08-28, снявшего два живых примера импорта с рабочего стола:
# echobase-assignments-ROTE-P1_1_1-<ts>.json (этап 1, только 3 обычные зоны) и
# echobase-assignments-ROTE-P4_M4_Z4-<ts>.json (этап 4, + бонус-зоны Зеффо/Мандалора) —
# и попросившего собирать такой же файл из назначений /tb/platoons, чтобы завозить их
# обратно в HotUtils. Схема файла: {"phase": "N/N/N", "timestamp": ISO8601,
# "platoonAssignments": [{"allyCode", "unitBaseId", "zoneId", "platoonDefinitionId"}, ...]}.
# zoneId = "tb3_mixed_phase{round:02d}_conflict{code}[_bonus]_recon01", code — из
# _PLANET_CONFLICT_CODE, "_bonus" — только для Zeffo/Mandalore (см. _BONUS_PLANETS и
# комментарий у _PLANET_CONFLICT_CODE про то, как это подтверждено). platoonDefinitionId =
# "tb3-platoon-{operation}" — подтверждено тем же сравнением (6 операций конструктора = 6
# tb3-platoon-N в обоих примерах). Поле "phase" в самом файле по-прежнему НЕ проверено на
# реальном импорте: во втором примере оно "4/M4/Z4" — не просто "N/N/N", вероятно
# человекочитаемый ярлык самого сохранённого шаблона HotUtils, а не вычисляемое из данных
# значение; жёстко утверждать нельзя. У цифры фазы В zoneId — своя отдельная оговорка про
# перенос планеты с этапа на этап, см. комментарий у _PLANET_CONFLICT_CODE выше.
def _build_round_platoon_assignments(
    round_num: int, round_entries: list[dict], assignments: dict,
) -> tuple[list[dict], list[str]]:
    """round_entries — записи _fetch_plan_planets для одного этапа. Возвращает
    (platoonAssignments-список, имена пропущенных планет) — планета пропускается, только
    если её название вообще не распознано (raw-заметка officer-анонса, не нашлось в
    _PLANET_NAME_TO_RU)."""
    unit_names = {
        name
        for e in round_entries
        if e["planet"]
        for operation in range(1, 7)
        for name in (tb_platoon_data.ROTE_PLATOON_SUGGESTIONS.get((e["planet"], operation)) or [])
    }
    name_to_base_id = database.resolve_unit_display_names(list(unit_names)) if unit_names else {}

    result = []
    skipped = []
    for e in round_entries:
        planet = e["planet"]
        conflict_code = _PLANET_CONFLICT_CODE.get(planet) if planet else None
        if not conflict_code:
            skipped.append(planet or e["raw"])
            continue
        bonus_suffix = "_bonus" if planet in _BONUS_PLANETS else ""
        zone_id = f"tb3_mixed_phase{round_num:02d}_conflict{conflict_code}{bonus_suffix}_recon01"
        for operation in range(1, 7):
            unit_list = tb_platoon_data.ROTE_PLATOON_SUGGESTIONS.get((planet, operation)) or []
            for slot_index, unit_name in enumerate(unit_list):
                # round-aware: донат, отложенный автозаполнением на более поздний этап
                # многоэтапной планеты, не попадает в файл ЭТОГО этапа — он ещё "не сделан
                # по времени" (см. tb_platoon_engine.visible_assignment).
                assignment = tb_platoon_engine.visible_assignment(
                    assignments.get((planet, operation, slot_index)), round_num,
                )
                if not assignment:
                    continue
                base_id = name_to_base_id.get(unit_name)
                if not base_id:
                    continue
                result.append({
                    "allyCode": assignment["ally_code"],
                    "unitBaseId": base_id,
                    "zoneId": zone_id,
                    "platoonDefinitionId": f"tb3-platoon-{operation}",
                })
    return result, skipped


def _echobase_export_bytes(round_num: int, platoon_assignments: list[dict]) -> tuple[bytes, str]:
    """Сериализует один этап в JSON того же вида, что и живой пример HotUtils/EchoBase
    (минифицированный, без пробелов — как в самом примере) + имя файла с тем же паттерном
    (echobase-assignments-ROTE-P{r}_{r}_{r}-{timestamp с ':' -> '_'}.json)."""
    now = datetime.utcnow()
    timestamp = now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"
    payload = {
        "phase": f"{round_num}/{round_num}/{round_num}",
        "timestamp": timestamp,
        "platoonAssignments": platoon_assignments,
    }
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    filename = f"echobase-assignments-ROTE-P{round_num}_{round_num}_{round_num}-{timestamp.replace(':', '_')}.json"
    return body, filename


def _attachment_headers(filename: str) -> dict:
    ascii_fallback = filename.encode("ascii", "ignore").decode("ascii") or "download"
    return {"Content-Disposition": f"attachment; filename=\"{ascii_fallback}\"; filename*=UTF-8''{quote(filename)}"}


@router.get("/tb/platoons/export")
async def tb_platoons_export(request: Request, user: dict = Depends(feature_flags.require_feature("tb_plan_order"))):
    guild_id = user["guild_id"]
    plan, error = _resolve_viewed_plan(guild_id, request.query_params.get("plan_id"))
    if plan is None:
        raise HTTPException(404, error or "План не найден.")
    try:
        round_num = int(request.query_params.get("round", ""))
    except ValueError:
        raise HTTPException(400, "Некорректный номер этапа.")

    entries, fetch_error = await _fetch_plan_planets(plan)
    if fetch_error:
        raise HTTPException(400, fetch_error)
    round_entries = [e for e in entries if e["round"] == round_num]
    if not round_entries:
        raise HTTPException(404, f"На этапе {round_num} нет планет.")

    assignments = database.get_tb_platoon_assignments(guild_id, plan["id"])
    platoon_assignments, _skipped = _build_round_platoon_assignments(round_num, round_entries, assignments)
    body, filename = _echobase_export_bytes(round_num, platoon_assignments)
    return Response(content=body, media_type="application/json", headers=_attachment_headers(filename))


@router.get("/tb/platoons/export/all")
async def tb_platoons_export_all(request: Request, user: dict = Depends(feature_flags.require_feature("tb_plan_order"))):
    guild_id = user["guild_id"]
    plan, error = _resolve_viewed_plan(guild_id, request.query_params.get("plan_id"))
    if plan is None:
        raise HTTPException(404, error or "План не найден.")

    entries, fetch_error = await _fetch_plan_planets(plan)
    if fetch_error:
        raise HTTPException(400, fetch_error)

    by_round: dict[int, list[dict]] = {}
    seen = set()
    for e in entries:
        key = (e["round"], e["planet"] or f"raw:{e['raw']}")
        if key in seen:
            continue
        seen.add(key)
        by_round.setdefault(e["round"], []).append(e)
    if not by_round:
        raise HTTPException(404, "В плане не нашлось ни одного этапа.")

    assignments = database.get_tb_platoon_assignments(guild_id, plan["id"])

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        for round_num in sorted(by_round):
            platoon_assignments, _skipped = _build_round_platoon_assignments(round_num, by_round[round_num], assignments)
            body, filename = _echobase_export_bytes(round_num, platoon_assignments)
            zf.writestr(filename, body)

    safe_plan_name = re.sub(r'[\\/:*?"<>|]+', "_", plan["name"]).strip() or "plan"
    zip_name = f"echobase-assignments-ROTE-{safe_plan_name}.zip"
    return Response(content=buffer.getvalue(), media_type="application/zip", headers=_attachment_headers(zip_name))


@router.get("/tb/platoons/notify", response_class=HTMLResponse)
async def tb_platoons_notify_page(request: Request, user: dict = Depends(feature_flags.require_feature("tb_plan_order"))):
    """Страница выбора получателей перед рассылкой взводов в личку — по прямому запросу
    пользователя 2026-09-14 ("окно, где можно указать, кому отправить или кому НЕ
    отправлять", например при повторной отправке не всем, кто уже задонатил). Кто уже
    реально задонатил в игре бот не знает (Comlink это не отдаёт) — исключение получателей
    при повторной отправке офицер делает вручную, просто снимая галочки."""
    guild_id = user["guild_id"]
    plan, error = _resolve_viewed_plan(guild_id, request.query_params.get("plan_id"))
    try:
        round_num = int(request.query_params.get("round", ""))
    except ValueError:
        round_num = None
    if plan is not None and round_num is None:
        error = "Не выбран этап — вернитесь в конструктор взводов и выберите этап."

    rows = await tb_platoon_notify.build_player_rows(guild_id, plan, round_num) if plan and round_num else []
    return templates.TemplateResponse(request, "tb_platoon_notify.html", {
        "user": user, "plan": plan, "round_num": round_num, "rows": rows, "error": error, "report": None,
    })


@router.post("/tb/platoons/notify", response_class=HTMLResponse)
async def tb_platoons_notify_send(
    request: Request,
    plan_id: int = Form(...),
    round_num: int = Form(...),
    ally_codes: list[str] = Form([]),
    user: dict = Depends(feature_flags.require_feature("tb_plan_order")),
):
    guild_id = user["guild_id"]
    plan = database.get_tb_saved_plan(plan_id)
    if not plan or plan["guild_id"] != guild_id:
        return templates.TemplateResponse(request, "tb_platoon_notify.html", {
            "user": user, "plan": None, "round_num": round_num, "rows": [], "error": "План не найден.", "report": None,
        })

    token = os.environ.get("DISCORD_TOKEN")
    if not token:
        return templates.TemplateResponse(request, "tb_platoon_notify.html", {
            "user": user, "plan": plan, "round_num": round_num, "rows": [],
            "error": "DISCORD_TOKEN не настроен на сервере — рассылка недоступна.", "report": None,
        })

    report = await tb_platoon_notify.send_broadcast(token, guild_id, plan, round_num, set(ally_codes))
    return templates.TemplateResponse(request, "tb_platoon_notify.html", {
        "user": user, "plan": plan, "round_num": round_num, "rows": [], "error": None, "report": report,
    })


# Пагинация "1 страница = 1 календарный день" (а не фиксированное число строк) — так
# страница 1 всегда за сегодня, страница 2 — за вчера и т.д., как просил пользователь.
# ACTIVITY_DAY_ROW_LIMIT — просто защитный потолок на случай аномально активного дня
# (массовый первый синк и т.п.), а не обычный постраничный лимит.
ACTIVITY_DAY_ROW_LIMIT = 2000
ACTIVITY_PERIOD_DAYS = {"7": 7, "30": 30, "90": 90}  # пресеты вместо ручного выбора дат — см. обсуждение в гайд-канале 2026-08-25

# Пагинация по дням (см. ACTIVITY_DAY_ROW_LIMIT выше) хороша для ленты "вся гильдия сразу",
# но внутри одного игрока часто есть дни с 1-2 событиями и дни с полусотней — постранично
# по дате получались то пустые, то гигантские страницы. Поэтому при фильтре по игроку
# переключаемся на обычную постраничную пагинацию по числу событий с выбором размера страницы.
ACTIVITY_PAGE_SIZES = (10, 50, 100)
ACTIVITY_DEFAULT_PAGE_SIZE = 50


def _period_to_date_from(period: str | None) -> str | None:
    days = ACTIVITY_PERIOD_DAYS.get(period or "")
    if not days:
        return None
    return (datetime.now(MSK).date() - timedelta(days=days)).isoformat()


def _iso_date_or_none(value: str | None) -> str | None:
    try:
        return datetime.strptime((value or "").strip()[:10], "%Y-%m-%d").date().isoformat()
    except ValueError:
        return None


def _custom_period_label(date_from: str | None, date_to: str | None) -> str | None:
    if not (date_from or date_to):
        return None
    fmt = lambda d: datetime.strptime(d, "%Y-%m-%d").strftime("%d.%m.%Y")
    if date_from and date_to:
        return f"{fmt(date_from)} — {fmt(date_to)}"
    return f"с {fmt(date_from)}" if date_from else f"по {fmt(date_to)}"


@router.get("/activity", response_class=HTMLResponse)
async def activity(request: Request, user: dict = Depends(require_guild_access)):
    guild_id = user["guild_id"]
    player_filter = request.query_params.get("player") or None
    action_type_filter = request.query_params.get("action_type") or None
    period = request.query_params.get("period") or ""
    if period not in ACTIVITY_PERIOD_DAYS:
        period = ""
    date_from = _period_to_date_from(period)
    date_to = None
    # Произвольный период ?from=YYYY-MM-DD&to=YYYY-MM-DD (ссылка «что качал» из проваленной
    # задачи на /tasks) — перекрывает пресет period.
    custom_from = _iso_date_or_none(request.query_params.get("from"))
    custom_to = _iso_date_or_none(request.query_params.get("to"))
    if custom_from or custom_to:
        period = ""
        date_from, date_to = custom_from, custom_to
    # «Общим списком» (?view=list) — без группировки по игрокам, у каждой строки своё имя.
    flat_view = request.query_params.get("view") == "list"
    # Выбывшие из гильдии игроки архивируются автоматически (sync_guild_roster) и живут
    # на отдельной вкладке «Выбывшие» (?departed=1) — там ТОЛЬКО они, в основной ленте их нет.
    show_departed = request.query_params.get("departed") == "1"
    archived_scope = "only" if show_departed else False

    # "Событий по фильтру/всего" на плашке наверху — суммарно по всему фильтру, не по одному
    # дню, иначе цифра скакала бы при перелистывании страниц.
    total_count = dashboard_data.get_guild_activity_count(
        guild_id, ally_code=player_filter, action_type=action_type_filter, date_from=date_from,
        date_to=date_to, include_archived=archived_scope,
    )

    page_date_label = None
    page_size = None

    if player_filter:
        # Внутри одного игрока — обычная постраничная пагинация по числу событий
        # (см. ACTIVITY_PAGE_SIZES выше), а не по календарным дням.
        try:
            page_size = int(request.query_params.get("page_size", ACTIVITY_DEFAULT_PAGE_SIZE))
        except ValueError:
            page_size = ACTIVITY_DEFAULT_PAGE_SIZE
        if page_size not in ACTIVITY_PAGE_SIZES:
            page_size = ACTIVITY_DEFAULT_PAGE_SIZE
        total_pages = max(1, -(-total_count // page_size))  # ceil div
        try:
            page = int(request.query_params.get("page", "1"))
        except ValueError:
            page = 1
        page = min(max(page, 1), total_pages)
        rows = dashboard_data.get_guild_activity(
            guild_id, ally_code=player_filter, action_type=action_type_filter,
            limit=page_size, offset=(page - 1) * page_size, date_from=date_from,
            date_to=date_to, include_archived=archived_scope,
        )
    else:
        # Список дат, за которые вообще есть события по фильтру (без учёта страницы) — сама
        # пагинация идёт по этому списку, а не по offset/limit строк: страница N
        # показывает все события за N-й по свежести день, а не N-ю полусотню строк.
        activity_dates = dashboard_data.get_guild_activity_dates(
            guild_id, ally_code=player_filter, action_type=action_type_filter, date_from=date_from,
            date_to=date_to, include_archived=archived_scope,
        )
        total_pages = max(1, len(activity_dates))
        try:
            page = int(request.query_params.get("page", "1"))
        except ValueError:
            page = 1
        page = min(max(page, 1), total_pages)
        selected_date = activity_dates[page - 1] if activity_dates else None

        rows = dashboard_data.get_guild_activity(
            guild_id, ally_code=player_filter, action_type=action_type_filter,
            limit=ACTIVITY_DAY_ROW_LIMIT, date_from=selected_date, date_to=selected_date,
            include_archived=archived_scope,
        ) if selected_date else []
        page_date_label = dashboard_data.friendly_activity_date_label(selected_date)

    players = dashboard_data.get_guild_activity_players(guild_id, include_archived=archived_scope)
    grouped = dashboard_data.group_activity(rows)
    sync_status = dashboard_data.get_activity_sync_status(guild_id)
    sync_status_text = _sync_status_text(sync_status)

    # Панель "по типу изменения" — по всему фильтру (игрок/период), не по одной странице,
    # иначе бары скакали бы при перелистывании и не отражали реальную картину.
    breakdown_rows = dashboard_data.get_guild_activity_breakdown(
        guild_id, ally_code=player_filter, date_from=date_from, date_to=date_to,
        include_archived=archived_scope,
    )
    max_breakdown = breakdown_rows[0][1] if breakdown_rows else 0

    filter_params = {k: v for k, v in {"player": player_filter, "action_type": action_type_filter, "period": period,
                                       "from": custom_from, "to": custom_to}.items() if v}
    if show_departed:
        filter_params["departed"] = "1"
    base_params = dict(filter_params)
    if flat_view:
        base_params["view"] = "list"
    if player_filter and page_size != ACTIVITY_DEFAULT_PAGE_SIZE:
        base_params["page_size"] = page_size

    page_size_urls = None
    if player_filter:
        page_size_urls = [
            (size, f"/activity?{urlencode({**filter_params, **({'view': 'list'} if flat_view else {}), 'page_size': size})}")
            for size in ACTIVITY_PAGE_SIZES
        ]

    return templates.TemplateResponse(request, "activity.html", {
        "user": user,
        "rows": rows,
        "grouped": grouped,
        "players": players,
        "player_filter": player_filter,
        "action_type_filter": action_type_filter,
        "action_types": dashboard_data.ACTIVITY_ACTION_LABELS.items(),
        "period": period,
        "custom_from": custom_from,
        "custom_to": custom_to,
        "custom_period_label": _custom_period_label(custom_from, custom_to),
        "flat_view": flat_view,
        "grouped_url": f"/activity?{urlencode({k: v for k, v in base_params.items() if k != 'view'})}",
        "flat_url": f"/activity?{urlencode({**base_params, 'view': 'list'})}",
        "show_departed": show_departed,
        "departed_count": len(database.get_guild_activity_player_codes(guild_id, include_archived="only")),
        "breakdown_rows": breakdown_rows,
        "max_breakdown": max_breakdown,
        "sync_status": sync_status_text,
        "synced_now": request.query_params.get("synced"),
        "total_count": total_count,
        "page": page,
        "total_pages": total_pages,
        "page_date_label": page_date_label,
        "page_size": page_size,
        "page_size_urls": page_size_urls,
        "prev_page_url": f"/activity?{urlencode({**base_params, 'page': page - 1})}" if page > 1 else None,
        "next_page_url": f"/activity?{urlencode({**base_params, 'page': page + 1})}" if page < total_pages else None,
        "reset_url": f"/activity?{urlencode({**({'departed': '1'} if show_departed else {}), **({'view': 'list'} if flat_view else {})})}",
    })


@router.post("/activity/sync", response_class=HTMLResponse)
async def activity_sync(
    request: Request,
    player: str = Form(""),
    action_type: str = Form(""),
    period: str = Form(""),
    user: dict = Depends(require_guild_access),
):
    # Ограниченная параллельность (semaphore) + таймаут внутри sync_player — тот же паттерн,
    # что и в web/routes/datacrons.py::_build_guild_report, чтобы HTTP-запрос от кнопки
    # "Обновить сейчас" не висел последовательно по 15с на каждого из ~50 игроков гильдии.
    guild_id = user["guild_id"]
    ally_codes = [code for _, code, _ in database.get_all_user_mappings(guild_id)]
    comlink = _get_comlink()
    today = datetime.now(MSK).date().isoformat()
    semaphore = asyncio.Semaphore(6)
    skill_tier_map = database.get_all_skill_tier_thresholds()

    async def sync_one(ally_code):
        async with semaphore:
            try:
                # guild_ids не {guild_id} — игрок теоретически состоит сразу в нескольких
                # зарегистрированных гильдиях; player_unit_cache общий на всех, и если
                # обновить его тут только под текущую гильдию, автоцикл бота позже не
                # найдёт разницы и "чужая" гильдия потеряет эти события безвозвратно.
                guild_ids = database.get_guild_ids_for_ally_code(ally_code) or {guild_id}
                # Объявления в Discord из omicron_hits тут не постим — у веб-процесса нет
                # Discord-клиента, см. services/activity_diff.py::sync_player.
                _, added, _ = await activity_diff.sync_player(comlink, ally_code, guild_ids, today, skill_tier_map)
                return added
            except Exception as e:
                print(f"⚠️ [/activity/sync] Не удалось обновить ростер {ally_code}: {e}")
                return 0

    results = await asyncio.gather(*(sync_one(ac) for ac in ally_codes))
    params = {"synced": sum(results)}
    if period in ACTIVITY_PERIOD_DAYS:
        params["period"] = period
    if action_type:
        params["action_type"] = action_type
    if player:
        params["player"] = player
    return RedirectResponse(f"/activity?{urlencode(params)}", status_code=303)


@router.get("/activity/players", response_class=HTMLResponse)
async def activity_players(request: Request, user: dict = Depends(require_guild_access)):
    guild_id = user["guild_id"]
    period = request.query_params.get("period") or ""
    if period not in ACTIVITY_PERIOD_DAYS:
        period = ""
    date_from = _period_to_date_from(period)

    rows = dashboard_data.get_guild_activity_player_stats(guild_id, date_from=date_from)
    period_params = {"period": period} if period else {}
    for row in rows:
        row["player_url"] = f"/activity?{urlencode({'player': row['ally_code'], **period_params})}"
        row["type_urls"] = {
            action_type: f"/activity?{urlencode({'player': row['ally_code'], 'action_type': action_type, **period_params})}"
            for action_type in row["counts"]
        }

    return templates.TemplateResponse(request, "activity_players.html", {
        "user": user,
        "rows": rows,
        "period": period,
        "action_types": list(dashboard_data.ACTIVITY_ACTION_LABELS.items()),
    })


@router.get("/activity/resources", response_class=HTMLResponse)
async def activity_resources(request: Request, user: dict = Depends(require_guild_access)):
    """Детали снаряжения + материалы реликвии, потраченные за период — веб-паритет
    /ресурсы (см. services/resource_spend.py), запрошен пользователем 2026-09-21 сразу
    после деплоя Discord-команды. period — "week"/"month"/"3months", те же ключи и
    семантика (месяц/3 месяца — по календарю, с 1 числа), что и в Discord-команде.

    По запросу пользователя (тот же день): клик по игроку открывает попап с ПОЛНЫМ
    списком деталей/сигналов/крафт-материалов и их количеством (без усечения, тот же
    принцип, что и в /ресурсы после правки лимита топ-N). Имена резолвятся ОДНИМ
    bulk-запросом на все id разом (по всей гильдии), а не по разу на игрока — тот же
    паттерн, что stats_check.html/get_game_unit_names для попапа "проблемных" игроков."""
    guild_id = user["guild_id"]
    period = request.query_params.get("period") or "month"
    if period not in resource_spend.PERIOD_LABELS:
        period = "month"

    rows = resource_spend.build_guild_report(guild_id, period)
    period_urls = {
        key: f"/activity/resources?{urlencode({'period': key})}"
        for key in resource_spend.PERIOD_LABELS
    }

    all_ids = set()
    for row in rows:
        all_ids.update(row["gear"]["by_equipment"].keys())
        all_ids.update(row["relic"]["signals"]["by_material"].keys())
        all_ids.update(row["relic"]["craft"]["by_material"].keys())
    names = database.get_game_equipment_names(list(all_ids))

    def _named_sorted(by_id: dict) -> list:
        return [
            {"id": item_id, "name": names.get(item_id, item_id), "qty": qty}
            for item_id, qty in sorted(by_id.items(), key=lambda kv: kv[1], reverse=True)
        ]

    table_rows = []
    players_detail = []
    for i, row in enumerate(rows):
        gear, relic = row["gear"], row["relic"]
        signals, craft = relic["signals"], relic["craft"]
        table_rows.append({
            "player_index": i,
            "name": row["name"],
            "gear_total": gear["total"],
            "gear_upgrades": gear["upgrades"],
            "signals_total": signals["total"],
            "craft_total": craft["total"],
            "relic_upgrades": relic["upgrades"],
        })
        players_detail.append({
            "name": row["name"],
            "gear": _named_sorted(gear["by_equipment"]),
            "signals": _named_sorted(signals["by_material"]),
            "craft": _named_sorted(craft["by_material"]),
        })

    return templates.TemplateResponse(request, "activity_resources.html", {
        "user": user,
        "rows": table_rows,
        "players_detail": players_detail,
        "period": period,
        "period_labels": resource_spend.PERIOD_LABELS,
        "period_urls": period_urls,
    })


@router.get("/violations", response_class=HTMLResponse)
async def violations(request: Request, user: dict = Depends(feature_flags.require_feature("violations"))):
    show_all = request.query_params.get("all") == "1"
    show_departed = request.query_params.get("departed") == "1"
    # Вкладка «Выбывшие» — только они и все целиком (нарушения за 90 дней у давно ушедших
    # обычно нулевые, фильтр show_all там не имеет смысла).
    rows = dashboard_data.get_violations_overview(user["guild_id"], include_zero=show_all or show_departed,
                                                  include_departed="only" if show_departed else False)
    top_offenders = [r for r in rows if r.recent_total > 0 and not r.departed][:8]
    max_recent = top_offenders[0].recent_total if top_offenders else 0
    return templates.TemplateResponse(request, "violations.html", {
        "user": user,
        "rows": rows,
        "show_all": show_all,
        "show_departed": show_departed,
        "departed_count": len(rows) if show_departed else len(dashboard_data.get_violations_overview(
            user["guild_id"], include_zero=True, include_departed="only")),
        "n_limit": dashboard_data.N_LIMIT,
        "top_offenders": top_offenders,
        "max_recent": max_recent,
        "warns_structure": WARNS_STRUCTURE,
        "error": request.query_params.get("error"),
    })


@router.get("/violations/api/players", response_class=JSONResponse)
async def violations_players_search(q: str = "", user: dict = Depends(feature_flags.require_feature("violations"))):
    if not q or len(q.strip()) < 2:
        return []
    q_lower = q.strip().lower()
    mappings = database.get_all_user_mappings(user["guild_id"])
    matches = [
        {"ally_code": ally_code, "name": name}
        for _discord_id, ally_code, name in mappings
        if name and q_lower in name.lower()
    ]
    matches.sort(key=lambda m: m["name"].lower())
    return matches[:20]


@router.post("/violations/add", response_class=HTMLResponse)
async def violation_add(
    ally_code: str = Form(...),
    category: str = Form(...),
    violation_1: str = Form(""),
    comment_1: str = Form(""),
    violation_2: str = Form(""),
    comment_2: str = Form(""),
    violation_3: str = Form(""),
    comment_3: str = Form(""),
    date: str = Form(""),
    user: dict = Depends(feature_flags.require_feature("violations")),
):
    guild_id = user["guild_id"]
    ally_code = ally_code.strip()

    if category not in WARNS_STRUCTURE:
        return RedirectResponse(f"/violations?{urlencode({'error': 'Некорректный режим (ТБ/ВГ/Рейд).'})}", status_code=303)
    if not ally_code:
        return RedirectResponse(f"/violations?{urlencode({'error': 'Выберите игрока из подсказок.'})}", status_code=303)

    if date:
        try:
            date_str = datetime.strptime(date, "%Y-%m-%d").strftime("%d.%m.%Y")
        except ValueError:
            return RedirectResponse(f"/violations?{urlencode({'error': 'Некорректная дата.'})}", status_code=303)
    else:
        date_str = datetime.now().strftime("%d.%m.%Y")

    # Дедуп одинаковых пунктов в одном сабмите — та же логика, что /нарушение (cogs/violations.py).
    pairs = [
        (violation_1, comment_1.strip() or None),
        (violation_2, comment_2.strip() or None),
        (violation_3, comment_3.strip() or None),
    ]
    seen = set()
    unique_pairs = []
    for v, c in pairs:
        if v and v in WARNS_STRUCTURE[category] and v not in seen:
            seen.add(v)
            unique_pairs.append((v, c))

    if not unique_pairs:
        return RedirectResponse(f"/violations?{urlencode({'error': 'Выберите хотя бы одно корректное нарушение.'})}", status_code=303)

    for v, c in unique_pairs:
        database.add_warn(ally_code, category, v, date_str, c, guild_id=guild_id)

    return RedirectResponse(f"/violations/{ally_code}", status_code=303)


async def _run_player_stats_check(guild_id: int, ally_code: str, player_label: str, plate: str, force_refresh: bool, forced_scheme: int | None = None) -> dict:
    # Тот же расчёт, что и /stats-check?action=run с уже выбранным игроком — см.
    # web/routes/stat_forecast.py::stats_check_form, здесь просто без формы выбора игрока
    # (он и так один, из карточки) и без варианта "по одному персонажу" (всегда весь плейт).
    char_keys = database.get_stat_requirement_characters(plate, guild_id=guild_id)
    try:
        comlink = _get_comlink()
        stat_calc = await stat_forecast.get_stat_calc(comlink)
    except Exception:
        return {"loading": True, "results": None, "failed_required": []}

    results = []
    failed_required = []
    for base_id in char_keys:
        outcome = await stat_forecast.evaluate_character_player(
            comlink, stat_calc, plate, base_id, ally_code, force_refresh, player_label, guild_id=guild_id,
            forced_scheme=forced_scheme,
        )
        if outcome is None:
            continue
        char_name, block, matched, total, updated_at, char_failed_required, _required_total, _active_scheme, _scheme_label = outcome
        results.append({"char_name": char_name, "block": block, "matched": matched, "total": total, "updated_at": updated_at})
        for item in char_failed_required:
            failed_required.append({"char_name": char_name, **item})
    return {"loading": False, "results": results, "failed_required": failed_required}


@router.get("/me", response_class=HTMLResponse)
async def my_profile(request: Request, user: dict = Depends(require_member_access)):
    """«Мой профиль» — карточка игрока на своём основном аккаунте (альты — переключателем
    в самой карточке). Без привязки — подсказка про /регистрация."""
    accounts = own_accounts(user)
    if not accounts:
        return templates.TemplateResponse(request, "my_profile_empty.html", {"user": user})
    return RedirectResponse(f"/player/{accounts[0][0]}", status_code=303)


@router.post("/me/alt", response_class=HTMLResponse)
async def my_profile_add_alt(ally_code: str = Form(...), user: dict = Depends(require_member_access)):
    """Привязать альт-аккаунт к себе — то же, что /регистрация альт:True в Discord."""
    from services.registration import register_player
    from swgoh_comlink import SwgohComlink
    accounts = own_accounts(user)
    back = f"/player/{accounts[0][0]}" if accounts else "/me"
    result = await register_player(SwgohComlink(url="http://localhost:3000"), str(user["discord_id"]), ally_code, is_alt=True)
    if not result.ok:
        return RedirectResponse(f"{back}?{urlencode({'alt_error': result.error})}", status_code=303)
    return RedirectResponse(f"/player/{result.ally_code}?{urlencode({'alt_added': result.ingame_name})}", status_code=303)


# ---- Личные задачи: игрок ставит задачу сам себе в «Моём профиле» (Юсин, тред «Гайд по
# АС Боту» 2026-10-16). Видны ему и офицерам (/tasks?view=personal), срок необязателен,
# в канал задач не пишутся. Проверка выполнения — тот же ежечасный аудит бота. ----

@router.get("/me/api/units", response_class=JSONResponse)
async def my_units_search(q: str = "", user: dict = Depends(require_member_access)):
    if len(q.strip()) < 2:
        return []
    return [{"base_id": base_id, "name": name} for base_id, name in database.search_game_units(q.strip(), limit=20)]


@router.get("/me/api/omicron-skills", response_class=JSONResponse)
async def my_omicron_skills(base_id: str = "", user: dict = Depends(require_member_access)):
    return tasks_routes._omicron_options(base_id) if base_id else []


@router.post("/me/tasks/add", response_class=HTMLResponse)
async def my_task_add(
    ally_code: str = Form(...),
    base_id: str = Form(""),
    target_type: str = Form(...),
    target_value: str = Form(""),
    deadline: str = Form(""),
    user: dict = Depends(require_member_access),
):
    guild_id = user["guild_id"]
    back = f"/player/{ally_code}"

    def fail(text: str):
        return RedirectResponse(f"{back}?{urlencode({'task_error': text})}#my-tasks", status_code=303)

    if ally_code not in {a[0] for a in own_accounts(user)}:
        return RedirectResponse("/me", status_code=303)
    if not feature_flags.is_enabled(guild_id, "tasks"):
        return fail("Задачи в этой гильдии выключены.")
    unit_name = database.get_game_unit_name(base_id) if base_id else None
    if not unit_name:
        return fail("Выберите юнита из подсказок.")
    err = tasks_routes._validate_target(base_id, target_type, target_value)
    if not err and deadline.strip():
        err = tasks_routes._validate_deadline(deadline)
    if err:
        return fail(err)
    if database.get_active_task_for_unit(guild_id, ally_code, base_id, target_type):
        type_label = dict(tasks_routes.TARGET_TYPE_OPTIONS)[target_type]
        return fail(f"На «{unit_name}» у вас уже есть активная задача этого типа ({type_label}).")

    target_value = target_value.strip()
    initial_value = None
    try:
        unit_data = await asyncio.to_thread(tasks_routes._fetch_unit_data, tasks_routes._get_comlink(), ally_code, base_id)
    except Exception:
        unit_data = None
    if unit_data is not None:
        thresholds = database.get_all_skill_tier_thresholds() if target_type == "omicron" else {}
        if tasks_routes._is_target_completed(unit_data, target_type, target_value, thresholds):
            return fail(f"«{unit_name}» уже соответствует этой цели.")
        initial_value = tasks_routes._current_progress_value(unit_data, target_type, target_value)

    database.add_task(ally_code, base_id, target_type, target_value, deadline.strip(), str(user["discord_id"]),
                      guild_id=guild_id, initial_value=initial_value, is_personal=True)
    return RedirectResponse(f"{back}?{urlencode({'task_notice': f'Задача «{unit_name}» поставлена.'})}#my-tasks", status_code=303)


@router.post("/me/tasks/{task_id}/delete", response_class=HTMLResponse)
async def my_task_delete(task_id: int, user: dict = Depends(require_member_access)):
    """Удалить можно только свою личную задачу (офицерские — нельзя)."""
    task = database.get_task(task_id)
    own_codes = {a[0] for a in own_accounts(user)}
    if not task or task[7] != user["guild_id"] or task[1] not in own_codes or not task[15]:
        return RedirectResponse("/me", status_code=303)
    database.delete_task(task_id)
    return RedirectResponse(f"/player/{task[1]}?{urlencode({'task_notice': 'Задача удалена.'})}#my-tasks", status_code=303)


def _forecast_units(ally_code: str) -> list:
    """[(base_id, name, is_ship), ...] из кэша ростера — для выбора юнита в виджете прогноза."""
    units = database.get_player_units(ally_code)
    names = database.get_game_unit_names(list(units))
    types = database.get_unit_types(list(units))
    rows = [(b, names.get(b) or b, types.get(b) == "ship") for b in units]
    return sorted(rows, key=lambda r: r[1].lower())


def _int_or_none(value: str | None) -> int | None:
    try:
        return int(value) if (value or "").strip() else None
    except ValueError:
        return None


@router.get("/player/{ally_code}/forecast.png")
async def player_unit_forecast(request: Request, ally_code: str, user: dict = Depends(require_member_access)):
    """Картинка /юнит_прогноз для виджета карточки игрока: юнит из кэша ростера (часовой синк),
    без живого Comlink. Доступ — как у самой карточки."""
    from services import unit_forecast, unit_forecast_image
    if user.get("tier") != "officer" and ally_code not in {a[0] for a in own_accounts(user)}:
        raise HTTPException(status_code=403)
    q = request.query_params
    base_id = (q.get("unit") or "").strip()
    stars, gear, relic = _int_or_none(q.get("stars")), _int_or_none(q.get("gear")), _int_or_none(q.get("relic"))
    units = database.get_player_units(ally_code)
    unit = units.get(base_id)
    if unit is None:
        return JSONResponse({"error": "У игрока нет этого юнита (или ростер ещё не синхронизирован)."}, status_code=404)
    is_ship = database.get_unit_types([base_id]).get(base_id) == "ship"
    if is_ship:
        gear = relic = None  # у корабля гира/релика нет — просто не учитываем
    error = unit_forecast.validate(stars, gear, relic)
    if error is None and stars is not None and not 1 <= stars <= 7:
        error = "Звёзды — от 1 до 7."
    if error:
        return JSONResponse({"error": error}, status_code=400)
    stat_calc = stat_forecast.cached_stat_calc()
    if stat_calc is None:
        stat_forecast.start_background_build(_get_comlink())
        return JSONResponse({"error": "Калькулятор статов ещё загружается — повторите через минуту."}, status_code=503)
    player_name = database.get_player_names(user["guild_id"]).get(ally_code, ally_code)
    unit_name = database.get_game_unit_name(base_id) or base_id
    age = database.get_player_units_age_seconds(ally_code)
    updated_at = (datetime.now(MSK) - timedelta(seconds=age)).strftime("%d.%m.%Y %H:%M МСК") if age is not None else None

    def work():
        if is_ship:
            crew = unit_forecast.ship_crew(stat_calc, list(units.values()), base_id)
            result = unit_forecast.forecast_ship(stat_calc, unit, crew, stars)
        else:
            result = unit_forecast.forecast(stat_calc, unit, stars, gear, relic)
        return unit_forecast_image.render(unit_name, base_id, player_name, updated_at, result)

    try:
        png = await asyncio.to_thread(work)
    except Exception as e:
        print(f"⚠️ [web/forecast] {ally_code} {base_id}: {e!r}")
        return JSONResponse({"error": "Не удалось посчитать статы этого юнита."}, status_code=500)
    return Response(png, media_type="image/png", headers={"Cache-Control": "no-store"})


@router.get("/player/{ally_code}/compare", response_class=HTMLResponse)
async def player_compare_page(request: Request, ally_code: str, user: dict = Depends(require_member_access)):
    """Сравнение игрока карточки с другими (до 8 в столбик, services/player_compare.py). Чужих
    сравнивать можно всем: те же открытые данные, что /профиль по любому коду."""
    if user.get("tier") != "officer" and ally_code not in {a[0] for a in own_accounts(user)}:
        raise HTTPException(status_code=403)
    raw = request.query_params.get("with") or ""
    codes = list(dict.fromkeys([ally_code] + player_compare.parse_codes(raw)))[:player_compare.MAX_PLAYERS]
    ctx = {"user": user, "ally_code": ally_code, "raw": raw, "players": [], "sections": [], "missed": [],
           "error": None, "max_players": player_compare.MAX_PLAYERS,
           "members": _compare_members(user["guild_id"], ally_code)}
    stat_calc = stat_forecast.cached_stat_calc()
    if len(codes) < 2:
        ctx["error"] = "Добавьте хотя бы одного игрока: код союзника или выберите из гильдии."
    elif stat_calc is None:
        stat_forecast.start_background_build(_get_comlink())
        ctx["error"] = "Калькулятор статов ещё загружается — обновите страницу через минуту."
        ctx["loading"] = True
    else:
        players, missed = await player_compare.load(_get_comlink(), codes, stat_calc)
        ctx.update(players=players, missed=missed, sections=player_compare.table(players) if len(players) > 1 else [])
        if len(players) < 2:
            ctx["error"] = "Сравнивать не с кем: остальные коды не найдены в игре."
    return templates.TemplateResponse(request, "player_compare.html", ctx)


def _compare_members(guild_id: int, ally_code: str) -> list[tuple[str, str]]:
    """[(ник, код)] состава гильдии для подсказки в поле сравнения, без самого игрока."""
    names = database.get_player_names(guild_id)
    roster = [code for code, _name in database.get_roster_by_player_id(guild_id).values() if code and code != ally_code]
    return sorted(((names.get(c, c), c) for c in roster), key=lambda x: x[0].lower())


@router.get("/player/{ally_code}/tb", response_class=HTMLResponse)
async def player_my_tb(request: Request, ally_code: str, user: dict = Depends(require_member_access)):
    """«Мой ВИ» — фрагмент карточки (взводы и бои по этапам активного ордера, services/tb_my.py).
    Отдельным запросом: ордер читается из треда в Discord, карточка его не ждёт."""
    if user.get("tier") != "officer" and ally_code not in {a[0] for a in own_accounts(user)}:
        raise HTTPException(status_code=403)
    guild_id = user["guild_id"]
    order, entries, error = await tb_my.load_order(guild_id)
    data = {"error": error} if error else await asyncio.to_thread(tb_my.build, guild_id, ally_code, order, entries)
    if not data.get("error") and not data.get("rounds"):
        data = {"error": "В активном ордере не нашлось планет по этапам."}
    return templates.TemplateResponse(request, "_player_tb.html", {"data": data})


@router.get("/player/{ally_code}/tb-advisor", response_class=HTMLResponse)
async def player_tb_advisor(request: Request, ally_code: str, user: dict = Depends(require_member_access)):
    """«Докачать для взводов» — фрагмент карточки (services/tb_advisor.py: дыры гильдии во взводах стандартной раскладки)."""
    if user.get("tier") != "officer" and ally_code not in {a[0] for a in own_accounts(user)}:
        raise HTTPException(status_code=403)
    roster = await asyncio.to_thread(database.get_player_units, ally_code)
    data = await asyncio.to_thread(tb_advisor.build, user["guild_id"], ally_code, roster) if roster else None
    return templates.TemplateResponse(request, "_player_tb_advisor.html", {"data": data})


@router.get("/player/{ally_code}/mod-grade", response_class=HTMLResponse)
async def player_mod_grade(request: Request, ally_code: str, user: dict = Depends(require_member_access)):
    """«Оценка модов» — фрагмент карточки (services/mod_grade.py, эталон по всем гильдиям бота)."""
    if user.get("tier") != "officer" and ally_code not in {a[0] for a in own_accounts(user)}:
        raise HTTPException(status_code=403)
    roster = await asyncio.to_thread(database.get_player_units, ally_code)
    data = await asyncio.to_thread(mod_grade.player_grades, roster) if roster else None
    return templates.TemplateResponse(request, "_player_mod_grade.html", {"data": data, "part_labels": {
        "primary": "основы", "sets": "наборы", "speed": "скорость", "quality": "качество"}})


@router.get("/player/{ally_code}/mod-grade/{base_id}", response_class=HTMLResponse)
async def player_mod_grade_unit(request: Request, ally_code: str, base_id: str, user: dict = Depends(require_member_access)):
    """Попап «Оценки модов»: моды персонажа по слотам и что ставят другие."""
    if user.get("tier") != "officer" and ally_code not in {a[0] for a in own_accounts(user)}:
        raise HTTPException(status_code=403)
    roster = await asyncio.to_thread(database.get_player_units, ally_code)
    data = await asyncio.to_thread(mod_grade.unit_detail, roster or {}, base_id)
    if data is None:
        raise HTTPException(status_code=404)
    return templates.TemplateResponse(request, "_player_mod_grade_unit.html", {"u": data})


@router.get("/player/{ally_code}", response_class=HTMLResponse)
async def player_card(request: Request, ally_code: str, user: dict = Depends(require_member_access)):
    guild_id = user["guild_id"]
    # Офицер видит карточку любого игрока; участник — только свои аккаунты («Мой профиль»).
    is_officer = user.get("tier") == "officer"
    accounts = own_accounts(user)
    own_codes = {a[0] for a in accounts}
    if not is_officer and ally_code not in own_codes:
        return RedirectResponse("/me", status_code=303)
    card = dashboard_data.get_player_card(guild_id, ally_code)
    if card is None:
        raise HTTPException(status_code=404, detail=f"Игрок с кодом союзника «{ally_code}» не найден в составе гильдии")

    # ---- Статы: выпадашка из уже сохранённых плейтов. Последний выбор + его результат
    # переживают рефреш и видны любому другому офицеру, открывшему эту же карточку —
    # persist через bot_state (guild_id-скоуп уже встроен в get/set_bot_state, ключ
    # дополнительно несёт ally_code, т.к. состояние своё у каждой карточки). Явная отправка
    # формы (ключ "plate" ЕСТЬ в query, даже пустой — значит выбрали "— выберите —") эту
    # память перезаписывает; открытие страницы без параметров ("голый" /player/<код>) —
    # читает её и сразу пересчитывает, чтобы показать актуальные данные под сохранённым
    # выбором, а не замороженный текст с прошлого раза.
    stats_plates = database.get_all_stat_requirement_plates(guild_id=guild_id)
    stats_state_key = f"player_card_stats_plate:{ally_code}"
    if "plate" in request.query_params:
        selected_plate = request.query_params.get("plate") or ""
        database.set_bot_state(stats_state_key, selected_plate, guild_id=guild_id)
    else:
        selected_plate = database.get_bot_state(stats_state_key, guild_id=guild_id) or ""
    force_refresh = request.query_params.get("force_refresh") == "1"
    selected_scheme = request.query_params.get("scheme") or ""
    if selected_scheme not in ("", "1", "2"):
        selected_scheme = ""
    stats_result = None
    if selected_plate and not card.departed_at:
        forced_scheme = int(selected_scheme) if selected_scheme else None
        stats_result = await _run_player_stats_check(guild_id, ally_code, card.player_name, selected_plate, force_refresh, forced_scheme=forced_scheme)

    # ---- Датакроны: тот же принцип (сохранённый сезон вместо плейта). ----
    datacron_seasons = []
    datacron_state_key = f"player_card_datacron_season:{ally_code}"
    if "season" in request.query_params:
        selected_season = request.query_params.get("season") or ""
        database.set_bot_state(datacron_state_key, selected_season, guild_id=guild_id)
    else:
        selected_season = database.get_bot_state(datacron_state_key, guild_id=guild_id) or ""
    datacron_result = None
    catalog = await _safe_datacron_catalog() if not card.departed_at else None
    if catalog:
        datacron_seasons = [
            {"set_id": sid, "display_name": data["display_name"]}
            for sid, data in sorted(catalog["seasons"].items(), key=lambda kv: -kv[0])
        ]
        if selected_season.isdigit() and int(selected_season) in catalog["seasons"]:
            set_id = int(selected_season)
            requirements = database.get_datacron_requirements_by_set(set_id, guild_id=guild_id)
            focused_requirements = database.get_datacron_focused_requirements_by_set(set_id, guild_id=guild_id)
            if requirements or focused_requirements:
                datacron_result = await _build_datacron_player_report(
                    catalog, set_id, ally_code, card.player_name, requirements, focused_requirements, guild_id,
                )

    # ---- Омикроны: чисто по кэшу (get_player_units), без Comlink — всегда считаем,
    # даже если приоритет гильдии ещё не настроен (тогда просто пустой список + подсказка). ----
    omicron_missing = omicron_priority.missing_omicrons_for_player(ally_code, guild_id) if not card.departed_at else []
    has_omicron_priority = bool(database.get_guild_omicron_priority(guild_id))

    # ---- ТБ: прогресс/просадка от ТБ к ТБ — переиспользуем гильдийский расчёт
    # (get_tb_report уже строит спарклайн/regressed на игрока), просто достаём свою строку. ----
    tb_report = dashboard_data.get_tb_report(guild_id) if is_officer else None
    tb_row = None
    if tb_report:
        name_lower = card.player_name.strip().lower()
        tb_row = next((r for r in tb_report.history if r.name.strip().lower() == name_lower), None)

    return templates.TemplateResponse(request, "player_card.html", {
        "user": user, "card": card,
        "stats_plates": stats_plates, "selected_plate": selected_plate,
        "force_refresh": force_refresh, "stats_result": stats_result,
        "selected_scheme": selected_scheme, "scheme_choices": SCHEME_LABELS.items(),
        "datacron_seasons": datacron_seasons, "selected_season": selected_season, "datacron_result": datacron_result,
        "datacron_priority_order": DATACRON_PRIORITY_ORDER,
        "datacron_priority_labels": DATACRON_PRIORITY_LABELS,
        "datacron_priority_emoji": DATACRON_PRIORITY_EMOJI,
        "omicron_missing": omicron_missing, "has_omicron_priority": has_omicron_priority,
        "tb_report_events": tb_report.events if tb_report else [],
        "tb_row": tb_row,
        "is_officer": is_officer,
        "own_accounts": accounts if ally_code in own_codes else None,
        "resources": _player_resources(guild_id, ally_code, request.query_params.get("res_period") or "month") if not card.departed_at else None,
        "alt_error": request.query_params.get("alt_error"),
        "alt_added": request.query_params.get("alt_added"),
        "can_add_personal_task": ally_code in own_codes and not card.departed_at and feature_flags.is_enabled(guild_id, "tasks"),
        "target_type_options": tasks_routes.TARGET_TYPE_OPTIONS,
        "task_error": request.query_params.get("task_error"),
        "task_notice": request.query_params.get("task_notice"),
        "forecast_units": _forecast_units(ally_code) if not card.departed_at else [],
        "compare_members": _compare_members(guild_id, ally_code) if not card.departed_at else [],
        "kpis": _card_kpis(guild_id, ally_code, card, is_officer),
        "show_my_tb": not card.departed_at and feature_flags.is_enabled(guild_id, "tb_plan_order"),
    })


def _card_kpis(guild_id: int, ally_code: str, card, is_officer: bool) -> list[dict]:
    """Цифры в шапке карточки: [{value, label, tone}] — из кэшей синка, без запросов в игру."""
    from services import player_profile
    kpis = []
    stats = (database.get_player_profiles_bulk([ally_code]).get(ally_code) or {}).get("stats") or {}
    gp = stats.get("STAT_GALACTIC_POWER_ACQUIRED_NAME")
    if gp:
        kpis.append({"value": dashboard_data._compact_number(gp), "label": "галактическая мощь"})
    units = database.get_player_units(ally_code)
    if units:
        gls = [u for b, u in units.items() if b in set(player_profile.galactic_legends())]
        ults = sum(bool(u.get("purchasedAbilityId")) for u in gls)
        kpis.append({"value": str(len(gls)), "label": f"ГЛ, с ультой {ults}" if gls else "ГЛ"})
    active = sum(t.status_badge in ("badge-task-progress", "badge-task-assigned") for t in card.current_tasks)
    overdue = sum(t.status_badge == "badge-task-overdue" for t in card.current_tasks)
    kpis.append({"value": str(active), "label": f"задач в работе, провалено {overdue}" if overdue else "задач в работе",
                 "tone": "danger" if overdue else "", "href": "#my-tasks"})
    if is_officer:
        kpis.append({"value": str(card.violations_recent_count), "label": "нарушений за 90 дн.",
                     "tone": "danger" if card.violations_recent_count >= 3 else "", "href": card.violations_url})
    last, _joined = database.get_member_activity(guild_id).get(ally_code) or (None, None)
    if last and not card.departed_at:
        days = int((time.time() - last) // 86400)
        kpis.append({"value": "сегодня" if days == 0 else f"{days} дн.", "label": "последний вход",
                     "tone": "danger" if days >= 3 else ("good" if days == 0 else "")})
    return kpis


def _player_resources(guild_id: int, ally_code: str, period: str) -> dict:
    """Потрачено деталей/сигналов/крафта за период — для карточки (бывшая /ресурсы)."""
    if period not in resource_spend.PERIOD_LABELS:
        period = "month"
    date_from = resource_spend.period_date_from(period)
    gear = resource_spend.gear_pieces_spent(guild_id, ally_code, date_from)
    relic = resource_spend.relic_materials_spent(guild_id, ally_code, date_from)
    ids = set(gear["by_equipment"]) | set(relic["signals"]["by_material"]) | set(relic["craft"]["by_material"])
    names = database.get_game_equipment_names(list(ids))

    def items(by_id: dict) -> list:
        return [{"id": i, "name": names.get(i, i), "qty": q}
                for i, q in sorted(by_id.items(), key=lambda kv: kv[1], reverse=True)]

    return {
        "period": period, "period_labels": resource_spend.PERIOD_LABELS,
        "gear_total": gear["total"], "gear_upgrades": gear["upgrades"],
        "signals_total": relic["signals"]["total"], "craft_total": relic["craft"]["total"],
        "relic_upgrades": relic["upgrades"],
        "groups": [(title, lst) for title, lst in (
            ("🔧 Детали снаряжения", items(gear["by_equipment"])),
            ("📡 Сигналы", items(relic["signals"]["by_material"])),
            ("♻️ Крафт-материалы", items(relic["craft"]["by_material"])),
        ) if lst],
    }


@router.get("/violations/{ally_code}", response_class=HTMLResponse)
async def violation_dossier(request: Request, ally_code: str, user: dict = Depends(feature_flags.require_feature("violations"))):
    guild_id = user["guild_id"]
    player_name = database.get_player_names(guild_id).get(ally_code, ally_code)
    departed = database.get_departed_players(guild_id).get(ally_code)

    rows = database.get_player_warns(ally_code, guild_id=guild_id)
    three_months_ago = datetime.now() - timedelta(days=90)
    entries = []
    recent_count = 0
    for warn_id, category, subcategory, date_str, comment in rows:
        is_recent = False
        try:
            is_recent = datetime.strptime(date_str, "%d.%m.%Y") >= three_months_ago
        except ValueError:
            pass
        if is_recent:
            recent_count += 1
        entries.append({
            "id": warn_id, "category": category, "subcategory": subcategory,
            "date_str": date_str, "comment": comment, "recent": is_recent,
        })

    return templates.TemplateResponse(request, "violation_dossier.html", {
        "user": user,
        "ally_code": ally_code,
        "player_name": player_name,
        "departed_at": dashboard_data._format_utc_date(departed[1]) if departed else None,
        "entries": entries,
        "recent_count": recent_count,
        "lifetime_count": len(entries),
        "n_limit": dashboard_data.N_LIMIT,
        "error": request.query_params.get("error"),
    })


@router.post("/violations/{warn_id}/delete", response_class=HTMLResponse)
async def violation_delete(
    warn_id: int,
    ally_code: str = Form(...),
    user: dict = Depends(feature_flags.require_feature("violations")),
):
    database.remove_warn_by_id(warn_id, guild_id=user["guild_id"])
    return RedirectResponse(f"/violations/{ally_code}", status_code=303)


# Поля сгруппированы по режиму бота, который их использует — те же группы и подписи,
# что и разделы панели /настройки в Discord (cogs/guild_settings.py::SETTINGS_SECTIONS).
# ТБ раньше была одним блоком на 6 разнородных полей (широкий, плохо читался и на
# узких колонках переносился) — разбита на тег/план+ордер/отчёт вслед за тем, как уже
# называются сабкоманды (тб_ротация_*/тб_план_*/тб_ордер_*/тб_отчет_*), с отдельной
# короткой подсказкой под каждый смысловой блок вместо одного длинного абзаца.
# (поле в guilds, человекочитаемое название, "channel"|"role")
GUILD_SETTINGS_GROUPS = [
    {
        "name": "ТБ — тег на подготовку",
        "tab": "ТБ",
        "hint": "Раз в две недели, в начале «тегаемой» недели цикла ТБ, бот пишет в этот канал и тегает эту "
                "роль — напоминание проверить взвод и заказ склада перед стартом Территориальной битвы. "
                "Чётность самой недели общая для всех гильдий бота (задаёт супер-админ). Ниже — своё для "
                "каждой гильдии расписание: время и текст тега, полностью на ваше усмотрение — можно "
                "оставить пустым, если напоминания не нужны вообще, добавить свои строки или удалить "
                "любую (дни недели одинаковы для всех гильдий — это общий календарь ТБ, задаёт супер-админ). "
                "Автопубликация самого ордера ТБ (заполняется отдельно ниже, «ТБ — план и ордер») от этого "
                "расписания не зависит — работает независимо, даже если тег-напоминания выключены. Общий "
                "переключатель ниже отключает этот блок целиком, без удаления самих настроек.",
        "fields": [
            ("tb_ping", "Тег-напоминания включены", "flag"),
            ("ping_channel_id", "Канал для тега на ротацию/взводы", "channel"),
            ("ping_role_id", "Тегаемая роль", "role"),
            ("ping_schedule_json", "Расписание тега (время/текст)", "schedule"),
        ],
    },
    {
        "name": "ТБ — план и ордер",
        "tab": "ТБ",
        "hint": "Канал, куда бот публикует план ТБ (планеты по этапам) и автоматические ордера на каждый этап. "
                "Ордера бот собирает из канала/ветки со стратегией и тегает в них указанную роль. Общий "
                "переключатель ниже отключает автопубликацию целиком, без удаления самих настроек канала/роли.",
        "fields": [
            ("tb_plan_order", "Автопубликация плана/ордера включена", "flag"),
            ("tb_plan_channel_id", "Канал анонсов плана ТБ и автоордеров", "channel"),
            ("tb_order_source_channel_id", "Канал/ветка-источник стратегии по этапам", "channel"),
            ("tb_order_role_id", "Роль, тегаемая в автоордере", "role"),
        ],
    },
    {
        "name": "ТБ — итоговый отчёт",
        "tab": "ТБ",
        "hint": "Канал, куда бот публикует автоотчёт по итогам завершённой ТБ и уведомления офицерам. "
                "«Кто что не сделал» — вместе с отчётом бот пишет, кто по дням не заливал или залил не "
                "полностью, не ходил в бои, мог пройти ОЗ, но не пошёл, и не поставил ни одного юнита во "
                "взводы. В нарушения это не записывается. По умолчанию включено.",
        "fields": [
            ("officer_channel_id", "Канал для автоотчёта и уведомлений", "channel"),
            ("tb_misses_report", "«Кто что не сделал» в отчёте", "flag"),
        ],
    },
    {
        "name": "День рождения",
        "tab": "ДР",
        "hint": "Канал, куда бот пишет поздравление, и роль, которую выдаёт имениннику в день рождения.",
        "fields": [
            ("birthday_channel_id", "Канал для поздравлений с ДР", "channel"),
            ("birthday_role_id", "Роль, выдаваемая в ДР", "role"),
        ],
    },
    {
        "name": "Омикроны",
        "tab": "Омикроны",
        "hint": "Канал, куда бот пишет автообъявления о выдаче омикронов.",
        "fields": [
            ("omicron_channel_id", "Канал для автообъявлений об омикронах", "channel"),
        ],
    },
    {
        "name": "Территориальная Война (ВГ)",
        "tab": "ВГ",
        "hint": "Форум-канал с гайдами по контрам, откуда бот забирает данные для /вг_ордер. "
                "«Персональные» — свои персонажи гильдии для блока «Персональные» в /вг_соперник "
                "(как у HotBot): средние статы у владельцев в нашей гильдии и у соперника.",
        "fields": [
            ("tw_guide_forum_channel_id", "Форум-канал гайдов по контрам ВГ", "channel"),
            ("personal_units", "Персональные персонажи для /вг_соперник", "units"),
        ],
    },
    {
        "name": "Задачи — уведомления",
        "tab": "Задачи",
        "hint": "Канал, куда бот пишет о выполнении/просрочке задачи и напоминания о дедлайне, и время (МСК), "
                "в которое раз в сутки бот проверяет дедлайны и шлёт напоминания. По умолчанию 10:00. "
                "Отдельный переключатель — тегать ли игрока прямо в сообщении о постановке новой задачи "
                "(по умолчанию выключено; включите, если это нужно вашей гильдии).",
        "fields": [
            ("tasks_log_channel_id", "Канал уведомлений по задачам", "channel"),
            ("tasks_notify_time", "Время уведомлений (МСК)", "time"),
            ("tasks_mention", "Тег игрока при постановке задачи", "flag"),
        ],
    },
    {
        "name": "Энка (рейдовые билеты)",
        "tab": "Энка",
        "hint": "За 2 часа и за 30 минут до сброса дня гильдии бот тегает в канале тех, кто не сдал 600 билетов, "
                "после сброса пишет итог без тегов. Время сброса берётся из игры. История — на странице «Энка». "
                "«Личка недосдавшим» — в те же моменты бот дополнительно пишет каждому из них в личку "
                "(по умолчанию выключено).",
        "fields": [
            ("raid_tickets_channel_id", "Канал напоминаний про энку", "channel"),
            ("raid_tickets", "Напоминания и итог дня", "flag"),
            ("raid_tickets_dm", "Личка недосдавшим", "flag"),
        ],
    },
]


SETTINGS_TAB_ORDER = ["ТБ", "ВГ", "Энка", "Задачи", "ДР", "Омикроны"]


# Числовые ID типов каналов Discord (те же значения, что в REST-ответе
# GET /channels/{id} и в disnake.ChannelType) — нужны только чтобы записать
# channel_type в guild_channels в том же формате, что и cogs/channel_registry.py
# (channel.type.name), когда канал авто-регистрируется из голого ID (см.
# guild_settings_save ниже), а не через живой disnake-объект.
_DISCORD_CHANNEL_TYPE_NAMES = {
    0: "text", 2: "voice", 4: "category", 5: "news",
    10: "news_thread", 11: "public_thread", 12: "private_thread",
    13: "stage_voice", 15: "forum",
}


async def _fetch_discord_channel(channel_id: str) -> dict | None:
    """Живой GET /channels/{id} через Discord REST токеном бота — тот же паттерн,
    что уже используется в tb_plan_reader.py (веб-процесс не держит соединения с
    гейтвеем, но токен бота у него есть через .env). Используется и для показа
    имени уже сохранённого, но не зарегистрированного через /канал канала (GET
    /settings), и для авто-регистрации канала, когда его ID вписали вручную
    вместо выбора из списка (POST /settings, см. ниже)."""
    from services import demo_mode
    token = os.environ.get("DISCORD_TOKEN")
    if not token or demo_mode.active():
        return None
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                f"https://discord.com/api/v10/channels/{channel_id}",
                headers={"Authorization": f"Bot {token}"},
            )
            if resp.status_code != 200:
                return None
            return resp.json()
    except httpx.HTTPError:
        return None


async def _resolve_unregistered_channel_name(channel_id: str) -> str | None:
    data = await _fetch_discord_channel(channel_id)
    return data.get("name") if data else None


# Редактор ping_schedule_json (kind == "schedule" в GUILD_SETTINGS_GROUPS) — тот
# же формат [{"time": "ЧЧ:ММ", "text": "...", "days": [0..6]}], что уже лежит в
# колонке. "days" officer'у в веб-форме НЕ показывается и не редактируется (по
# прямому запросу пользователя 2026-09-15) — это факт общего игрового календаря
# ТБ, единый для всех гильдий (см. services/tb_schedule.py::STANDARD_ORDER_DAYS),
# при сохранении каждая строка получает его автоматически. Повторяемые строки
# рендерятся через уже существующий общий JS-паттерн [data-row-group]
# (web/static/dashboard.js).
# Дефолт-подсказка ТОЛЬКО для гильдии, ещё вообще не настраивавшей расписание —
# те же время и текст, что уже годами живьём работают у AbsoluteChaos. Это лишь
# стартовый пример: обе строки, как и любые другие, полностью кастомны — можно
# переименовать, поменять время или удалить, включая пустое расписание вообще
# (гильдии, которой не нужны тег-напоминания). Автопубликация самого ордера ТБ
# (cogs/guild_events.py::tb_order_loop) от этого расписания больше НЕ зависит —
# см. [[project_tb_schedule_dst_feature_2026-09-15]] за тем, почему их
# развязали (раньше отсутствие строки "ордер" тихо ломало автопубликацию).
DEFAULT_SCHEDULE_ENTRIES = [
    {"time": "18:50", "text": "взводы"},
    {"time": "19:50", "text": "ордер"},
]


def _parse_schedule_rows(raw_json: str) -> list[dict]:
    if not raw_json:
        return [dict(e) for e in DEFAULT_SCHEDULE_ENTRIES]
    try:
        entries = json.loads(raw_json)
    except (TypeError, json.JSONDecodeError):
        return [dict(e) for e in DEFAULT_SCHEDULE_ENTRIES]
    parsed = [
        {"time": e.get("time", ""), "text": e.get("text", "")}
        for e in entries if isinstance(e, dict)
    ]
    return parsed if parsed else [dict(e) for e in DEFAULT_SCHEDULE_ENTRIES]


UNITS_LIMIT = 20  # персонажей в блоке «Персональные»


def _unit_label(base_id: str, name: str) -> str:
    return f"{name} [{base_id}]"


def _character_options() -> list[str]:
    """Подсказки для поля-списка персонажей: «Имя [BASE_ID]» по всем персонажам игры."""
    rows = database.search_game_units("", limit=2000)
    types = database.get_unit_types([b for b, _ in rows])
    return sorted(_unit_label(b, n) for b, n in rows if types.get(b) != "ship")


def _unit_entries(value: str) -> list[str]:
    ids = [b.strip() for b in (value or "").split(",") if b.strip()]
    names = database.get_game_unit_names(ids)
    return [_unit_label(b, names.get(b, b)) for b in ids]


def _parse_unit_entries(raw_values: list[str]) -> tuple[list[str], str | None]:
    """«Имя [BASE_ID]» или точное имя → base_id; (список, первая нераспознанная строка)."""
    result = []
    for raw in raw_values:
        raw = (raw or "").strip()
        if not raw:
            continue
        m = re.search(r"\[([A-Z0-9_]+)\]\s*$", raw)
        base_id = m.group(1) if m else None
        if base_id is None:
            match = next(((b, n) for b, n in database.search_game_units(raw, limit=20) if n.lower() == raw.lower()), None)
            base_id = match[0] if match else None
        if not base_id or not database.get_game_unit_name(base_id):
            return [], raw
        if base_id not in result:
            result.append(base_id)
    return result[:UNITS_LIMIT], None


@router.get("/settings", response_class=HTMLResponse)
async def guild_settings(request: Request, user: dict = Depends(require_guild_access)):
    guild_cfg = database.get_guild_config(user["guild_id"])
    known_channels = database.get_guild_channels(user["guild_id"])
    known_channel_ids = {c["channel_id"] for c in known_channels}
    # Все каналы Discord-сервера гильдии — живым запросом к Discord, чтобы канал можно
    # было выбрать сразу, без /канал в Discord (2026-10-14). Выбранный отсюда канал
    # при сохранении регистрируется сам (см. guild_settings_save). Discord недоступен —
    # остаётся список зарегистрированных + ручной ввод ID.
    server_channels = []
    if guild_cfg and guild_cfg.get("discord_guild_id"):
        try:
            server_channels = [
                c for c in await discord_rest.list_guild_channels(guild_cfg["discord_guild_id"])
                if c["id"] not in known_channel_ids
            ]
        except Exception as e:
            print(f"⚠️ [web/settings] Не удалось получить каналы сервера: {e}")
    server_channel_ids = {c["id"] for c in server_channels}

    async def _channel_options(current_value: str):
        options = [{"id": c["channel_id"], "label": c["channel_name"]} for c in known_channels]
        options += [
            {"id": c["id"], "label": (f"{c['category']} / " if c["category"] else "") + f"#{c['name']}",
             "group": "server"}
            for c in server_channels
        ]
        # Уже сохранённое значение может ссылаться на канал, который никто ещё не
        # зарегистрировал через /канал (старые гильдии, засеянные напрямую в БД,
        # или канал переименовали/удалили) — не терять его молча, показать реальное
        # имя (через живой запрос к Discord), а не голый ID.
        if current_value and current_value not in known_channel_ids and current_value not in server_channel_ids:
            resolved_name = await _resolve_unregistered_channel_name(current_value)
            label = resolved_name or f"Канал не найден (`{current_value}`)"
            options.append({"id": current_value, "label": label})
        return options

    def _channel_error(guild_id: int, field: str, value: str) -> dict | None:
        err = channel_health.get_error(guild_id, field) if value else None
        if not err or err.get("channel_id") != str(value):
            return None
        err["since_text"] = time.strftime("%d.%m %H:%M", time.gmtime(err["since"] + 3 * 3600))
        return err

    groups = []
    for group in GUILD_SETTINGS_GROUPS:
        rows = []
        for field, label, kind in group["fields"]:
            # "flag" — не колонка guilds, а services.feature_flags (тот же bot_state,
            # что и /admin/features у супер-админа) — офицер сам включает/выключает
            # блок целиком, не удаляя сами настройки канала/роли под ним.
            value = feature_flags.is_enabled(user["guild_id"], field) if kind == "flag" else (guild_cfg.get(field) or "")
            rows.append({
                "field": field, "label": label, "kind": kind, "value": value,
                "channel_options": await _channel_options(value) if kind == "channel" else None,
                "channel_error": _channel_error(user["guild_id"], field, value) if kind == "channel" else None,
                "schedule_entries": _parse_schedule_rows(value) if kind == "schedule" else None,
                "unit_entries": _unit_entries(value) if kind == "units" else None,
            })
        groups.append({"name": group["name"], "hint": group["hint"], "rows": rows, "tab": group["tab"]})
    has_units = any(r["kind"] == "units" for g in groups for r in g["rows"])
    return templates.TemplateResponse(request, "guild_settings.html", {
        "user": user,
        "groups": groups,
        "unit_options": _character_options() if has_units else [],
        # Вкладки над формой (ТБ / ВГ / Энка / ...) — порядок как в GUILD_SETTINGS_GROUPS.
        "tabs": sorted(dict.fromkeys(g["tab"] for g in groups), key=lambda t: SETTINGS_TAB_ORDER.index(t) if t in SETTINGS_TAB_ORDER else 99),
        "error": request.query_params.get("error"),
        "saved": request.query_params.get("saved"),
    })


@router.post("/settings", response_class=HTMLResponse)
async def guild_settings_save(request: Request, user: dict = Depends(require_guild_access)):
    # request.form() напрямую (а не одноимённые Form(...)-параметры на каждое
    # поле) — поля живут в одном месте (GUILD_SETTINGS_GROUPS), не дублируются
    # в сигнатуре; заодно позволяет читать доп. "<field>_manual" для ручного
    # ввода ID канала (см. ниже и guild_settings.html), не объявляя их все тоже.
    form = await request.form()
    guild_id = user["guild_id"]
    guild_cfg_before = database.get_guild_config(guild_id) or {}
    cleaned = {}

    for group in GUILD_SETTINGS_GROUPS:
        for field, _label, kind in group["fields"]:
            if kind == "time":
                continue  # tasks_notify_time — отдельно, ниже
            if kind == "schedule":
                continue  # ping_schedule_json — отдельно, ниже
            if kind == "units":
                base_ids, bad = _parse_unit_entries(form.getlist(field))
                if bad:
                    return RedirectResponse(
                        f"/settings?{urlencode({'error': 'Персонаж не найден — выберите из подсказки: ' + bad})}", status_code=303
                    )
                cleaned[field] = ",".join(base_ids) or None
                continue
            if kind == "flag":
                # Не guilds-колонка — services.feature_flags (bot_state), не часть
                # cleaned/update_guild_config. Чекбокс не приходит в form вообще,
                # если снят — присутствие ключа и есть "включено".
                feature_flags.set_enabled(guild_id, field, field in form, updated_by=str(user["discord_id"]))
                continue

            if kind == "role":
                raw = (form.get(field) or "").strip()
                if raw and not raw.isdigit():
                    return RedirectResponse(
                        f"/settings?{urlencode({'error': f'ID должен состоять только из цифр ({field})'})}", status_code=303
                    )
                cleaned[field] = raw or None
                continue

            # kind == "channel": можно выбрать из списка (select, значение = ID
            # уже зарегистрированного через /канал канала) ИЛИ вписать ID вручную
            # в соседнее поле "<field>_manual" — тогда ручной ввод побеждает.
            selected = (form.get(field) or "").strip()
            manual = (form.get(f"{field}_manual") or "").strip()
            value = manual or selected
            if value and not value.isdigit():
                return RedirectResponse(
                    f"/settings?{urlencode({'error': f'ID канала должен состоять только из цифр ({field})'})}", status_code=303
                )
            if value:
                known_ids = {c["channel_id"] for c in database.get_guild_channels(guild_id)}
                if value not in known_ids:
                    # Канал не из списка (ID вписан вручную мимо /канал) —
                    # регистрируем его сами по данным живого запроса к Discord,
                    # как будто офицер сам выполнил /канал в этом канале: дальше
                    # он ведёт себя как обычный зарегистрированный канал и
                    # появится в выпадающем списке при следующем открытии страницы.
                    data = await _fetch_discord_channel(value)
                    if data is None:
                        return RedirectResponse(
                            f"/settings?{urlencode({'error': f'Бот не видит канал с ID {value} (не на этом сервере или нет доступа) — {field}'})}",
                            status_code=303,
                        )
                    name = data.get("name") or value
                    channel_type = _DISCORD_CHANNEL_TYPE_NAMES.get(data.get("type"))
                    database.register_guild_channel(guild_id, value, name, channel_type=channel_type, registered_by=user["discord_id"])
            cleaned[field] = value or None

    tasks_notify_time = (form.get("tasks_notify_time") or "").strip()
    if tasks_notify_time and not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", tasks_notify_time):
        return RedirectResponse(
            f"/settings?{urlencode({'error': 'Время уведомлений должно быть в формате ЧЧ:ММ (00:00–23:59)'})}",
            status_code=303,
        )
    cleaned["tasks_notify_time"] = tasks_notify_time or None

    # ping_schedule_json — повторяемые строки [data-row-group] (см. guild_settings.html),
    # одноимённые поля во всех строках -> form.getlist. Пустые строки (время и текст оба
    # пусты — пользователь добавил строку и не заполнил) пропускаются молча, любая другая
    # некорректная строка отклоняет всю форму, а не тихо теряет часть расписания. "days"
    # не приходит из формы вообще (officer его не видит/не редактирует) — каждая строка
    # получает единый tb_schedule.STANDARD_ORDER_DAYS, см. коммент у _parse_schedule_rows.
    sched_times = form.getlist("ping_schedule_json_time")
    sched_texts = form.getlist("ping_schedule_json_text")
    schedule_rows = []
    for raw_time, raw_text in zip(sched_times, sched_texts):
        raw_time, raw_text = raw_time.strip(), raw_text.strip()
        if not raw_time and not raw_text:
            continue
        if not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", raw_time):
            error_msg = f"Время тега должно быть в формате ЧЧ:ММ: «{raw_time}»"
            return RedirectResponse(f"/settings?{urlencode({'error': error_msg})}", status_code=303)
        if not raw_text:
            return RedirectResponse(
                f"/settings?{urlencode({'error': 'У каждой строки расписания тега должен быть текст'})}", status_code=303
            )
        schedule_rows.append({"time": raw_time, "text": raw_text, "days": tb_schedule.STANDARD_ORDER_DAYS})
    cleaned["ping_schedule_json"] = json.dumps(schedule_rows) if schedule_rows else None

    # Кто задал канал — ему личка, если бот потом не сможет туда писать (services/channel_health.py).
    for field, value in cleaned.items():
        if field.endswith("_channel_id") and value and value != (guild_cfg_before.get(field) or None):
            channel_health.record_setter(guild_id, field, user["discord_id"])
            database.set_bot_state(f"channel_error:{field}", "", guild_id=guild_id)  # новый канал — старая пометка не про него

    database.update_guild_config(guild_id, **cleaned)
    return RedirectResponse("/settings?saved=1", status_code=303)


# ---- Ручной доступ (офицерская версия /admin/access — только для своей гильдии) ----
# Дубликат блока "Ручные гранты доступа" с /admin/access (web/routes/admin.py),
# но guild_id жёстко из user["guild_id"] (не выбирается формой) и список отфильтрован
# по своей гильдии — см. services/guild_admin.py::add_guild_scoped_grant/
# remove_guild_scoped_grant/list_grants_for_guild за проверкой границы гильдии.

@router.get("/access", response_class=HTMLResponse)
async def guild_access(request: Request, user: dict = Depends(require_guild_access)):
    grants = [
        {**g, "granted_by_name": database.get_username_for_discord_id(g["granted_by"])}
        for g in list_grants_for_guild(user["guild_id"])
    ]
    return templates.TemplateResponse(request, "guild_access.html", {
        "user": user,
        "grants": grants,
        "error": request.query_params.get("error"),
    })


@router.post("/access/add", response_class=HTMLResponse)
async def guild_access_add(
    discord_id: str = Form(...),
    ally_code: str = Form(...),
    tier: str = Form(...),
    user: dict = Depends(require_guild_access),
):
    comlink = _get_comlink()
    result = await add_guild_scoped_grant(comlink, discord_id, ally_code, tier, user["guild_id"], user["discord_id"])
    if not result.ok:
        return RedirectResponse(f"/access?{urlencode({'error': result.error})}", status_code=303)
    return RedirectResponse("/access", status_code=303)


@router.post("/access/{discord_id}/remove", response_class=HTMLResponse)
async def guild_access_remove(discord_id: str, user: dict = Depends(require_guild_access)):
    result = remove_guild_scoped_grant(discord_id, user["guild_id"])
    if not result.ok:
        return RedirectResponse(f"/access?{urlencode({'error': result.error})}", status_code=303)
    return RedirectResponse("/access", status_code=303)
