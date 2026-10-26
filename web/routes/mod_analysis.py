"""Веб-only "Анализ модинга" (/mod-analysis) — форма (персонаж + целевой релик, опционально
код союзника/ID чужой гильдии) + рендер отчёта, расчёт целиком в services/mod_analysis.py.
GET с query-параметрами (как /mod-search, /steal-build) — результат можно заблокмаркить/
переоткрыть по ссылке.

Реализация ТЗ из "Описание модели анализа модинга персонажа по гильдии.pdf" (пользователь,
чат 2026-09-15). По умолчанию данные — уже закэшированный player_unit_cache своей гильдии,
без обращений к Comlink, кроме однократной сборки StatCalc. Опция "чужая гильдия" (добавлено
2026-09-16, по прямому запросу пользователя) переиспользует services.steal_build.resolve_guild
и бьёт по Comlink живьём, как /steal-build."""

import asyncio
import json
import time
from pathlib import Path
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

import database
import services.mod_analysis as mod_analysis
import services.mod_compare as mod_compare
import services.stat_forecast as stat_forecast
import services.steal_build as steal_build
from services import feature_flags

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent.parent / "templates"))

DEFAULT_RELIC = "9"


def _view_urls(character: str, relic_raw: str, guild_ref: str) -> dict:
    """Ссылки переключателя Δ/%/Итог (запрос пользователя 2026-09-22) — обычный GET-реюз
    страницы с другим view, тот же паттерн, что segmented-toggle в activity.html и др."""
    base_params = {"character": character, "relic": relic_raw}
    if guild_ref:
        base_params["guild_ref"] = guild_ref
    return {v: "/mod-analysis?" + urlencode({**base_params, "view": v}) for v in mod_analysis.VIEWS}


def _get_comlink():
    # Как в services/stat_forecast.py/steal_build.py — веб-процесс не поднимает диска-клиента,
    # строит свой поверх того же comlink-сайдкара.
    from swgoh_comlink import SwgohComlink
    return SwgohComlink(url="http://localhost:3000")


def _char_label(base_id: str) -> str:
    return database.get_game_unit_name(base_id) or base_id


def _history_rows(guild_id: int):
    return [
        {
            "id": hid, "character_label": _char_label(base_id), "base_id": base_id, "relic": relic,
            "created_at": created_at, "guild_ref": guild_ref or "", "guild_name": guild_name or "",
        }
        for hid, base_id, relic, _created_by, created_at, guild_ref, guild_name in database.get_mod_analysis_history(guild_id=guild_id)
    ]


