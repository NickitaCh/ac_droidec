"""Энка (рейдовые билеты) — кто сколько сдал: текущий день живьём из Comlink и история
по дням (raid_ticket_daily, пишет cogs/raid_tickets.py в итоговом отчёте). Напоминания
и итог в Discord — только бот; здесь просмотр."""

import asyncio
import time
from datetime import date
from pathlib import Path

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

import database
from services import feature_flags
from services import raid_tickets as rt
from services.config_status import config_warning_html

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent.parent / "templates"))
HISTORY_DAYS = 14


def _live(swgoh_guild_id: str):
    from swgoh_comlink import SwgohComlink
    resp = SwgohComlink(url="http://localhost:3000").get_guild(swgoh_guild_id, include_recent_guild_activity_info=True)
    return rt.parse_guild(resp)


@router.get("", response_class=HTMLResponse)
async def raid_tickets_page(request: Request, user: dict = Depends(feature_flags.require_feature("raid_tickets"))):
    guild_id = user["guild_id"]
    cfg = database.get_guild_config(guild_id) or {}
    roster = database.get_roster_by_player_id(guild_id)

    live, reset = None, None
    if cfg.get("swgoh_guild_id"):
        try:
            reset, members = await asyncio.to_thread(_live, cfg["swgoh_guild_id"])
            live = {m["player_id"]: m["current"] for m in members}
            names_live = {m["player_id"]: m["name"] for m in members}
        except Exception as e:
            print(f"⚠️ [web/tickets] Comlink недоступен: {e!r}")

    history = database.get_raid_ticket_history(guild_id, days=30)
    days = sorted({r[0] for r in history}, reverse=True)
    shown_days = days[:HISTORY_DAYS]
    by_player = {}
    for day, pid, _code, name, tickets in history:
        p = by_player.setdefault(pid, {"player_id": pid, "name": name, "days": {}, "missed": 0, "short": 0})
        p["days"][day] = tickets
        if tickets < rt.REQUIRED:
            p["missed"] += 1
            p["short"] += rt.REQUIRED - tickets

    # Текущий состав — всегда в таблице (даже без истории); выбывшие — только если есть история.
    for pid, (_code, name) in roster.items():
        by_player.setdefault(pid, {"player_id": pid, "name": name, "days": {}, "missed": 0, "short": 0})["name"] = name
    if live is not None:
        for pid, cur in live.items():
            p = by_player.setdefault(pid, {"player_id": pid, "name": names_live.get(pid, pid), "days": {}, "missed": 0, "short": 0})
            p["today"] = cur
    players = sorted(by_player.values(), key=lambda p: (-p["missed"], p.get("today", rt.REQUIRED), p["name"].lower()))

    today_debtors = sum(1 for p in players if p.get("today") is not None and p["today"] < rt.REQUIRED)
    return templates.TemplateResponse(request, "raid_tickets.html", {
        "user": user,
        "players": players,
        "days": shown_days,
        "days_total": len(days),
        "required": rt.REQUIRED,
        "live_ok": live is not None,
        "today_debtors": today_debtors,
        "reset_text": rt.reset_time_text(reset) if reset else None,
        "left_min": max(0, (reset - int(time.time())) // 60) if reset else None,
        "day_short": {d: f"{d[8:10]}.{d[5:7]}" for d in shown_days},
        "config_warning": config_warning_html(cfg, "raid_tickets"),
        "today": date.today().isoformat(),
    })
