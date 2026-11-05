"""Деф на ВГ — веб (/tw/def): библиотека деф-паков гильдии с требованиями к юнитам,
планы расстановки под соперника на карте из 10 зон, подбор игроков (вручную через окно
кандидатов или автоматически) и текст расстановки для Discord. Логика — tw_def_engine.py
(её же потом использует бот), здесь только HTTP и шаблоны."""
import asyncio
import copy
import json
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlencode, urlsplit

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

import database
import tw_def_engine as engine
from services import datacron_catalog, feature_flags, stat_forecast, tw_def_notify, tw_def_library, tw_workspace as work

templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent.parent / "templates"))

_require = feature_flags.require_feature("tw_order")


async def protect_plan(request: Request, user: dict = Depends(_require)):
    """Cross-process write lease + optimistic version + persistent undo history."""
    origin = request.headers.get("origin")
    if request.method == "POST" and origin and urlsplit(origin).netloc != request.url.netloc:
        raise HTTPException(403, "Запрос должен быть отправлен с этого сайта.")
    match = re.search(r"/tw/def/plans/(\d+)/([^/]+)$", request.url.path)
    if request.method != "POST" or not match or match[2] in ("preview", "template-save"):
        yield
        return
    guild_id, plan_id = user["guild_id"], int(match[1])
    if not database.get_tw_def_plan(guild_id, plan_id):
        raise HTTPException(404, "План не найден")
    form = await request.form()
    try:
        token = work.acquire(guild_id, plan_id)
    except ValueError as exc:
        raise HTTPException(409, str(exc))
    try:
        before = work.state(guild_id, plan_id)
        if form.get("revision") != work.digest(before):
            raise HTTPException(409, "План изменился. Обновите страницу и повторите действие — ваши изменения не применены.")
        work.remember(guild_id, plan_id, before, _who(user), "До изменения")
        yield
        after = work.state(guild_id, plan_id)
        if after["plan"] and work.digest(after) != work.digest(before):
            actions = {"assign": "Назначения", "autoadd": "Автоподбор в зону", "move": "Перенос отряда",
                       "unassign": "Снятие назначения", "clear": "Очистка", "settings": "Настройки",
                       "strategy": "Стратегия", "reserve": "Резерв атаки", "reserve-remove": "Снятие резерва",
                       "campaign": "Параметры ВГ", "apply": "Подбор всей защиты", "restore": "Восстановление версии",
                       "target-remove": "Удаление цели", "template-apply": "Применение шаблона", "order": "Приказ атаки",
                       "order-remove": "Удаление приказа"}
            work.remember(guild_id, plan_id, after, _who(user), actions.get(match[2], "Изменение плана"))
    finally:
        work.release(guild_id, plan_id, token)


router = APIRouter(dependencies=[Depends(protect_plan)])


def _with_reserves(plan, assignments, roster, library):
    return assignments + work.reserve_assignments(work.workspace(plan["guild_id"], plan["id"]), roster, library)


def _get_comlink():
    # Веб-процесс не поднимает бота — свой SwgohComlink на тот же сайдкар (как в guild_dashboard.py).
    from swgoh_comlink import SwgohComlink
    return SwgohComlink(url="http://localhost:3000")


def _who(user: dict) -> str:
    return f"web:{user['discord_id']}"


def _redirect(path: str, **params) -> RedirectResponse:
    params = {k: v for k, v in params.items() if v not in (None, "")}
    return RedirectResponse(path + (f"?{urlencode(params)}" if params else ""), status_code=303)


CALC_LOADING_TEXT = "Данные игры для проверки статов ещё загружаются — паки с требованиями к статам пока считаются недоступными."


def _warm_stat_calc() -> None:
    """Запускает фоновую сборку калькулятора статов (если его нет/устарел). Вызывается при
    входе на любую страницу раздела — к моменту работы с паками всё уже готово."""
    stat_forecast.start_background_build(_get_comlink())


async def _load_roster(guild_id: int, squads: list[dict], extra_base_ids=()) -> tuple[engine.Roster, str | None]:
    """Ростер + StatCalc, если какой-то пак требует статы. Никогда не ждёт сборку
    калькулятора (она долгая — вся игровая база из Comlink): берём уже собранный (даже
    устаревший, обновление идёт в фоне), а если его ещё нет — возвращаем предупреждение,
    страница показывает плашку загрузки и сама обновляется (web/static/tw_def.js)."""
    _warm_stat_calc()
    stat_calc = None
    warning = None
    if engine.squads_need_stats(squads):
        stat_calc = stat_forecast.cached_stat_calc()
        if stat_calc is None:
            warning = CALC_LOADING_TEXT
    roster = await asyncio.to_thread(engine.load_roster, guild_id, squads, stat_calc=stat_calc, extra_base_ids=extra_base_ids)
    return roster, warning


def _stats_not_ready(squad: dict) -> bool:
    return engine.squads_need_stats([squad]) and stat_forecast.cached_stat_calc() is None


