"""Калькулятор Завоевания (/conquest) — расчёт и данные в services/conquest.py. Открыт
всем, кто вошёл через Discord, даже без подключённой гильдии (решение пользователя
2026-10-22): это личный инструмент игрока, отметки хранятся по Discord-аккаунту
(conquest_marks). Склад для «Кем пройти?» — свой зарегистрированный аккаунт или код
союзника, вписанный на странице (?acc=); если его нет в кэше, берём из игры один раз."""

from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

import database
import guild_resolver
from services import activity_diff, conquest, guild_units
from web.deps import get_current_user, own_accounts

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent.parent / "templates"))
MSK = ZoneInfo("Europe/Moscow")


def _msk(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).astimezone(MSK).strftime("%d.%m %H:%M")


WHO_SHOWN = 8  # своих подходящих персонажей в раскрывашке «Кем пройти?»


def _who(feats: list, roster: dict | None) -> dict:
    """{feat_id: {label, need, have, can, units: [(имя, уровень)], more, all_names}} для
    подвигов с подсказкой. roster None — аккаунт не привязан: показываем просто кто подходит."""
    ids = {b for f in feats for b in (f.get("who") or {}).get("units") or []}
    names = database.get_game_unit_names(list(ids))
    out = {}
    for f in feats:
        who = f.get("who") or {}
        if who.get("kind") in (None, "any") or not who.get("units"):
            continue
        row = {"kind": who["kind"], "label": who.get("label") or "", "need": who.get("need") or 1}
        if roster is None:
            row.update(have=None, can=True, units=[(names.get(b, b), "") for b in who["units"][:WHO_SHOWN]],
                       more=max(0, len(who["units"]) - WHO_SHOWN))
        else:
            owned = []
            for b in who["units"]:
                if b in roster:
                    lv = guild_units.level_of(roster[b])
                    owned.append((lv["stars"], lv["relic"] if lv["relic"] is not None else -1, lv["gear"],
                                  names.get(b, b), guild_units.level_text(lv)))
            owned.sort(reverse=True)
            row.update(have=len(owned), can=len(owned) >= row["need"],
                       units=[(o[3], o[4]) for o in owned[:WHO_SHOWN]], more=max(0, len(owned) - WHO_SHOWN))
        out[f["id"]] = row
    return out


def _accounts(user: dict) -> list:
    """[(ally_code, имя, is_main)] — в своей гильдии как в «Моём профиле», без подключённой
    гильдии — регистрации во всех гильдиях бота."""
    if user.get("tier") in ("officer", "member") and user.get("guild_id") is not None:
        return own_accounts(user)
    seen, out = set(), []
    for _gid, code, name, is_main in database.get_all_registrations_for_discord_id(str(user["discord_id"])):
        if code not in seen:
            seen.add(code)
            out.append((code, name, is_main))
    return sorted(out, key=lambda a: not a[2])


async def _roster(ally_code: str) -> dict:
    roster = database.get_player_units_bulk([ally_code]).get(ally_code)
    if roster:
        return roster
    # Игрок не из подключённой гильдии — его склада нет в кэше: один запрос в игру.
    try:
        from swgoh_comlink import SwgohComlink
        await activity_diff.refresh_player_units(SwgohComlink(url="http://localhost:3000"), ally_code)
    except Exception as e:
        print(f"⚠️ [Завоевание] склад {ally_code} не получен: {e!r}")
        return {}
    return database.get_player_units_bulk([ally_code]).get(ally_code) or {}


@router.get("", response_class=HTMLResponse)
async def conquest_page(request: Request, diff: str = "", acc: str = "", user: dict = Depends(get_current_user)):
    state = conquest.state()
    ctx = {"user": user, "state": state}
    accounts = _accounts(user)
    account = next((a for a in accounts if a[0] == acc), None)
    typed = guild_resolver.normalize_ally_code(acc) if acc and account is None else None
    if typed:
        account = (typed, typed, 0)
    elif account is None:
        account = next((a for a in accounts if a[2]), None) or (accounts[0] if accounts else None)
    ctx["typed_code"] = typed
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
            "who": _who(d["feats"], (await _roster(account[0]) or None) if account else None),
            "accounts": accounts, "account": account,
            "kind_labels": conquest.KIND_LABELS,
            "period": f"{_msk(state['start'])} — {_msk(state['end'])} МСК",
            "started": state["start"] <= datetime.now(timezone.utc).timestamp() * 1000,
        })
    return templates.TemplateResponse(request, "conquest.html", ctx)


@router.post("/save")
async def conquest_save(request: Request, user: dict = Depends(get_current_user)):
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
    acc = form.get("acc", "")
    return RedirectResponse(f"/conquest?diff={diff}" + (f"&acc={acc}" if acc.isdigit() else ""), status_code=303)
