"""Деф на ВГ — веб (/tw/def): библиотека деф-паков гильдии с требованиями к юнитам,
планы расстановки под соперника на карте из 10 зон, подбор игроков (вручную через окно
кандидатов или автоматически) и текст расстановки для Discord. Логика — tw_def_engine.py
(её же потом использует бот), здесь только HTTP и шаблоны."""
import json
from pathlib import Path
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

import database
import tw_def_engine as engine
from services import feature_flags, stat_forecast

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent.parent / "templates"))

_require = feature_flags.require_feature("tw_order")


def _get_comlink():
    # Веб-процесс не поднимает бота — свой SwgohComlink на тот же сайдкар (как в guild_dashboard.py).
    from swgoh_comlink import SwgohComlink
    return SwgohComlink(url="http://localhost:3000")


def _who(user: dict) -> str:
    return f"web:{user['discord_id']}"


def _redirect(path: str, **params) -> RedirectResponse:
    params = {k: v for k, v in params.items() if v not in (None, "")}
    return RedirectResponse(path + (f"?{urlencode(params)}" if params else ""), status_code=303)


CALC_LOADING_TEXT = "Данные игры для проверки скорости ещё загружаются — паки с требованием скорости пока считаются недоступными."


def _warm_stat_calc() -> None:
    """Запускает фоновую сборку калькулятора статов (если его нет/устарел). Вызывается при
    входе на любую страницу раздела — к моменту работы с паками всё уже готово."""
    stat_forecast.start_background_build(_get_comlink())


async def _load_roster(guild_id: int, squads: list[dict], extra_base_ids=()) -> tuple[engine.Roster, str | None]:
    """Ростер + StatCalc, если какой-то пак требует скорость. Никогда не ждёт сборку
    калькулятора (она долгая — вся игровая база из Comlink): берём уже собранный (даже
    устаревший, обновление идёт в фоне), а если его ещё нет — возвращаем предупреждение,
    страница показывает плашку загрузки и сама обновляется (web/static/tw_def.js)."""
    _warm_stat_calc()
    stat_calc = None
    warning = None
    if engine.squads_need_speed(squads):
        stat_calc = stat_forecast.cached_stat_calc()
        if stat_calc is None:
            warning = CALC_LOADING_TEXT
    return engine.load_roster(guild_id, squads, stat_calc=stat_calc, extra_base_ids=extra_base_ids), warning


def _speed_not_ready(squad: dict) -> bool:
    return engine.squads_need_speed([squad]) and stat_forecast.cached_stat_calc() is None


@router.get("/api/calc-status", response_class=JSONResponse)
async def calc_status(retry: int = 0, user: dict = Depends(_require)):
    """Опрос плашки «данные загружаются». retry=1 — повторить сборку после ошибки."""
    if retry:
        _warm_stat_calc()
    return stat_forecast.build_status()


def _squad_view(squad: dict, roster: engine.Roster | None = None) -> dict:
    """Пак для шаблона: слоты с именами юнитов и подписями требований."""
    names = roster.unit_names if roster else database.get_game_unit_names(list(engine.squad_base_ids(squad)))
    slots = []
    for slot in squad["slots"]:
        slots.append([
            {**opt, "name": names.get(opt["base_id"]) or opt["base_id"],
             "req": engine.option_requirement_label(opt, squad["combat_type"])}
            for opt in slot["options"]
        ])
    search_text = " ".join([squad["name"]] + [o["name"] for slot in slots for o in slot]).lower()
    return {**squad, "slot_views": slots, "search_text": search_text}


# =====================================================================
# Библиотека паков
# =====================================================================
@router.get("/squads", response_class=HTMLResponse)
async def squads_page(request: Request, user: dict = Depends(_require)):
    guild_id = user["guild_id"]
    squads = database.list_tw_def_squads(guild_id)
    roster, warning = await _load_roster(guild_id, squads)
    views = []
    for s in squads:
        v = _squad_view(s, roster)
        v["availability"] = engine.availability(roster, s)
        v["speed_pending"] = _speed_not_ready(s)
        views.append(v)
    return templates.TemplateResponse(request, "tw_def_squads.html", {
        "user": user,
        "squads": views,
        "roster_size": len(roster.ally_codes),
        "last_sync": roster.last_sync,
        "warning": warning,
        "saved": request.query_params.get("saved"),
        "error": request.query_params.get("error"),
    })