@router.get("/api/calc-status", response_class=JSONResponse)
async def calc_status(retry: int = 0, user: dict = Depends(_require)):
    """Опрос плашки «данные загружаются». retry=1 — повторить сборку после ошибки."""
    if retry:
        _warm_stat_calc()
    return stat_forecast.build_status()


_catalog_task: asyncio.Task | None = None


def _datacron_catalog_cached():
    """Каталог ДК для подписей уровней — только из кэша, без ожидания; если его нет,
    сборка уходит в фон (подписи появятся при следующем открытии страницы)."""
    global _catalog_task
    catalog = datacron_catalog._cache
    if catalog is None and (_catalog_task is None or _catalog_task.done()):
        async def build():
            try:
                await datacron_catalog.get_catalog(_get_comlink())
            except Exception as e:
                print(f"⚠️ [web] tw_def: каталог ДК не собрался: {e}")
        _catalog_task = asyncio.create_task(build())
    return catalog


def _dc_options(guild_id: int) -> list[dict]:
    """Требования ДК гильдии (страница «Датакроны») для выпадашки в редакторе пака."""
    from cogs.datacron_requirements import _focused_char_label, _level_label
    catalog = _datacron_catalog_cached()
    options = []
    for row in database.get_all_datacron_requirements(guild_id):
        req_id, set_id, pack, l3, l6, l9, comment = row[:7]
        season = catalog["seasons"].get(set_id) if catalog else None
        levels = []
        for n, value in ((3, l3), (6, l6), (9, l9)):
            label = _level_label(season[f"level{n}"] if season else [], value)
            if label and label != "-":
                levels.append(f"{n}: {label}")
        quantity = row[11] if len(row) > 11 and row[11] and row[11] > 1 else None
        title = pack or "ДК"
        label = f"Сезон {set_id} · {title} — " + "; ".join(levels) + (f" ×{quantity}" if quantity else "")
        options.append({"value": f"base:{req_id}", "label": label[:170], "short": title, "set_id": set_id})
    for row in database.get_all_datacron_focused_requirements(guild_id):
        req_id, set_id, pack, character_key, required_level = row[:5]
        char = _focused_char_label(catalog, set_id, character_key) if catalog else character_key
        title = pack or char
        options.append({"value": f"focused:{req_id}",
                        "label": f"Сезон {set_id} · {title} — персональный {char}, ур. {required_level}"[:170],
                        "short": title, "set_id": set_id})
    # Свежие сезоны сверху — ДК закончившихся сезонов у игроков уже нет.
    options.sort(key=lambda o: -o["set_id"])
    return options


def _parse_dc(value: str, required: bool, options: list[dict]) -> dict | None:
    if not value or value not in {o["value"] for o in options}:
        return None
    kind, req_id = value.split(":", 1)
    return {"kind": kind, "req_id": int(req_id), "required": bool(required)}


def _dc_value(dc: dict | None) -> str:
    return f"{dc['kind']}:{dc['req_id']}" if dc else ""


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
    dc_labels = {o["value"]: o["label"] for o in _dc_options(guild_id)} if any(s.get("datacron") for s in squads) else {}
    views = []
    for s in squads:
        v = _squad_view(s, roster)
        v["availability"] = engine.availability(roster, s)
        v["stats_pending"] = _stats_not_ready(s)
        if s.get("datacron"):
            v["dc_label"] = dc_labels.get(_dc_value(s["datacron"]), "требование ДК удалено на странице «Датакроны»")
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


@router.get("/squads/export")
async def squads_export(user: dict = Depends(_require)):
    payload = tw_def_library.export_library(user["guild_id"])
    return Response(json.dumps(payload, ensure_ascii=False, indent=2), media_type="application/json",
                    headers={"Content-Disposition": 'attachment; filename="def-packs.json"'})


@router.get("/squads/import", response_class=HTMLResponse)
async def squads_import_page(request: Request, user: dict = Depends(_require)):
    return templates.TemplateResponse(request, "tw_def_import.html", {"user": user})


@router.post("/squads/import", response_class=HTMLResponse)
async def squads_import(request: Request, library: UploadFile | None = File(None),
                        payload: str = Form(""), apply: str = Form(""), user: dict = Depends(_require)):
    context = {"user": user}
    try:
        if library and library.filename:
            content = await library.read(tw_def_library.MAX_BYTES + 1)
            if len(content) > tw_def_library.MAX_BYTES:
                raise ValueError("Файл больше 1 МБ.")
            payload = content.decode("utf-8-sig")
        rows = tw_def_library.preview(payload, user["guild_id"])
        if apply:
            form = await request.form()
            selected = {int(v) for v in form.getlist("selected") if str(v).isdigit()}
            count = tw_def_library.import_selected(payload, user["guild_id"], selected, _who(user))
            context["imported"] = count
        else:
            context.update({"rows": rows, "payload": payload})
    except (ValueError, TypeError, AttributeError) as e:
        context["error"] = str(e)
    return templates.TemplateResponse(request, "tw_def_import.html", context)


