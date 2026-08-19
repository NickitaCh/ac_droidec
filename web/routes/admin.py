from pathlib import Path
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

import database
from command_catalog import COMMAND_GROUPS
from services.guild_admin import (
    add_grant,
    add_guild,
    add_super_admin,
    deactivate_guild,
    list_admins,
    list_guilds,
    remove_grant,
    remove_super_admin,
)
from web.deps import require_super_admin

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent.parent / "templates"))


def _get_comlink():
    # См. web/routes/registration.py::_get_comlink — веб-процесс не поднимает
    # диска-клиента, строит свой SwgohComlink на тот же comlink-сайдкар.
    from swgoh_comlink import SwgohComlink
    return SwgohComlink(url="http://localhost:3000")


@router.get("/guilds", response_class=HTMLResponse)
async def guilds_page(request: Request, user: dict = Depends(require_super_admin)):
    return templates.TemplateResponse(request, "admin_guilds.html", {
        "user": user,
        "guilds": list_guilds(active_only=False),
        "error": request.query_params.get("error"),
    })


@router.post("/guilds/add", response_class=HTMLResponse)
async def guilds_add(
    request: Request,
    ally_code: str = Form(...),
    discord_guild_id: str = Form(...),
    user: dict = Depends(require_super_admin),
):
    comlink = _get_comlink()
    result = await add_guild(comlink, ally_code, discord_guild_id)
    if not result.ok:
        return RedirectResponse(f"/admin/guilds?{urlencode({'error': result.error})}", status_code=303)
    return RedirectResponse("/admin/guilds", status_code=303)


@router.post("/guilds/{guild_id}/deactivate", response_class=HTMLResponse)
async def guilds_deactivate(guild_id: int, user: dict = Depends(require_super_admin)):
    deactivate_guild(guild_id)
    return RedirectResponse("/admin/guilds", status_code=303)


@router.get("/access", response_class=HTMLResponse)
async def access_page(request: Request, user: dict = Depends(require_super_admin)):
    data = list_admins()
    # Голые discord_id/guild_id нечитаемы для человека — резолвим в имена здесь,
    # а не в шаблоне (Jinja без доступа к БД). guild_names — по ВСЕМ гильдиям
    # (не только активным), т.к. грант может указывать на уже деактивированную.
    guild_names = {g["id"]: g["name"] for g in list_guilds(active_only=False)}
    super_admins = [
        {**a, "added_by_name": database.get_username_for_discord_id(a["added_by"])}
        for a in data["super_admins"]
    ]
    grants = [
        {
            **g,
            "granted_by_name": database.get_username_for_discord_id(g["granted_by"]),
            "guild_name": guild_names.get(g["guild_id"]),
        }
        for g in data["grants"]
    ]
    return templates.TemplateResponse(request, "admin_access.html", {
        "user": user,
        "super_admins": super_admins,
        "grants": grants,
        "guilds": list_guilds(active_only=True),
        "error": request.query_params.get("error"),
    })


@router.post("/access/super-admins/add", response_class=HTMLResponse)
async def super_admins_add(
    discord_id: str = Form(...),
    username: str = Form(""),
    user: dict = Depends(require_super_admin),
):
    add_super_admin(discord_id, username or None, user["discord_id"])
    return RedirectResponse("/admin/access", status_code=303)


@router.post("/access/super-admins/{discord_id}/remove", response_class=HTMLResponse)
async def super_admins_remove(discord_id: str, user: dict = Depends(require_super_admin)):
    remove_super_admin(discord_id)
    return RedirectResponse("/admin/access", status_code=303)


@router.post("/access/grants/add", response_class=HTMLResponse)
async def grants_add(
    request: Request,
    discord_id: str = Form(...),
    ally_code: str = Form(...),
    tier: str = Form(...),
    guild_id: int = Form(...),
    user: dict = Depends(require_super_admin),
):
    comlink = _get_comlink()
    result = await add_grant(comlink, discord_id, ally_code, guild_id, tier, user["discord_id"])
    if not result.ok:
        return RedirectResponse(f"/admin/access?{urlencode({'error': result.error})}", status_code=303)
    return RedirectResponse("/admin/access", status_code=303)


@router.post("/access/grants/{discord_id}/remove", response_class=HTMLResponse)
async def grants_remove(discord_id: str, user: dict = Depends(require_super_admin)):
    remove_grant(discord_id)
    return RedirectResponse("/admin/access", status_code=303)


@router.get("/access-log", response_class=HTMLResponse)
async def access_log_page(request: Request, user: dict = Depends(require_super_admin)):
    return templates.TemplateResponse(request, "admin_access_log.html", {
        "user": user,
        "entries": database.get_web_access_log(limit=200),
    })