def _editor_payload(squad: dict | None) -> dict:
    if not squad:
        return {"id": None, "name": "", "combat_type": "character", "note": "", "slots": [{"options": []}]}
    base_ids = list(engine.squad_base_ids(squad))
    names = database.get_game_unit_names(base_ids)
    omicron = database.get_omicron_capable(base_ids)
    slots = [{"options": [{**opt, "name": names.get(opt["base_id"]) or opt["base_id"],
                           "has_omicron": opt["base_id"] in omicron} for opt in slot["options"]]}
             for slot in squad["slots"]]
    return {"id": squad["id"], "name": squad["name"], "combat_type": squad["combat_type"],
            "note": squad.get("note") or "", "slots": slots}


@router.get("/squads/new", response_class=HTMLResponse)
async def squad_new(request: Request, user: dict = Depends(_require)):
    _warm_stat_calc()
    return templates.TemplateResponse(request, "tw_def_squad_edit.html", {
        "user": user, "squad": _editor_payload(None), "errors": [], "max_slots": engine.MAX_SLOTS,
    })


@router.get("/squads/{squad_id}/edit", response_class=HTMLResponse)
async def squad_edit(squad_id: int, request: Request, user: dict = Depends(_require)):
    _warm_stat_calc()
    squad = database.get_tw_def_squad(user["guild_id"], squad_id)
    if not squad:
        raise HTTPException(404, "Пак не найден.")
    return templates.TemplateResponse(request, "tw_def_squad_edit.html", {
        "user": user, "squad": _editor_payload(squad), "errors": [], "max_slots": engine.MAX_SLOTS,
    })


@router.post("/squads/save", response_class=HTMLResponse)
async def squad_save(
    request: Request,
    squad_id: str = Form(""),
    name: str = Form(""),
    combat_type: str = Form("character"),
    note: str = Form(""),
    slots_json: str = Form("[]"),
    user: dict = Depends(_require),
):
    guild_id = user["guild_id"]
    sid = int(squad_id) if squad_id.isdigit() else None
    if sid and not database.get_tw_def_squad(guild_id, sid):
        raise HTTPException(404, "Пак не найден.")
    combat_type = combat_type if combat_type in engine.MAX_SLOTS else "character"
    errors = []
    try:
        raw_slots = json.loads(slots_json or "[]")
    except ValueError:
        raw_slots = []
        errors.append("Не удалось прочитать слоты.")
    base_ids = [str(o.get("base_id")) for s in raw_slots if isinstance(s, dict)
                for o in (s.get("options") or []) if isinstance(o, dict) and o.get("base_id")]
    known = database.get_unit_types(base_ids) if base_ids else {}
    omicron_capable = database.get_omicron_capable(base_ids) if base_ids else set()
    slots, slot_errors = engine.normalize_slots(raw_slots, combat_type, known, omicron_capable)
    errors += slot_errors
    name = name.strip()
    if not name:
        errors.append("Укажите название пака.")
    if errors:
        draft = {"id": sid, "name": name, "combat_type": combat_type, "note": note,
                 "slots": raw_slots if isinstance(raw_slots, list) else []}
        names = database.get_game_unit_names(base_ids) if base_ids else {}
        for s in draft["slots"]:
            for o in (s.get("options") or []) if isinstance(s, dict) else []:
                if isinstance(o, dict) and o.get("base_id"):
                    o["name"] = names.get(o["base_id"]) or o["base_id"]
        return templates.TemplateResponse(request, "tw_def_squad_edit.html", {
            "user": user, "squad": draft, "errors": errors, "max_slots": engine.MAX_SLOTS,
        }, status_code=400)
    database.save_tw_def_squad(guild_id, sid, name, combat_type, slots, note.strip() or None, _who(user))
    return _redirect("/tw/def/squads", saved=name)


