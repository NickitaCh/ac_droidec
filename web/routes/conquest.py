"""Калькулятор Завоевания (/conquest) — расчёт и данные в services/conquest.py. Открыт
участникам, не только офицерам: это личный инструмент игрока, отметки хранятся по
Discord-аккаунту (conquest_marks)."""

from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

import database
from services import conquest
from web.deps import require_member_access

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent.parent / "templates"))
MSK = ZoneInfo("Europe/Moscow")


def _msk(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).astimezone(MSK).strftime("%d.%m %H:%M")


@router.get("", response_class=HTMLResponse)
async def conquest_page(request: Request, diff: str = "", user: dict = Depends(require_member_access)):
    state = conquest.state()
    ctx = {"user": user, "state": state}
    if state:
        keys = [d["key"] for d in state["difficulties"]]
        if diff not in keys:
            diff = database.get_conquest_last_difficulty(user["discord_id"], state["id"]) or "hard"
        d = next(x for x in state["difficulties"] if x["key"] == diff)
        done, battles = database.get_conquest_marks(user["discord_id"], state["id"], diff)
        groups = [("Общие подвиги", [f for f in d["feats"] if f["sector"] is None])]
        for sector in sorted({f["sector"] for f in d["feats"] if f["sector"]}):
            groups.append((f"Сектор {sector}", [f for f in d["feats"] if f["sector"] == sector]))
        names = database.get_game_unit_names([i[len("unitshard_"):] for c in d["crates"] for i, _n, _q in c["items"]
                                              if i.startswith("unitshard_")])
        crates = [{**c,
                   "shards": [(names.get(i[len("unitshard_"):], n), q) for i, n, q in c["items"] if i.startswith("unitshard_")],
                   "other": [(n, q) for i, n, q in c["items"] if not i.startswith("unitshard_")]}
                  for c in d["crates"]]
        ctx.update({
            "diff": d, "crates": crates, "done": done, "battles": battles, "groups": groups,
            "result": conquest.evaluate(d, done, battles),
            "kind_labels": conquest.KIND_LABELS,
            "period": f"{_msk(state['start'])} — {_msk(state['end'])} МСК",
            "started": state["start"] <= datetime.now(timezone.utc).timestamp() * 1000,
        })
    return templates.TemplateResponse(request, "conquest.html", ctx)


@router.post("/save")
async def conquest_save(request: Request, user: dict = Depends(require_member_access)):
    state = conquest.state()
    form = await request.form()
    diff = form.get("diff", "hard")
    if not state:
        return RedirectResponse("/conquest", status_code=303)
    d = next((x for x in state["difficulties"] if x["key"] == diff), None)
    if d is None:
        return RedirectResponse("/conquest", status_code=303)
    valid = {f["id"] for f in d["feats"]}
    feats = [f for f in form.getlist("feat") if f in valid]
    try:
        battles = max(0, min(10000, int(form.get("battles") or 0)))
    except ValueError:
        battles = 0
    database.set_conquest_marks(user["discord_id"], state["id"], diff, feats, battles)
    return RedirectResponse(f"/conquest?diff={diff}", status_code=303)