@router.get("/command-usage", response_class=HTMLResponse)
async def command_usage_page(request: Request, user: dict = Depends(require_super_admin)):
    usage = database.get_command_usage_counts()
    known_names = {name for _, cmds in COMMAND_GROUPS for name, _ in cmds}
    groups = [
        {
            "title": title,
            "rows": [
                {
                    "name": name,
                    "description": description,
                    "count": usage.get(name, {}).get("count", 0),
                    "last_used_at": usage.get(name, {}).get("last_used_at"),
                }
                for name, description in cmds
            ],
        }
        for title, cmds in COMMAND_GROUPS
    ]
    # Команды, вызванные ботом, но отсутствующие в каталоге (забыли добавить в
    # command_catalog.py при новой команде, либо команда с тех пор удалена) —
    # отдельной группой в конце, чтобы расхождение было видно, а не потеряно.
    unknown_rows = [
        {"name": name, "description": None, "count": data["count"], "last_used_at": data["last_used_at"]}
        for name, data in usage.items()
        if name not in known_names
    ]
    if unknown_rows:
        groups.append({"title": "Не в каталоге (проверьте command_catalog.py)", "rows": unknown_rows})
    return templates.TemplateResponse(request, "admin_command_usage.html", {
        "user": user,
        "groups": groups,
        "total_calls": sum(r["count"] for g in groups for r in g["rows"]),
    })


# =====================================================================
# ФРАЗЫ-ПРИПИСКИ ДЛЯ АВТООБЪЯВЛЕНИЙ О ВЫДАЧЕ ОМИКРОНОВ
# Справочник глобальный (не per-guild, см. database.py::_ensure_omicron_phrases_table),
# поэтому — как /admin/guilds и /admin/access — управляется супер-админами, а не
# гильдийскими офицерами (в отличие от /plates, /datacrons и т.п.). Сама детекция
# выдачи и отправка сообщения в Discord — cogs/stat_requirements.py::_announce_omicrons,
# эта страница только редактирует соответствия персонаж → фраза, ничего не постит.
# =====================================================================
@router.get("/omicron-phrases", response_class=HTMLResponse)
async def omicron_phrases_page(request: Request, user: dict = Depends(require_super_admin)):
    rows = database.get_all_omicron_phrases()
    phrases = [
        {
            "character_key": character_key,
            "character_name": database.get_game_unit_name(character_key) or character_key,
            "phrase": phrase,
            "updated_by_name": database.get_username_for_discord_id(updated_by) if updated_by else None,
            "updated_at": updated_at,
        }
        for _id, character_key, phrase, updated_by, updated_at in rows
    ]
    phrases.sort(key=lambda p: p["character_name"].lower())
    return templates.TemplateResponse(request, "admin_omicron_phrases.html", {
        "user": user,
        "phrases": phrases,
        "error": request.query_params.get("error"),
    })


@router.get("/omicron-phrases/api/units", response_class=JSONResponse)
async def omicron_phrases_units_search(q: str = "", user: dict = Depends(require_super_admin)):
    # Только юниты, у которых омикрон реально существует (кто-то из гильдии уже
    # разблокировал способность на омикрон-тире) — как в автокомплите /омикрон_текст
    # фраза (cogs/stat_requirements.py::autocomplete_omicron_capable_character), а не
    # весь справочник game_units, где большинство персонажей омикрона не имеют вовсе.
    if not q or len(q.strip()) < 2:
        return []
    query = q.strip().lower()
    capable_ids = database.get_omicron_capable_base_ids()
    names = database.get_game_unit_names(list(capable_ids))
    matches = [
        {"base_id": base_id, "name": name}
        for base_id, name in names.items()
        if query in name.lower()
    ]
    matches.sort(key=lambda m: m["name"].lower())
    return matches[:20]


@router.post("/omicron-phrases/add", response_class=HTMLResponse)
async def omicron_phrases_add(
    character_key: str = Form(...),
    phrase: str = Form(...),
    user: dict = Depends(require_super_admin),
):
    character_key = character_key.strip()
    phrase = phrase.strip()
    if not character_key or not phrase:
        return RedirectResponse(f"/admin/omicron-phrases?{urlencode({'error': 'Персонаж и текст фразы обязательны.'})}", status_code=303)
    database.set_omicron_phrase(character_key, phrase, user["discord_id"])
    return RedirectResponse("/admin/omicron-phrases", status_code=303)


@router.post("/omicron-phrases/{character_key}/edit", response_class=HTMLResponse)
async def omicron_phrases_edit(character_key: str, phrase: str = Form(...), user: dict = Depends(require_super_admin)):
    phrase = phrase.strip()
    if not phrase:
        return RedirectResponse(f"/admin/omicron-phrases?{urlencode({'error': 'Текст фразы не может быть пустым.'})}", status_code=303)
    database.set_omicron_phrase(character_key, phrase, user["discord_id"])
    return RedirectResponse("/admin/omicron-phrases", status_code=303)


@router.post("/omicron-phrases/{character_key}/delete", response_class=HTMLResponse)
async def omicron_phrases_delete(character_key: str, user: dict = Depends(require_super_admin)):
    database.delete_omicron_phrase(character_key)
    return RedirectResponse("/admin/omicron-phrases", status_code=303)