@router.post("/squads/{squad_id}/copy", response_class=HTMLResponse)
async def squad_copy(squad_id: int, user: dict = Depends(_require)):
    squad = database.get_tw_def_squad(user["guild_id"], squad_id)
    if not squad:
        raise HTTPException(404, "Пак не найден.")
    new_id = database.save_tw_def_squad(user["guild_id"], None, f"{squad['name']} (копия)", squad["combat_type"],
                                        squad["slots"], squad.get("note"), _who(user))
    return RedirectResponse(f"/tw/def/squads/{new_id}/edit", status_code=303)


@router.post("/squads/{squad_id}/delete", response_class=HTMLResponse)
async def squad_delete(squad_id: int, user: dict = Depends(_require)):
    database.delete_tw_def_squad(user["guild_id"], squad_id)
    return _redirect("/tw/def/squads")


@router.get("/api/units", response_class=JSONResponse)
async def units_search(q: str = "", type: str = "", user: dict = Depends(_require)):
    if not q or len(q.strip()) < 2:
        return []
    rows = database.search_game_units(q.strip(), limit=40)
    types = database.get_unit_types([b for b, _ in rows])
    omicron = database.get_omicron_capable([b for b, _ in rows])
    result = [{"base_id": b, "name": n, "type": types.get(b, "character"), "has_omicron": b in omicron} for b, n in rows]
    if type in engine.MAX_SLOTS:
        result = [r for r in result if r["type"] == type]
    return result[:20]


def _candidate_json(roster: engine.Roster, squad: dict, candidates: list[dict], suggested: set[str]) -> list[dict]:
    out = []
    for c in candidates:
        units = roster.units.get(c["ally_code"], {})
        out.append({
            "ally_code": c["ally_code"],
            "name": c["name"],
            "status": c["status"],
            "reason": c["reason"],
            "assigned": c["assigned"],
            "conflict": c["conflict"],
            "power": c["power"],
            "suggested": c["ally_code"] in suggested,
            "units": [{"base_id": b, "name": roster.unit_name(b),
                       "badge": engine.unit_badge(units.get(b), squad["combat_type"])} for b in c["units"]],
        })
    order = {"ok": 0, "used": 1, "no": 2, "excluded": 3}
    out.sort(key=lambda c: (order.get(c["status"], 9), not c["suggested"], c["name"].lower()))
    return out


@router.get("/api/squads/{squad_id}/check", response_class=JSONResponse)
async def squad_check(squad_id: int, user: dict = Depends(_require)):
    """Кто в гильдии может поставить пак (без учёта планов) — окно «кто может» в библиотеке."""
    squad = database.get_tw_def_squad(user["guild_id"], squad_id)
    if not squad:
        raise HTTPException(404, "Пак не найден.")
    roster, warning = await _load_roster(user["guild_id"], [squad])
    candidates = engine.evaluate_candidates(roster, squad, [])
    return {"squad": squad["name"], "warning": warning,
            "candidates": _candidate_json(roster, squad, candidates, set())}


# =====================================================================
# Планы расстановки
# =====================================================================
@router.get("/plans", response_class=HTMLResponse)
async def plans_page(request: Request, user: dict = Depends(_require)):
    _warm_stat_calc()
    return templates.TemplateResponse(request, "tw_def_plans.html", {
        "user": user,
        "plans": database.list_tw_def_plans(user["guild_id"]),
        "squad_count": len(database.list_tw_def_squads(user["guild_id"])),
        "error": request.query_params.get("error"),
    })


def _int_in(value: str, default: int, lo: int = 1, hi: int = 100) -> int:
    try:
        return max(lo, min(hi, int(value)))
    except (TypeError, ValueError):
        return default


@router.post("/plans/create", response_class=HTMLResponse)
async def plan_create(
    name: str = Form(""),
    squads_per_zone: str = Form("40"),
    fleets_per_zone: str = Form("40"),
    user: dict = Depends(_require),
):
    name = name.strip()
    if not name:
        return _redirect("/tw/def/plans", error="Укажите название плана.")
    plan_id = database.create_tw_def_plan(user["guild_id"], name, _int_in(squads_per_zone, 40),
                                          _int_in(fleets_per_zone, 40), _who(user))
    return RedirectResponse(f"/tw/def/plans/{plan_id}", status_code=303)