@router.get("", response_class=HTMLResponse)
async def mod_analysis_page(request: Request, user: dict = Depends(feature_flags.require_feature("mod_analysis"))):
    qp = request.query_params
    guild_id = user["guild_id"]
    character = qp.get("character", "")
    relic_raw = qp.get("relic", DEFAULT_RELIC)
    guild_ref = qp.get("guild_ref", "").strip()
    view = qp.get("view", "delta")
    if view not in mod_analysis.VIEWS:
        view = "delta"

    history_id = qp.get("history_id")
    if history_id:
        entries = database.get_mod_analysis_history(guild_id=guild_id)
        entry = next((e for e in entries if e[0] == int(history_id)), None)
        if entry:
            _hid, character, relic_raw, _created_by, _created_at, guild_ref, _guild_name = entry
            relic_raw = str(relic_raw)
            guild_ref = guild_ref or ""

    context = {
        "user": user,
        "selected_character": character,
        "selected_character_label": _char_label(character) if character else "",
        "selected_relic": relic_raw,
        "guild_ref": guild_ref,
        "result": None,
        "loading": False,
        "error": None,
        "history": _history_rows(guild_id),
    }
    if qp.get("error"):
        context["error"] = qp.get("error")

    if not character:
        return templates.TemplateResponse(request, "mod_analysis.html", context)

    try:
        relic = int(relic_raw)
    except ValueError:
        context["error"] = "Некорректный уровень реликвии."
        return templates.TemplateResponse(request, "mod_analysis.html", context)
    if not (0 <= relic <= 10):
        context["error"] = "Релик должен быть от 0 до 10."
        return templates.TemplateResponse(request, "mod_analysis.html", context)

    try:
        comlink = _get_comlink()
        stat_calc = await stat_forecast.get_stat_calc(comlink)
    except Exception:
        context["loading"] = True
        return templates.TemplateResponse(request, "mod_analysis.html", context)

    guild_name_for_history = None
    if guild_ref:
        lookup = await steal_build.resolve_guild(comlink, guild_ref)
        if not lookup.ok:
            context["error"] = lookup.error
            return templates.TemplateResponse(request, "mod_analysis.html", context)
        guild_name_for_history = lookup.guild_name
        report = await mod_analysis.build_report_live(comlink, stat_calc, character, relic, lookup, view=view)
    else:
        report = await mod_analysis.build_report(stat_calc, character, relic, guild_id=guild_id, view=view)

    if report["error"]:
        context["error"] = report["error"]
        return templates.TemplateResponse(request, "mod_analysis.html", context)

    context["result"] = report
    context["view_urls"] = _view_urls(character, relic_raw, guild_ref)
    # Все три вида Δ/%/Итог сразу — переключатель на странице показывает нужный без запроса
    # (жалоба 2026-10-26 «страница обновляется при переключении»). Дорогая часть уже в
    # _REPORT_CACHE, повторные вызовы — только агрегация.
    views = {view: report}
    for v in mod_analysis.VIEWS:
        if v != view:
            views[v] = (await mod_analysis.build_report_live(comlink, stat_calc, character, relic, lookup, view=v)
                        if guild_ref else
                        await mod_analysis.build_report(stat_calc, character, relic, guild_id=guild_id, view=v))
    context["views"] = [views[v] for v in mod_analysis.VIEWS]

    # Не плодить дубли в "Истории" при простом переключении Δ/%/Итог (тот же GET той же
    # страницы, что и обычный пересчёт, см. _view_urls) — сравниваем с последней записью по
    # этому персонажу/гильдии, а не только по guild_id, иначе повторный клик по любому виду
    # (view) на неизменных персонаже/релике/guild_ref бесполезно двигал бы историю и вытеснял
    # реально другие прошлые запуски (запрос пользователя 2026-09-22, тот же тред).
    last = database.get_mod_analysis_history(guild_id=guild_id, limit=1)
    is_same_as_last = bool(last) and last[0][1] == character and str(last[0][2]) == str(relic) and (last[0][5] or "") == (guild_ref or "")
    if not is_same_as_last:
        database.add_mod_analysis_history(
            character, relic, user["discord_id"], guild_id=guild_id,
            guild_ref=guild_ref or None, guild_name=guild_name_for_history,
        )
    context["history"] = _history_rows(guild_id)

    return templates.TemplateResponse(request, "mod_analysis.html", context)


# ---------------- Сравнение гильдий (/mod-analysis/compare) ----------------
# services/mod_compare.py: те же персонажи в нескольких гильдиях сразу. Чужие ростеры
# собираются фоном (страница перезагружается, пока идёт сбор) и лежат в кэше сутки.

_COMPARE_STATE_KEY = "mod_compare_last"
_compare_cache: dict = {}  # ключ запроса -> (time, результат)


def _compare_params(qp) -> dict:
    guilds = [g.strip() for g in qp.getlist("guild") if g.strip()][:mod_compare.MAX_GUILDS]
    chars = list(dict.fromkeys(c for c in qp.getlist("char") if c))[:mod_compare.MAX_CHARS]
    relic_raw = qp.get("relic", "")
    relic = int(relic_raw) if relic_raw.isdigit() and 5 <= int(relic_raw) <= 10 else None
    return {"guilds": guilds, "chars": chars, "relic": relic, "own": qp.get("own") == "1"}