def _editor_payload(squad: dict | None) -> dict:
    if not squad:
        return {"id": None, "name": "", "combat_type": "character", "note": "", "slots": [{"options": []}],
                "datacron": None}
    base_ids = list(engine.squad_base_ids(squad))
    names = database.get_game_unit_names(base_ids)
    omicron = database.get_omicron_capable(base_ids)
    slots = [{"options": [{**opt, "name": names.get(opt["base_id"]) or opt["base_id"],
                           "has_omicron": opt["base_id"] in omicron} for opt in slot["options"]]}
             for slot in squad["slots"]]
    return {"id": squad["id"], "name": squad["name"], "combat_type": squad["combat_type"],
            "note": squad.get("note") or "", "slots": slots, "datacron": squad.get("datacron")}


def _editor_context(user: dict, squad: dict, errors: list) -> dict:
    return {
        "user": user, "squad": squad, "errors": errors, "max_slots": engine.MAX_SLOTS,
        "stat_choices": [{"key": k, "label": label} for k, label, _short, _pct in engine.STAT_CHOICES],
        "dc_options": _dc_options(user["guild_id"]),
        "dc_value": _dc_value(squad.get("datacron")),
    }


@router.get("/squads/new", response_class=HTMLResponse)
async def squad_new(request: Request, user: dict = Depends(_require)):
    _warm_stat_calc()
    return templates.TemplateResponse(request, "tw_def_squad_edit.html", _editor_context(user, _editor_payload(None), []))


@router.get("/squads/{squad_id}/edit", response_class=HTMLResponse)
async def squad_edit(squad_id: int, request: Request, user: dict = Depends(_require)):
    _warm_stat_calc()
    squad = database.get_tw_def_squad(user["guild_id"], squad_id)
    if not squad:
        raise HTTPException(404, "Пак не найден.")
    return templates.TemplateResponse(request, "tw_def_squad_edit.html", _editor_context(user, _editor_payload(squad), []))