def _get_plan_or_404(guild_id: int, plan_id: int) -> dict:
    plan = database.get_tw_def_plan(guild_id, plan_id)
    if not plan:
        raise HTTPException(404, "План не найден.")
    return plan


@router.get("/plans/{plan_id}", response_class=HTMLResponse)
async def plan_page(plan_id: int, request: Request, user: dict = Depends(_require)):
    guild_id = user["guild_id"]
    plan = _get_plan_or_404(guild_id, plan_id)
    squads = database.list_tw_def_squads(guild_id)
    assignments = database.get_tw_def_assignments(guild_id, plan_id)
    extra = {b for a in assignments for b in a["units"]}
    roster, warning = await _load_roster(guild_id, squads, extra_base_ids=extra)
    excluded = set(plan["excluded"])
    names = dict(roster.names)
    departed = set(database.get_departed_players(guild_id))
    roster_set = set(roster.ally_codes)

    def unit_cells(a: dict) -> list[dict]:
        units = roster.units.get(a["ally_code"], {})
        kind = "ship" if engine.ZONES_BY_KEY.get(a["zone"], {}).get("kind") == "ship" else "character"
        return [{"base_id": b, "name": roster.unit_name(b), "badge": engine.unit_badge(units.get(b), kind)}
                for b in a["units"]]

    squad_stats = []
    for s in squads:
        placed = sum(1 for a in assignments if a["squad_id"] == s["id"])
        av = engine.availability(roster, s, assignments, excluded)
        squad_stats.append({"id": s["id"], "name": s["name"], "combat_type": s["combat_type"], "placed": placed,
                            "speed_pending": _speed_not_ready(s), **av})

    free_by_squad = {st["id"]: (None if st["speed_pending"] else st["can_now"]) for st in squad_stats}

    zones = []
    for z in engine.ZONES:
        zone_assignments = [a for a in assignments if a["zone"] == z["key"]]
        groups: dict = {}
        for a in zone_assignments:
            g = groups.setdefault(a["squad_name"], {"squad_name": a["squad_name"], "squad_id": a["squad_id"], "rows": []})
            g["rows"].append({
                "id": a["id"],
                "ally_code": a["ally_code"],
                "name": names.get(a["ally_code"]) or a["ally_code"],
                "gone": a["ally_code"] not in roster_set or a["ally_code"] in departed,
                "excluded": a["ally_code"] in excluded,
                "units": unit_cells(a),
            })
        for g in groups.values():
            g["rows"].sort(key=lambda r: r["name"].lower())
        capacity = engine.zone_capacity(plan, z["key"])
        zones.append({**z, "groups": list(groups.values()), "count": len(zone_assignments), "capacity": capacity,
                      "squads": [{"id": s["id"], "name": s["name"], "free": free_by_squad.get(s["id"], 0)}
                                 for s in squads if s["combat_type"] == z["kind"]]})

    columns = [[z for z in zones if z["column"] == col] for col in (4, 3, 2, 1)]

    counts = engine.squad_counts_by_player(assignments)
    player_rows = []
    zone_order = {z["key"]: i for i, z in enumerate(engine.ZONES)}
    by_player: dict[str, list] = {}
    for a in sorted(assignments, key=lambda a: (zone_order.get(a["zone"], 99), a["squad_name"])):
        by_player.setdefault(a["ally_code"], []).append(a)
    for ally_code in sorted(set(roster.ally_codes) | set(by_player), key=lambda c: (names.get(c) or c).lower()):
        player_rows.append({
            "ally_code": ally_code,
            "name": names.get(ally_code) or ally_code,
            "excluded": ally_code in excluded,
            "gone": ally_code not in roster_set,
            "count": counts.get(ally_code, 0),
            "entries": [{"id": a["id"], "zone": engine.ZONES_BY_KEY.get(a["zone"], {}).get("label", a["zone"]),
                         "squad_name": a["squad_name"], "units": unit_cells(a)} for a in by_player.get(ally_code, [])],
        })

    capacity_total = sum(z["capacity"] for z in zones)
    view = request.query_params.get("view", "map")
    return templates.TemplateResponse(request, "tw_def_plan.html", {
        "user": user,
        "plan": plan,
        "zones": zones,
        "columns": columns,
        "zone_options": engine.ZONES,
        "squad_stats": squad_stats,
        "players": player_rows,
        "roster_players": [{"ally_code": c, "name": names.get(c) or c} for c in roster.ally_codes],
        "assigned_total": len(assignments),
        "capacity_total": capacity_total,
        "players_used": len(counts),
        "players_total": len([c for c in roster.ally_codes if c not in excluded]),
        "last_sync": roster.last_sync,
        "warning": warning,
        "view": view if view in ("map", "players") else "map",
        "message": request.query_params.get("message"),
        "error": request.query_params.get("error"),
        "has_squads": bool(squads),
    })


