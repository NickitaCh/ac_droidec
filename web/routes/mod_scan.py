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
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

import database
from services import mod_scan
from web.deps import require_officer_access

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent.parent / "templates"))

RESTRICTED_TO_GUILD_ID = 1


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


@router.get("", response_class=HTMLResponse)
async def mod_scan_page(request: Request, user: dict = Depends(require_mod_scan_access)):
    context = {
        "user": user,
        "error": request.query_params.get("error"),
        "targets": database.get_mod_scan_targets(owner_guild_id=RESTRICTED_TO_GUILD_ID),
        "minor_events": _with_character_labels(database.get_mod_scan_events(owner_guild_id=RESTRICTED_TO_GUILD_ID, kind="minor")),
        "anomaly_events": _with_character_labels(database.get_mod_scan_events(owner_guild_id=RESTRICTED_TO_GUILD_ID, kind="anomaly")),
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