@router.post("/squads/save", response_class=HTMLResponse)
async def squad_save(
    request: Request,
    squad_id: str = Form(""),
    name: str = Form(""),
    combat_type: str = Form("character"),
    note: str = Form(""),
    slots_json: str = Form("[]"),
    datacron: str = Form(""),
    datacron_required: str = Form(""),
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
    dc = _parse_dc(datacron, bool(datacron_required), _dc_options(guild_id)) if combat_type == "character" else None
    if errors:
        draft = {"id": sid, "name": name, "combat_type": combat_type, "note": note,
                 "slots": raw_slots if isinstance(raw_slots, list) else [], "datacron": dc}
        names = database.get_game_unit_names(base_ids) if base_ids else {}
        for s in draft["slots"]:
            for o in (s.get("options") or []) if isinstance(s, dict) else []:
                if isinstance(o, dict) and o.get("base_id"):
                    o["name"] = names.get(o["base_id"]) or o["base_id"]
        return templates.TemplateResponse(request, "tw_def_squad_edit.html", _editor_context(user, draft, errors),
                                          status_code=400)
    database.save_tw_def_squad(guild_id, sid, name, combat_type, slots, note.strip() or None, _who(user), dc)
    return _redirect("/tw/def/squads", saved=name)


@router.post("/squads/{squad_id}/copy", response_class=HTMLResponse)
async def squad_copy(squad_id: int, user: dict = Depends(_require)):
    squad = database.get_tw_def_squad(user["guild_id"], squad_id)
    if not squad:
        raise HTTPException(404, "Пак не найден.")
    new_id = database.save_tw_def_squad(user["guild_id"], None, f"{squad['name']} (копия)", squad["combat_type"],
                                        squad["slots"], squad.get("note"), _who(user), squad.get("datacron"))
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
    from services.player_profile import _skill_counts
    from services import unit_forecast

    calc = roster.stat_calc or stat_forecast.cached_stat_calc()
    ship = squad["combat_type"] == "ship"
    full_units = database.get_player_units_bulk(roster.ally_codes) if ship and calc else {}
    out = []
    for c in candidates:
        units = roster.units.get(c["ally_code"], {})
        gp = 0 if c["units"] else None
        unit_rows = []
        for b in c["units"]:
            unit = units.get(b) or {}
            zetas, omicrons = _skill_counts(unit, roster.skills)
            unit_rows.append({"base_id": b, "name": roster.unit_name(b),
                              "badge": engine.unit_badge(unit, squad["combat_type"]),
                              "zetas": zetas, "omicrons_tw": omicrons.count("ВГ"),
                              "omicrons": len(omicrons), "is_ship": ship})
            try:
                if ship and calc:
                    crew = unit_forecast.ship_crew(calc, list((full_units.get(c["ally_code"]) or {}).values()), b)
                    value = unit_forecast._ship_snapshot(calc, unit, crew)["gp"]
                else:
                    value = unit.get("gp") or (calc.calc_char_gp(copy.deepcopy(unit)) if calc else None)
                gp = gp + int(value) if gp is not None and value is not None else None
            except Exception:
                gp = None
        out.append({
            "ally_code": c["ally_code"],
            "name": c["name"],
            "status": c["status"],
            "reason": c["reason"],
            "assigned": c["assigned"],
            "conflict": c["conflict"],
            "power": c["power"],
            "gp": gp,
            "dc": c.get("dc"),
            "suggested": c["ally_code"] in suggested,
            "units": unit_rows,
        })
    order = {"ok": 0, "used": 1, "no": 2, "excluded": 3}
    out.sort(key=lambda c: (order.get(c["status"], 9), -(c["gp"] or 0), c["name"].lower()))
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
    plans = database.list_tw_def_plans(user["guild_id"])
    for p in plans:
        p["revision"] = work.revision(user["guild_id"], p["id"])
    return templates.TemplateResponse(request, "tw_def_plans.html", {
        "user": user,
        "plans": plans,
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
    ws = work.workspace(guild_id, plan_id)
    blocked_assignments = assignments + work.reserve_assignments(ws, roster, squads)
    issues = await asyncio.to_thread(work.audit, plan, assignments, ws, roster, squads)
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
        av = engine.availability(roster, s, blocked_assignments, excluded)
        squad_stats.append({"id": s["id"], "name": s["name"], "combat_type": s["combat_type"], "placed": placed,
                            "stats_pending": _stats_not_ready(s), **av})

    free_by_squad = {st["id"]: (None if st["stats_pending"] else st["can_now"]) for st in squad_stats}

    zones = []
    for z in engine.ZONES:
        zone_assignments = [a for a in assignments if a["zone"] == z["key"]]
        groups: dict = {}
        for a in zone_assignments:
            g = groups.setdefault((a["squad_id"], a["squad_name"]), {"squad_name": a["squad_name"], "squad_id": a["squad_id"], "rows": []})
            g["rows"].append({
                "id": a["id"],
                "ally_code": a["ally_code"],
                "name": names.get(a["ally_code"]) or a["ally_code"],
                "gone": a["ally_code"] not in roster_set or a["ally_code"] in departed,
                "excluded": a["ally_code"] in excluded,
                "dc": bool(a.get("datacron_id")),
                "units": unit_cells(a),
            })
        for g in groups.values():
            g["rows"].sort(key=lambda r: r["name"].lower())
        capacity = engine.zone_capacity(plan, z["key"])
        targets = [t for t in ws["targets"] if t["zone"] == z["key"]]
        zones.append({**z, "groups": list(groups.values()), "count": len(zone_assignments), "capacity": capacity,
                      "issue_count": sum(i["zone"] == z["key"] for i in issues),
                      "target_count": sum(t["count"] for t in targets),
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
                         "squad_name": a["squad_name"], "units": unit_cells(a),
                         "kind": engine.ZONES_BY_KEY.get(a["zone"], {}).get("kind")} for a in by_player.get(ally_code, [])],
        })

    capacity_total = sum(z["capacity"] for z in zones)
    view = request.query_params.get("view", "map")
    return templates.TemplateResponse(request, "tw_def_plan.html", {
        "user": user,
        "plan": plan,
        "workspace": ws,
        "revision": work.revision(guild_id, plan_id),
        "issues": issues,
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
        "view": view if view in ("map", "players", "problems") else "map",
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
    ws = work.workspace(user["guild_id"], new_id)
    ws["targets"] = work.workspace(user["guild_id"], plan_id)["targets"]
    work.save_workspace(user["guild_id"], new_id, ws)
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
    assignments = _with_reserves(plan, assignments, roster, library)
    candidates = engine.evaluate_candidates(roster, squad, assignments, set(plan["excluded"]), library)
    free = engine.zone_capacity(plan, zone) - sum(1 for a in assignments if a["zone"] == zone)
    # Предотмечаем столько, сколько офицер вписал в поле «сколько» у зоны (не всю зону).
    want = min(free, count) if count > 0 else free
    suggested = {c["ally_code"] for c in engine.suggest(candidates, want)}
    return {
        "squad": squad["name"],
        "zone": z["label"],
        "free": free,
        "warning": warning if engine.squads_need_stats([squad]) else None,
        "candidates": _candidate_json(roster, squad, candidates, suggested),
    }


async def _assign(guild_id: int, plan: dict, zone: str, squad: dict, ally_codes: list[str], user: dict) -> tuple[int, list[str]]:
    """Назначает игроков, перепроверяя каждого на сервере (план мог поменяться, пока было
    открыто окно). Возвращает (сколько добавлено, пропущенные с причиной)."""
    library = database.list_tw_def_squads(guild_id)
    assignments = database.get_tw_def_assignments(guild_id, plan["id"])
    roster, _warning = await _load_roster(guild_id, library)
    assignments = _with_reserves(plan, assignments, roster, library)
    used = engine.used_units_by_player(assignments)
    used_dc = engine.used_datacrons_by_player(assignments)
    dc = squad.get("datacron")
    free = engine.zone_capacity(plan, zone) - sum(1 for a in assignments if a["zone"] == zone)
    excluded = set(plan["excluded"])
    rows, skipped = [], []
    for ally_code in ally_codes:
        name = roster.names.get(ally_code) or ally_code
        if ally_code not in roster.ally_codes:
            skipped.append(f"{name} — нет в текущем составе")
            continue
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
        # ДК: свободный подходящий датакрон отдаётся этому паку (один ДК — один пак плана).
        dc_state, dc_id = engine.pick_datacron(roster, ally_code, dc, used_dc.get(ally_code, frozenset()))
        if dc and dc.get("required") and dc_state != "ok":
            skipped.append(f"{name} — {engine.DC_REASONS.get(dc_state, 'нет нужного ДК')}")
            continue
        used.setdefault(ally_code, set()).update(match.units)
        if dc_id:
            used_dc.setdefault(ally_code, set()).update(dc_id.split(","))
        rows.append((zone, squad["id"], squad["name"], ally_code, match.units, dc_id))
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
    if _stats_not_ready(squad):
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
    if _stats_not_ready(squad):
        return _redirect(f"/tw/def/plans/{plan_id}", error=CALC_LOADING_TEXT)
    library = database.list_tw_def_squads(guild_id)
    assignments = database.get_tw_def_assignments(guild_id, plan_id)
    roster, _warning = await _load_roster(guild_id, library)
    assignments = _with_reserves(plan, assignments, roster, library)
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


# ---- Рассылка плана в личку (services/tw_def_notify.py): кто где ставит и какой ДК.

async def _notify_catalog():
    try:
        return await datacron_catalog.get_catalog(_get_comlink())
    except Exception as e:
        print(f"⚠️ [web] tw_def notify: каталог ДК недоступен: {e}")
        return datacron_catalog._cache


@router.get("/plans/{plan_id}/notify", response_class=HTMLResponse)
async def plan_notify_page(plan_id: int, request: Request, user: dict = Depends(_require)):
    plan = _get_plan_or_404(user["guild_id"], plan_id)
    return templates.TemplateResponse(request, "tw_def_notify.html", _notify_context(user, plan, await _notify_catalog()))


@router.get("/plans/{plan_id}/notify/map/{ally_code}.png")
async def plan_notify_map(plan_id: int, ally_code: str, user: dict = Depends(_require)):
    from services import tw_def_image
    plan = _get_plan_or_404(user["guild_id"], plan_id)
    rows = await asyncio.to_thread(tw_def_notify.build_player_rows, user["guild_id"], plan, None)
    row = next((r for r in rows if r["ally_code"] == ally_code), None)
    if row is None:
        raise HTTPException(404, "У игрока нет назначений в этом плане")
    guild_name = (database.get_guild_config(user["guild_id"]) or {}).get("name") or "гильдия"
    png = await asyncio.to_thread(tw_def_image.render, guild_name, plan["name"], row)
    return Response(png, media_type="image/png", headers={"Cache-Control": "private, no-store"})


def _notify_context(user: dict, plan: dict, catalog, destinations=None, error=None):
    rows = tw_def_notify.build_player_rows(user["guild_id"], plan, catalog)
    guild_name = (database.get_guild_config(user["guild_id"]) or {}).get("name") or "гильдия"
    preview = tw_def_notify.format_dm(guild_name, plan["name"], rows[:1]) if rows else []
    destinations = destinations if destinations is not None else tw_def_notify.DESTINATIONS[tw_def_notify.guild_mode(user["guild_id"])]
    work.annotate_deliveries(user["guild_id"], plan["id"], rows, destinations)
    return {"user": user, "plan": plan, "rows": rows, "preview": "\n".join(preview), "report": None, "error": error,
            "revision": work.revision(user["guild_id"], plan["id"]), "destinations": destinations}


@router.post("/plans/{plan_id}/notify", response_class=HTMLResponse)
async def plan_notify_send(plan_id: int, request: Request, ally_codes: list[str] = Form([]),
                           destinations: list[str] = Form([]),
                           user: dict = Depends(_require)):
    plan = _get_plan_or_404(user["guild_id"], plan_id)
    mode = tw_def_notify.mode_for(destinations)
    from services import bot_profiles
    token = bot_profiles.token("discord", bot_profiles.guild_profile(user["guild_id"])) or ""
    error = "Выберите хотя бы один канал: Discord или Telegram." if mode is None else \
        "Отметьте хотя бы одного игрока." if not ally_codes else \
        "На сервере не задан токен Discord-бота." if "discord" in destinations and not token else None
    if error:
        return templates.TemplateResponse(request, "tw_def_notify.html",
            _notify_context(user, plan, datacron_catalog._cache, destinations, error), status_code=400)
    database.update_guild_config(user["guild_id"], tw_def_dm_mode=mode)
    report = await tw_def_notify.send_broadcast(token, user["guild_id"], plan, await _notify_catalog(), set(ally_codes), mode)
    return templates.TemplateResponse(request, "tw_def_notify.html", {
        "user": user, "plan": plan, "rows": [], "preview": "", "report": report, "error": None,
    })


@router.get("/command", response_class=HTMLResponse)
async def command_page(request: Request, user: dict = Depends(_require)):
    from services import guild_compare, tw_scout
    guild_id = user["guild_id"]
    plans = database.list_tw_def_plans(guild_id)
    selected = request.query_params.get("plan") or database.get_bot_state("tw_active_plan", guild_id=guild_id)
    plan = next((p for p in plans if str(p["id"]) == str(selected)), plans[0] if plans else None)
    ctx = {"user": user, "guild_name": (database.get_guild_config(guild_id) or {}).get("name") or "Мы",
           "plans": plans, "plan": plan, "phases": work.PHASES,
           "error": request.query_params.get("error"), "message": request.query_params.get("message"),
           "recent": guild_compare.load_recent_opponents(guild_id), "opponent_label": guild_compare.opponent_label}
    if plan:
        ws = work.workspace(guild_id, plan["id"])
        assignments = database.get_tw_def_assignments(guild_id, plan["id"])
        squads = database.list_tw_def_squads(guild_id)
        roster, warning = await _load_roster(guild_id, squads, {b for a in assignments for b in a["units"]})
        issues = await asyncio.to_thread(work.audit, plan, assignments, ws, roster, squads)
        report = tw_scout.load_report(guild_id, ws["opponent"]) if ws["opponent"] else None
        rows = await asyncio.to_thread(tw_def_notify.build_player_rows, guild_id, plan, datacron_catalog._cache)
        destinations = tw_def_notify.DESTINATIONS[tw_def_notify.guild_mode(guild_id)]
        work.annotate_deliveries(guild_id, plan["id"], rows, destinations)
        differences = []
        if report:
            for r in report.get("gls", []) + report.get("fleet", []):
                if r["own"] != r["opp"]:
                    differences.append({**r, "delta": r["own"] - r["opp"]})
            differences.sort(key=lambda r: -abs(r["delta"]))
        ctx.update(workspace=ws, revision=work.revision(guild_id, plan["id"]), issues=issues, warning=warning,
                   assigned=len(assignments), capacity=sum(engine.zone_capacity(plan, z["key"]) for z in engine.ZONES),
                   recipients=len(rows), delivered=sum(not r["changed"] for r in rows), last_sync=roster.last_sync,
                   differences=differences[:6], scout_report=report, zones=engine.ZONES,
                   results=[{"start": r[7], "opponent": r[5], "label":
                       datetime.fromtimestamp(r[8] or r[7], timezone(timedelta(hours=3))).strftime("%d.%m.%Y") +
                       f" · {r[4]} · {r[1]} : {r[2]}"} for r in database.get_recent_tw_events(guild_id, limit=50)],
                   result=next((r for r in database.get_recent_tw_events(guild_id, limit=50)
                                if str(r[7]) == str(ws.get("result_start")) and r[5] == ws["opponent"]), None))
    return templates.TemplateResponse(request, "tw_command.html", ctx)


@router.get("/plans/{plan_id}/strategy", response_class=HTMLResponse)
async def strategy_page(plan_id: int, request: Request, user: dict = Depends(_require)):
    plan = _get_plan_or_404(user["guild_id"], plan_id)
    squads = database.list_tw_def_squads(user["guild_id"])
    names = database.get_player_names(user["guild_id"])
    return templates.TemplateResponse(request, "tw_strategy.html", {
        "user": user, "plan": plan, "workspace": work.workspace(user["guild_id"], plan_id),
        "revision": work.revision(user["guild_id"], plan_id), "squads": squads,
        "squad_names": {s["id"]: s["name"] for s in squads}, "zones": engine.ZONES,
        "zone_names": {z["key"]: z["label"] for z in engine.ZONES}, "names": names,
        "players": [{"ally_code": c, "name": names.get(c, c)} for c, _ in database.get_roster_by_player_id(user["guild_id"]).values()],
        "strategy_templates": work.templates(user["guild_id"]), "versions": work.versions(user["guild_id"], plan_id),
        "error": request.query_params.get("error"), "message": request.query_params.get("message"),
    })


@router.post("/plans/{plan_id}/campaign")
async def campaign_save(plan_id: int, request: Request, user: dict = Depends(_require)):
    from services import mod_compare
    guild_id = user["guild_id"]
    form = await request.form()
    ws = work.workspace(guild_id, plan_id)
    opp = str(form.get("opponent_code") or form.get("opponent") or "").strip()
    if opp and opp != ws["opponent"]:
        lookup = await mod_compare.resolve(_get_comlink(), opp)
        if not lookup.ok:
            return _redirect("/tw", plan=plan_id, error=lookup.error)
        if lookup.swgoh_guild_id == (database.get_guild_config(guild_id) or {}).get("swgoh_guild_id"):
            return _redirect("/tw", plan=plan_id, error="Выбрана ваша гильдия.")
        ws["opponent"], ws["opponent_name"] = lookup.swgoh_guild_id, lookup.guild_name
    elif not opp:
        ws["opponent"], ws["opponent_name"] = "", ""
    phase = str(form.get("phase"))
    if phase not in work.PHASES:
        raise HTTPException(400, "Неизвестный этап ВГ")
    ws["phase"] = phase
    result_start = str(form.get("result_start") or "")
    if result_start and not any(str(r[7]) == result_start and r[5] == ws["opponent"]
                               for r in database.get_recent_tw_events(guild_id, limit=50)):
        raise HTTPException(400, "Результат не относится к выбранному сопернику")
    ws["result_start"] = result_start
    work.save_workspace(guild_id, plan_id, ws)
    database.set_bot_state("tw_active_plan", str(plan_id), guild_id=guild_id)
    return _redirect("/tw", plan=plan_id, message="Параметры ВГ сохранены")


@router.post("/plans/{plan_id}/strategy")
async def strategy_save(plan_id: int, zone: str = Form(...), squad_id: int = Form(...), count: str = Form("1"),
                        priority: str = Form("2"), user: dict = Depends(_require)):
    guild_id = user["guild_id"]
    plan = _get_plan_or_404(guild_id, plan_id)
    z = _zone_or_400(zone)
    _squad_for_zone(guild_id, squad_id, z)
    ws = work.workspace(guild_id, plan_id)
    count_value = _int_in(count, 1)
    targets = [t for t in ws["targets"] if (t["zone"], t["squad_id"]) != (zone, squad_id)]
    if sum(t["count"] for t in targets if t["zone"] == zone) + count_value > engine.zone_capacity(plan, zone):
        return _redirect(f"/tw/def/plans/{plan_id}/strategy", error="Цели превышают вместимость зоны.")
    targets.append({"zone": zone, "squad_id": squad_id, "count": count_value, "priority": _int_in(priority, 2, 1, 3)})
    ws["targets"] = targets
    work.save_workspace(guild_id, plan_id, ws)
    return _redirect(f"/tw/def/plans/{plan_id}/strategy", message="Цель сохранена")


@router.post("/plans/{plan_id}/target-remove")
async def target_remove(plan_id: int, zone: str = Form(...), squad_id: int = Form(...), user: dict = Depends(_require)):
    ws = work.workspace(user["guild_id"], plan_id)
    ws["targets"] = [t for t in ws["targets"] if (t["zone"], t["squad_id"]) != (zone, squad_id)]
    work.save_workspace(user["guild_id"], plan_id, ws)
    return _redirect(f"/tw/def/plans/{plan_id}/strategy")


@router.post("/plans/{plan_id}/reserve")
async def reserve_save(plan_id: int, ally_code: str = Form(...), squad_id: int = Form(...), user: dict = Depends(_require)):
    if ally_code not in {c for c, _n in database.get_roster_by_player_id(user["guild_id"]).values()}:
        raise HTTPException(400, "Игрок не в текущем составе")
    if not database.get_tw_def_squad(user["guild_id"], squad_id):
        raise HTTPException(404, "Пак не найден")
    ws = work.workspace(user["guild_id"], plan_id)
    row = {"ally_code": ally_code, "squad_id": squad_id}
    if row not in ws["reserves"]:
        ws["reserves"].append(row)
    work.save_workspace(user["guild_id"], plan_id, ws)
    return _redirect(f"/tw/def/plans/{plan_id}/strategy", message="Резерв сохранён. Если юниты уже в защите, они отмечены в проблемах.")


@router.post("/plans/{plan_id}/reserve-remove")
async def reserve_remove(plan_id: int, ally_code: str = Form(...), squad_id: int = Form(...), user: dict = Depends(_require)):
    ws = work.workspace(user["guild_id"], plan_id)
    ws["reserves"] = [r for r in ws["reserves"] if (r["ally_code"], r["squad_id"]) != (ally_code, squad_id)]
    work.save_workspace(user["guild_id"], plan_id, ws)
    return _redirect(f"/tw/def/plans/{plan_id}/strategy")


@router.post("/plans/{plan_id}/preview", response_class=HTMLResponse)
async def plan_preview(plan_id: int, request: Request, user: dict = Depends(_require)):
    guild_id = user["guild_id"]
    before = work.state(guild_id, plan_id)
    plan = before["plan"]
    if not plan:
        raise HTTPException(404, "План не найден")
    squads = database.list_tw_def_squads(guild_id)
    roster, warning = await _load_roster(guild_id, squads)
    if warning:
        return _redirect(f"/tw/def/plans/{plan_id}/strategy", error=warning)
    proposal = await asyncio.to_thread(work.propose, plan, before["assignments"], before["workspace"], roster, squads)
    # A preview belongs to exactly the state it was computed against.
    rev = work.digest(before)
    token = work.save_proposal(guild_id, plan_id, rev, roster, proposal)
    return templates.TemplateResponse(request, "tw_proposal.html", {"user": user, "plan": plan, "proposal": proposal,
        "token": token, "revision": rev, "zones": {z["key"]: z["label"] for z in engine.ZONES},
        "unit_names": roster.unit_names})


@router.post("/plans/{plan_id}/apply")
async def plan_apply(plan_id: int, token: str = Form(...), user: dict = Depends(_require)):
    guild_id = user["guild_id"]
    p = work.proposal(guild_id, plan_id, token)
    if not p or p["revision"] != work.revision(guild_id, plan_id):
        raise HTTPException(409, "Предложение устарело. Выполните подбор заново.")
    squads = database.list_tw_def_squads(guild_id)
    roster, warning = await _load_roster(guild_id, squads)
    if warning or work.roster_hash(roster) != p["roster_hash"]:
        raise HTTPException(409, "Ростеры или требования изменились. Выполните подбор заново.")
    proposal = json.loads(p["data"])
    rows = [(a["zone"], a["squad_id"], a["squad_name"], a["ally_code"], a["units"], a["datacron_id"]) for a in proposal["rows"]]
    database.add_tw_def_assignments(guild_id, plan_id, rows, _who(user))
    return _redirect(f"/tw/def/plans/{plan_id}", message=f"Добавлено назначений: {len(rows)}")


@router.post("/plans/{plan_id}/template-save")
async def template_save(plan_id: int, name: str = Form(...), user: dict = Depends(_require)):
    plan = _get_plan_or_404(user["guild_id"], plan_id)
    if not name.strip() or len(name) > 80:
        raise HTTPException(400, "Название должно содержать от 1 до 80 символов")
    work.save_template(user["guild_id"], name.strip(), plan, work.workspace(user["guild_id"], plan_id))
    return _redirect(f"/tw/def/plans/{plan_id}/strategy", message="Шаблон сохранён без игроков и назначений")


@router.post("/plans/{plan_id}/template-apply")
async def template_apply(plan_id: int, template_id: int = Form(...), user: dict = Depends(_require)):
    guild_id = user["guild_id"]
    template = work.load_template(guild_id, template_id)
    if template is None:
        raise HTTPException(404, "Шаблон не найден")
    plan = _get_plan_or_404(guild_id, plan_id)
    ws = work.workspace(guild_id, plan_id)
    ws["targets"] = template["targets"]
    database.update_tw_def_plan(guild_id, plan_id, name=plan["name"], squads_per_zone=template["squads_per_zone"],
        fleets_per_zone=template["fleets_per_zone"], excluded=plan["excluded"], note=plan["note"])
    work.save_workspace(guild_id, plan_id, ws)
    return _redirect(f"/tw/def/plans/{plan_id}/strategy", message="Цели стратегии заменены. Назначения игроков сохранены.")


@router.post("/plans/{plan_id}/restore")
async def plan_restore(plan_id: int, version_id: int = Form(...), user: dict = Depends(_require)):
    try:
        work.restore(user["guild_id"], plan_id, version_id)
    except ValueError as exc:
        raise HTTPException(404, str(exc))
    return _redirect(f"/tw/def/plans/{plan_id}", message="Версия восстановлена. Проверьте проблемы и изменения рассылки.")


@router.get("/plans/{plan_id}/attack", response_class=HTMLResponse)
async def attack_page(plan_id: int, request: Request, user: dict = Depends(_require)):
    guild_id = user["guild_id"]
    plan = _get_plan_or_404(guild_id, plan_id)
    packs = database.get_tw_counter_packs(guild_id)
    selected = request.query_params.get("pack", packs[0] if packs else "")
    counters = database.get_tw_counters_for_pack(guild_id, selected) if selected in packs else []
    ws = work.workspace(guild_id, plan_id)
    lines = [f"ВГ — {plan['name']}"]
    for o in ws["orders"]:
        lines.append(f"\n{o['zone']} — {o['enemy']} → {o['counter']}\n{o['composition']}\n{o['note']}")
        if o.get("datacron"):
            lines.append("Датакрон: " + o["datacron"])
        if o.get("guide"):
            lines.append(o["guide"])
        if o.get("team_code"):
            lines.append("Команда: " + o["team_code"])
    return templates.TemplateResponse(request, "tw_attack.html", {"user": user, "plan": plan, "workspace": ws,
        "revision": work.revision(guild_id, plan_id), "packs": packs, "selected_pack": selected, "counters": counters,
        "zones": engine.ZONES, "order_text": "\n".join(lines), "error": request.query_params.get("error")})


@router.post("/plans/{plan_id}/order")
async def order_save(plan_id: int, zone: str = Form(...), counter_id: int = Form(...), note: str = Form(""), user: dict = Depends(_require)):
    z = _zone_or_400(zone)
    counter = database.get_tw_counter_by_id(user["guild_id"], counter_id)
    if not counter:
        raise HTTPException(404, "Контра не найдена")
    ws = work.workspace(user["guild_id"], plan_id)
    ws["orders"].append({"id": os.urandom(8).hex(), "zone": z["label"], "enemy": counter[1], "counter": counter[2] or "Контра из гайда",
                         "composition": counter[3] or "", "note": note.strip()[:1000], "guide": counter[5] or counter[9] or "",
                         "datacron": counter[4] or "", "team_code": counter[6] or ""})
    work.save_workspace(user["guild_id"], plan_id, ws)
    return _redirect(f"/tw/def/plans/{plan_id}/attack")


@router.post("/plans/{plan_id}/order-remove")
async def order_remove(plan_id: int, order_id: str = Form(...), user: dict = Depends(_require)):
    ws = work.workspace(user["guild_id"], plan_id)
    ws["orders"] = [o for o in ws["orders"] if o["id"] != order_id]
    work.save_workspace(user["guild_id"], plan_id, ws)
    return _redirect(f"/tw/def/plans/{plan_id}/attack")