@router.post("/plans/{plan_id}/settings", response_class=HTMLResponse)
async def plan_settings(
    plan_id: int,
    request: Request,
    name: str = Form(""),
    squads_per_zone: str = Form("40"),
    fleets_per_zone: str = Form("40"),
    note: str = Form(""),
    user: dict = Depends(_require),
):
    plan = _get_plan_or_404(user["guild_id"], plan_id)
    form = await request.form()
    excluded = [c for c in form.getlist("excluded") if c]
    database.update_tw_def_plan(
        user["guild_id"], plan_id, name=name.strip() or plan["name"],
        squads_per_zone=_int_in(squads_per_zone, plan["squads_per_zone"]),
        fleets_per_zone=_int_in(fleets_per_zone, plan["fleets_per_zone"]),
        excluded=excluded, note=note.strip() or None,
    )
    return _redirect(f"/tw/def/plans/{plan_id}", message="Настройки сохранены")


@router.post("/plans/{plan_id}/copy", response_class=HTMLResponse)
async def plan_copy(plan_id: int, user: dict = Depends(_require)):
    plan = _get_plan_or_404(user["guild_id"], plan_id)
    new_id = database.copy_tw_def_plan(user["guild_id"], plan_id, f"{plan['name']} (копия)", _who(user))
    return RedirectResponse(f"/tw/def/plans/{new_id}", status_code=303)


@router.post("/plans/{plan_id}/delete", response_class=HTMLResponse)
async def plan_delete(plan_id: int, user: dict = Depends(_require)):
    _get_plan_or_404(user["guild_id"], plan_id)
    database.delete_tw_def_plan(user["guild_id"], plan_id)
    return _redirect("/tw/def/plans")


def _zone_or_400(zone: str) -> dict:
    z = engine.ZONES_BY_KEY.get(zone)
    if not z:
        raise HTTPException(400, "Неизвестная зона.")
    return z


def _squad_for_zone(guild_id: int, squad_id: int, zone: dict) -> dict:
    squad = database.get_tw_def_squad(guild_id, squad_id)
    if not squad:
        raise HTTPException(404, "Пак не найден.")
    if squad["combat_type"] != zone["kind"]:
        raise HTTPException(400, "Флот ставится только во флотские зоны, персонажи — в наземные.")
    return squad


@router.get("/api/plans/{plan_id}/candidates", response_class=JSONResponse)
async def plan_candidates(plan_id: int, squad_id: int, zone: str, count: int = 0, user: dict = Depends(_require)):
    guild_id = user["guild_id"]
    plan = _get_plan_or_404(guild_id, plan_id)
    z = _zone_or_400(zone)
    squad = _squad_for_zone(guild_id, squad_id, z)
    library = database.list_tw_def_squads(guild_id)
    assignments = database.get_tw_def_assignments(guild_id, plan_id)
    roster, warning = await _load_roster(guild_id, library)
    candidates = engine.evaluate_candidates(roster, squad, assignments, set(plan["excluded"]), library)
    free = engine.zone_capacity(plan, zone) - sum(1 for a in assignments if a["zone"] == zone)
    # Предотмечаем столько, сколько офицер вписал в поле «сколько» у зоны (не всю зону).
    want = min(free, count) if count > 0 else free
    suggested = {c["ally_code"] for c in engine.suggest(candidates, want)}
    return {
        "squad": squad["name"],
        "zone": z["label"],
        "free": free,
        "warning": warning if engine.squads_need_speed([squad]) else None,
        "candidates": _candidate_json(roster, squad, candidates, suggested),
    }


