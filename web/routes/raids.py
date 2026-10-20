"""Рейды — очки участников за сохранённые рейды (raid_results/raid_member_scores, пишет
cogs/raids.py после каждого завершённого рейда). Здесь только просмотр: график очков
гильдии по рейдам и таблица «игрок × рейд». Выбывшие — на отдельной вкладке."""

from pathlib import Path

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

import database
from services import feature_flags
from services import raids as rs

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent.parent / "templates"))
TABLE_RAIDS = 10
CHART_RAIDS = 20


@router.get("", response_class=HTMLResponse)
async def raids_page(request: Request, departed: int = 0, user: dict = Depends(feature_flags.require_feature("raids"))):
    guild_id = user["guild_id"]
    results = database.get_raid_results(guild_id, limit=CHART_RAIDS)
    table_raids = results[:TABLE_RAIDS]
    scores = database.get_raid_scores(guild_id, [r["key"] for r in table_raids])
    roster = database.get_roster_by_player_id(guild_id)  # текущий состав

    by_raid: dict[str, list] = {}
    for s in scores:
        by_raid.setdefault(s["raid_key"], []).append(s)
    avg_by_raid = {k: sum(x["score"] for x in v) / len(v) for k, v in by_raid.items() if v}
    place = {}
    for k, rows in by_raid.items():
        for i, x in enumerate(sorted(rows, key=lambda x: -x["score"]), 1):
            place[(k, x["player_id"])] = i

    players: dict[str, dict] = {}
    for s in scores:
        p = players.setdefault(s["player_id"], {"player_id": s["player_id"], "name": s["name"], "ally_code": s["ally_code"], "raids": {}})
        p["raids"][s["raid_key"]] = {
            "score": s["score"],
            "place": place.get((s["raid_key"], s["player_id"])),
            "low": s["score"] < avg_by_raid.get(s["raid_key"], 0) * 0.5,
        }
    for pid, (code, name) in roster.items():
        p = players.setdefault(pid, {"player_id": pid, "name": name, "ally_code": code, "raids": {}})
        p["name"] = name or p["name"]

    keys = [r["key"] for r in table_raids]
    for p in players.values():
        vals = [p["raids"][k]["score"] for k in keys if k in p["raids"]]
        p["avg"] = sum(vals) / len(vals) if vals else None
        p["last"] = p["raids"].get(keys[0], {}).get("score") if keys else None
        p["current"] = p["player_id"] in roster
    shown = [p for p in players.values() if p["current"] != bool(departed)]
    shown.sort(key=lambda p: (p["last"] is None, -(p["last"] or 0), p["name"].lower()))

    chart = list(reversed(results))
    top = max((r["guild_score"] for r in chart), default=0)
    latest = results[0] if results else None
    return templates.TemplateResponse(request, "raids.html", {
        "user": user,
        "raids": table_raids,
        "chart": [{"label": f"{rs.raid_name(r['raid_id'])} · {rs.end_date(r['end_time'])}",
                   "value": r["guild_score"], "pct": round(r["guild_score"] / top * 100, 1) if top else 0} for r in chart],
        "players": shown,
        "departed": bool(departed),
        "departed_count": sum(1 for p in players.values() if not p["current"]),
        "raid_name": rs.raid_name,
        "end_date": rs.end_date,
        "millions": rs.millions,
        "latest": latest,
        "latest_count": len(by_raid.get(latest["key"], [])) if latest else 0,
        "latest_avg": avg_by_raid.get(latest["key"]) if latest else None,
    })
