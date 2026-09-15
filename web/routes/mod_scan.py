"""Веб-страница /mod-scan — управление целями скана моддинга чужих гильдий
(services/mod_scan.py::resolve_target/scan_target делают всю работу; фоновый прогон —
cogs/mod_scan.py) и просмотр двух списков событий (минорные изменения / аномалии).

Доступ ограничен guild_id=1 (AbsoluteChaos) — по запросу пользователя (чат, 2026-09-15:
"доступ ... оставь сейчас только для абсолют хаос"), пока не появится полноценная система
доступа "фича для выбранных гильдий" (см. project_permission_model_comlink_rank в
памяти). Это НЕ feature_flags.require_feature (opt-out модель: включено всем по
умолчанию) — здесь наоборот, opt-in ровно для одной гильдии, поэтому проверка захардкожена
прямо в роуте, а не через общий тумблер /admin/features."""

from pathlib import Path
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

import database
from services import mod_scan
from web.deps import require_officer_access

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent.parent / "templates"))

RESTRICTED_TO_GUILD_ID = 1
# Как на /activity (web/routes/guild_dashboard.py) — умеренный размер страницы, чтобы
# и на телефоне таблица не превращалась в простыню, и лишних кликов "дальше" не было.
EVENTS_PAGE_SIZE = 25


def require_mod_scan_access(user: dict = Depends(require_officer_access)) -> dict:
    if user.get("guild_id") != RESTRICTED_TO_GUILD_ID:
        raise HTTPException(status_code=403, detail="Функция пока доступна только для AbsoluteChaos.")
    return user


def _get_comlink():
    # Как в web/routes/steal_build.py/mod_optimizer.py — веб-процесс строит свой клиент
    # поверх того же comlink-сайдкара, не поднимая main.py/bot.
    from swgoh_comlink import SwgohComlink
    return SwgohComlink(url="http://localhost:3000")


def _with_character_labels(events: list[dict]) -> list[dict]:
    # base_id — сырой id ("GRANDMASTERYODA") — по фидбеку в памяти всегда показывать
    # имя, если оно резолвится, а не голый id (см. database.get_game_unit_name, тот же
    # хелпер, что web/routes/steal_build.py::_char_label).
    for e in events:
        e["character_label"] = database.get_game_unit_name(e["base_id"]) or e["base_id"]
    return events


@router.get("/api/units", response_class=JSONResponse)
async def mod_scan_units_search(q: str = "", user: dict = Depends(require_mod_scan_access)):
    # Тот же приём, что web/routes/stat_plates.py::units_search и
    # web/routes/guild_dashboard.py::tb_platoons_units_search — свой эндпоинт, а не чужой
    # /plates/api/units, чтобы фильтр по персонажу тут не зависел от чужого feature-флага
    # (mod-scan захардкожен на guild_id=1 отдельной проверкой, см. докстринг выше).
    if not q or len(q.strip()) < 2:
        return []
    rows = database.search_game_units(q.strip(), limit=20)
    return [{"base_id": base_id, "name": name} for base_id, name in rows]


def _paginate_events(kind: str, base_ids: list[str] | None, page_param: str) -> dict:
    try:
        page = max(1, int(page_param or "1"))
    except ValueError:
        page = 1
    rows, total = database.get_mod_scan_events(
        owner_guild_id=RESTRICTED_TO_GUILD_ID, kind=kind,
        limit=EVENTS_PAGE_SIZE, offset=(page - 1) * EVENTS_PAGE_SIZE, base_ids=base_ids,
    )
    total_pages = max(1, -(-total // EVENTS_PAGE_SIZE))  # ceil div
    page = min(page, total_pages)
    return {"events": _with_character_labels(rows), "page": page, "total_pages": total_pages, "total": total}


@router.get("", response_class=HTMLResponse)
async def mod_scan_page(request: Request, user: dict = Depends(require_mod_scan_access)):
    qp = request.query_params
    character = (qp.get("character") or "").strip()
    character_label = database.get_game_unit_name(character) if character else ""
    base_ids = [character] if character else None

    minor = _paginate_events("minor", base_ids, qp.get("minor_page"))
    anomaly = _paginate_events("anomaly", base_ids, qp.get("anomaly_page"))

    def _page_url(page_param: str, page: int) -> str:
        params = {"character": character} if character else {}
        params[page_param] = page
        return f"/mod-scan?{urlencode(params)}"

    context = {
        "user": user,
        "error": request.query_params.get("error"),
        "targets": database.get_mod_scan_targets(owner_guild_id=RESTRICTED_TO_GUILD_ID),
        "minor_events": minor["events"],
        "minor_page": minor["page"],
        "minor_total_pages": minor["total_pages"],
        "minor_total": minor["total"],
        "minor_prev_url": _page_url("minor_page", minor["page"] - 1) if minor["page"] > 1 else None,
        "minor_next_url": _page_url("minor_page", minor["page"] + 1) if minor["page"] < minor["total_pages"] else None,
        "anomaly_events": anomaly["events"],
        "anomaly_page": anomaly["page"],
        "anomaly_total_pages": anomaly["total_pages"],
        "anomaly_total": anomaly["total"],
        "anomaly_prev_url": _page_url("anomaly_page", anomaly["page"] - 1) if anomaly["page"] > 1 else None,
        "anomaly_next_url": _page_url("anomaly_page", anomaly["page"] + 1) if anomaly["page"] < anomaly["total_pages"] else None,
        "character": character,
        "character_label": character_label,
        "max_targets": database.MOD_SCAN_MAX_TARGETS,
    }
    return templates.TemplateResponse(request, "mod_scan.html", context)


@router.post("/targets", response_class=HTMLResponse)
async def add_target(request: Request, user: dict = Depends(require_mod_scan_access)):
    form = await request.form()
    input_value = (form.get("input_value") or "").strip()
    if not input_value:
        return RedirectResponse(f"/mod-scan?{urlencode({'error': 'Укажите аликод или ID гильдии'})}", status_code=303)

    if database.count_mod_scan_targets(owner_guild_id=RESTRICTED_TO_GUILD_ID) >= database.MOD_SCAN_MAX_TARGETS:
        error = f"Максимум {database.MOD_SCAN_MAX_TARGETS} целей"
        return RedirectResponse(f"/mod-scan?{urlencode({'error': error})}", status_code=303)

    import guild_resolver
    ally_code = guild_resolver.normalize_ally_code(input_value)
    input_kind = "ally_code" if ally_code else "guild"

    comlink = _get_comlink()
    lookup = await mod_scan.resolve_target(comlink, input_value)
    if not lookup.ok:
        return RedirectResponse(f"/mod-scan?{urlencode({'error': lookup.error})}", status_code=303)

    database.create_mod_scan_target(
        input_kind=input_kind,
        input_value=input_value,
        swgoh_guild_id=lookup.swgoh_guild_id,
        guild_name=lookup.guild_name,
        member_count=len(lookup.members),
        owner_guild_id=RESTRICTED_TO_GUILD_ID,
    )
    return RedirectResponse("/mod-scan", status_code=303)


@router.post("/targets/{target_id}/delete", response_class=HTMLResponse)
async def delete_target(target_id: int, user: dict = Depends(require_mod_scan_access)):
    database.delete_mod_scan_target(target_id, owner_guild_id=RESTRICTED_TO_GUILD_ID)
    return RedirectResponse("/mod-scan", status_code=303)