async def _assign(guild_id: int, plan: dict, zone: str, squad: dict, ally_codes: list[str], user: dict) -> tuple[int, list[str]]:
    """Назначает игроков, перепроверяя каждого на сервере (план мог поменяться, пока было
    открыто окно). Возвращает (сколько добавлено, пропущенные с причиной)."""
    library = database.list_tw_def_squads(guild_id)
    assignments = database.get_tw_def_assignments(guild_id, plan["id"])
    roster, _warning = await _load_roster(guild_id, library)
    used = engine.used_units_by_player(assignments)
    free = engine.zone_capacity(plan, zone) - sum(1 for a in assignments if a["zone"] == zone)
    excluded = set(plan["excluded"])
    rows, skipped = [], []
    for ally_code in ally_codes:
        name = roster.names.get(ally_code) or ally_code
        if len(rows) >= free:
            skipped.append(f"{name} — зона заполнена")
            continue
        if ally_code in excluded:
            skipped.append(f"{name} — не участвует")
            continue
        match = engine.match_squad(roster, ally_code, squad, used.get(ally_code, set()))
        if match.units is None:
            skipped.append(f"{name} — {match.reason}")
            continue
        used.setdefault(ally_code, set()).update(match.units)
        rows.append((zone, squad["id"], squad["name"], ally_code, match.units))
    database.add_tw_def_assignments(guild_id, plan["id"], rows, _who(user))
    return len(rows), skipped


def _assign_message(zone_label: str, squad_name: str, added: int, skipped: list[str]) -> dict:
    params = {"message": f"{zone_label}: добавлено {squad_name} ×{added}"}
    if skipped:
        params["error"] = "Пропущены: " + "; ".join(skipped[:10]) + (" …" if len(skipped) > 10 else "")
    return params


@router.post("/plans/{plan_id}/assign", response_class=HTMLResponse)
async def plan_assign(plan_id: int, request: Request, zone: str = Form(...), squad_id: int = Form(...),
                      user: dict = Depends(_require)):
    guild_id = user["guild_id"]
    plan = _get_plan_or_404(guild_id, plan_id)
    z = _zone_or_400(zone)
    squad = _squad_for_zone(guild_id, squad_id, z)
    if _speed_not_ready(squad):
        return _redirect(f"/tw/def/plans/{plan_id}", error=CALC_LOADING_TEXT)
    form = await request.form()
    ally_codes = [c for c in form.getlist("ally_codes") if c]
    added, skipped = await _assign(guild_id, plan, zone, squad, ally_codes, user)
    params = _assign_message(z["label"], squad["name"], added, skipped)
    return RedirectResponse(f"/tw/def/plans/{plan_id}?{urlencode(params)}#zone-{zone}", status_code=303)


@router.post("/plans/{plan_id}/autoadd", response_class=HTMLResponse)
async def plan_autoadd(plan_id: int, zone: str = Form(...), squad_id: int = Form(...), count: str = Form("1"),
                       user: dict = Depends(_require)):
    guild_id = user["guild_id"]
    plan = _get_plan_or_404(guild_id, plan_id)
    z = _zone_or_400(zone)
    squad = _squad_for_zone(guild_id, squad_id, z)
    if _speed_not_ready(squad):
        return _redirect(f"/tw/def/plans/{plan_id}", error=CALC_LOADING_TEXT)
    library = database.list_tw_def_squads(guild_id)
    assignments = database.get_tw_def_assignments(guild_id, plan_id)
    roster, _warning = await _load_roster(guild_id, library)
    candidates = engine.evaluate_candidates(roster, squad, assignments, set(plan["excluded"]), library)
    asked = _int_in(count, 1, 1, 100)
    free = engine.zone_capacity(plan, zone) - sum(1 for a in assignments if a["zone"] == zone)
    want = min(asked, max(0, free))
    picked = engine.suggest(candidates, want)
    added, skipped = await _assign(guild_id, plan, zone, squad, [c["ally_code"] for c in picked], user)
    params = _assign_message(z["label"], squad["name"], added, skipped)
    if added < asked and not skipped:
        why = "в зоне больше нет мест" if added >= free else "больше нет свободных игроков с этим паком"
        params["error"] = f"Просили {asked}, добавлено {added}: {why}."
    return RedirectResponse(f"/tw/def/plans/{plan_id}?{urlencode(params)}#zone-{zone}", status_code=303)