@router.get("/compare", response_class=HTMLResponse)
async def mod_compare_page(request: Request, user: dict = Depends(feature_flags.require_feature("mod_analysis"))):
    qp = request.query_params
    guild_id = user["guild_id"]
    params = _compare_params(qp)
    submitted = bool(params["chars"])
    if not submitted:
        # Пустая форма — подставить прошлое сравнение этой гильдии.
        try:
            saved = json.loads(database.get_bot_state(_COMPARE_STATE_KEY, guild_id=guild_id) or "{}")
        except ValueError:
            saved = {}
        params = {"guilds": saved.get("guilds", []), "chars": saved.get("chars", []),
                  "relic": saved.get("relic"), "own": saved.get("own", True)}
    context = {
        "user": user, "params": params, "max_guilds": mod_compare.MAX_GUILDS, "max_chars": mod_compare.MAX_CHARS,
        "char_labels": {c: _char_label(c) for c in params["chars"]},
        "guild_errors": {}, "guild_names": {}, "pending": [], "result": None, "loading": False,
    }
    if not submitted:
        return templates.TemplateResponse(request, "mod_compare.html", context)
    if not params["guilds"] and not params["own"]:
        context["error"] = "Укажите хотя бы одну гильдию или отметьте свою."
        return templates.TemplateResponse(request, "mod_compare.html", context)
    database.set_bot_state(_COMPARE_STATE_KEY, json.dumps(params, ensure_ascii=False), guild_id=guild_id)

    comlink = _get_comlink()
    refresh = qp.get("refresh", "")
    lookups = []
    for ref in params["guilds"]:
        lookup = await mod_compare.resolve(comlink, ref)
        if not lookup.ok:
            context["guild_errors"][ref] = lookup.error
            continue
        context["guild_names"][ref] = lookup.guild_name
        gid = lookup.swgoh_guild_id
        if mod_compare.is_fetching(gid) or not mod_compare.is_fresh(gid) or refresh == gid:
            if not mod_compare.is_fetching(gid) and (refresh == gid or not mod_compare.is_fresh(gid)):
                mod_compare.start_fetch(comlink, lookup)
            context["pending"].append(mod_compare.progress(gid))
        lookups.append(lookup)
    if refresh:
        # Не перезапускать сбор при каждой авто-перезагрузке.
        return RedirectResponse(str(request.url.remove_query_params("refresh")), status_code=303)
    if context["pending"]:
        return templates.TemplateResponse(request, "mod_compare.html", context)

    try:
        stat_calc = await stat_forecast.get_stat_calc(comlink)
    except Exception:
        context["loading"] = True
        return templates.TemplateResponse(request, "mod_compare.html", context)

    guilds = []
    if params["own"]:
        guilds.append({"name": (database.get_guild_config(guild_id) or {}).get("name") or "Своя гильдия", "own": True,
                       "roster": mod_compare.own_roster(guild_id), "info": None})
    seen = set()
    for lookup in lookups:
        if lookup.swgoh_guild_id in seen:
            continue
        seen.add(lookup.swgoh_guild_id)
        info = mod_compare.roster_info(lookup.swgoh_guild_id)
        guilds.append({"name": lookup.guild_name, "own": False, "id": lookup.swgoh_guild_id,
                       "roster": mod_compare.load_roster(lookup.swgoh_guild_id), "info": info})

    key = (guild_id, tuple(params["chars"]), params["relic"], params["own"],
           tuple((g.get("id"), (g["info"] or {}).get("fetched_at")) for g in guilds))
    cached = _compare_cache.get(key)
    if cached and time.time() - cached[0] < 15 * 60:
        result = cached[1]
    else:
        result = await asyncio.to_thread(mod_compare.compare, stat_calc, params["chars"], guilds, params["relic"])
        if len(_compare_cache) > 50:
            _compare_cache.clear()
        _compare_cache[key] = (time.time(), result)
    context["result"] = result
    context["guilds"] = guilds
    return templates.TemplateResponse(request, "mod_compare.html", context)