@router.post("/plans/{plan_id}/unassign", response_class=HTMLResponse)
async def plan_unassign(plan_id: int, assignment_id: int = Form(...), back: str = Form(""),
                        user: dict = Depends(_require)):
    _get_plan_or_404(user["guild_id"], plan_id)
    database.delete_tw_def_assignment(user["guild_id"], plan_id, assignment_id)
    return RedirectResponse(f"/tw/def/plans/{plan_id}{_safe_back(back)}", status_code=303)


@router.post("/plans/{plan_id}/move", response_class=HTMLResponse)
async def plan_move(plan_id: int, assignment_id: int = Form(...), zone: str = Form(...),
                    user: dict = Depends(_require)):
    guild_id = user["guild_id"]
    plan = _get_plan_or_404(guild_id, plan_id)
    z = _zone_or_400(zone)
    assignments = database.get_tw_def_assignments(guild_id, plan_id)
    current = next((a for a in assignments if a["id"] == assignment_id), None)
    if not current:
        raise HTTPException(404, "Назначение не найдено.")
    cur_kind = engine.ZONES_BY_KEY.get(current["zone"], {}).get("kind")
    if cur_kind != z["kind"]:
        return _redirect(f"/tw/def/plans/{plan_id}", error="Флот переносится только во флотскую зону, персонажи — в наземную.")
    if sum(1 for a in assignments if a["zone"] == zone) >= engine.zone_capacity(plan, zone):
        return _redirect(f"/tw/def/plans/{plan_id}", error=f"{z['label']}: зона заполнена.")
    database.move_tw_def_assignment(guild_id, plan_id, assignment_id, zone)
    database.touch_tw_def_plan(guild_id, plan_id)
    return RedirectResponse(f"/tw/def/plans/{plan_id}#zone-{zone}", status_code=303)


@router.post("/plans/{plan_id}/clear", response_class=HTMLResponse)
async def plan_clear(plan_id: int, zone: str = Form(""), squad_id: str = Form(""), user: dict = Depends(_require)):
    _get_plan_or_404(user["guild_id"], plan_id)
    if zone:
        _zone_or_400(zone)
    database.clear_tw_def_assignments(user["guild_id"], plan_id, zone or None,
                                      int(squad_id) if squad_id.isdigit() else None)
    return RedirectResponse(f"/tw/def/plans/{plan_id}" + (f"#zone-{zone}" if zone else ""), status_code=303)


def _safe_back(back: str) -> str:
    """Возврат к якорю/виду после удаления — только "#..." или "?view=...", без внешних URL."""
    if back.startswith("#") or back.startswith("?view="):
        return back
    return ""


@router.get("/plans/{plan_id}/text", response_class=HTMLResponse)
async def plan_text(plan_id: int, request: Request, user: dict = Depends(_require)):
    guild_id = user["guild_id"]
    plan = _get_plan_or_404(guild_id, plan_id)
    assignments = database.get_tw_def_assignments(guild_id, plan_id)
    names = database.get_player_names(guild_id)
    for _discord_id, ally_code, ingame_name in database.get_all_user_mappings(guild_id):
        if ingame_name:
            names[ally_code] = ingame_name
    return templates.TemplateResponse(request, "tw_def_text.html", {
        "user": user,
        "plan": plan,
        "by_zone": engine.format_plan_by_zone(plan, assignments, names),
        "by_player": engine.format_plan_by_player(plan, assignments, names),
        "assigned_total": len(assignments),
    })
