from pathlib import Path
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

import database
from command_catalog import COMMAND_GROUPS
from services.units_sync import sync_units
from services.guild_admin import (
    add_grant,
    add_guild,
    add_super_admin,
    add_web_credential,
    deactivate_guild,
    list_admins,
    list_guilds,
    list_web_credentials,
    remove_grant,
    remove_super_admin,
    remove_web_credential,
    set_web_credential_password,
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


# =====================================================================
# Переключатель "войти как гильдия N" в шапке (base.html, только для
# супер-админов) — см. web/deps.py::_apply_guild_switch за тем, как это влияет
# на guild_id/tier во всех остальных роутах. Здесь только пишем/чистим
# session["impersonate_guild_id"] и возвращаем на ту же страницу.
# =====================================================================
@router.post("/switch-guild", response_class=RedirectResponse)
async def switch_guild(request: Request, guild_id: str = Form(...), user: dict = Depends(require_super_admin)):
    if guild_id == "__self__":
        request.session.pop("impersonate_guild_id", None)
    else:
        try:
            gid = int(guild_id)
        except ValueError:
            gid = None
        if gid is None or not any(g["id"] == gid for g in list_guilds(active_only=True)):
            return RedirectResponse(f"/admin/guilds?{urlencode({'error': 'Некорректная гильдия.'})}", status_code=303)
        request.session["impersonate_guild_id"] = gid
    referer = request.headers.get("referer") or "/"
    return RedirectResponse(referer, status_code=303)


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


@router.get("/web-accounts", response_class=HTMLResponse)
async def web_accounts_page(request: Request, user: dict = Depends(require_super_admin)):
    # Голый discord_id нечитаем — резолвим в лучшее известное имя тем же
    # способом, что и остальные /admin-страницы (database.get_username_for_discord_id).
    accounts = [
        {
            **a,
            "player_name": database.get_username_for_discord_id(a["discord_id"]),
            "created_by_name": database.get_username_for_discord_id(a["created_by"]) if a["created_by"] else None,
        }
        for a in list_web_credentials()
    ]
    return templates.TemplateResponse(request, "admin_web_accounts.html", {
        "user": user,
        "accounts": accounts,
        "error": request.query_params.get("error"),
    })


@router.post("/web-accounts/add", response_class=HTMLResponse)
async def web_accounts_add(
    login: str = Form(...),
    discord_id: str = Form(...),
    password: str = Form(...),
    user: dict = Depends(require_super_admin),
):
    result = add_web_credential(login, discord_id, password, user["discord_id"])
    if not result.ok:
        return RedirectResponse(f"/admin/web-accounts?{urlencode({'error': result.error})}", status_code=303)
    return RedirectResponse("/admin/web-accounts", status_code=303)


@router.post("/web-accounts/{login}/password", response_class=HTMLResponse)
async def web_accounts_set_password(login: str, password: str = Form(...), user: dict = Depends(require_super_admin)):
    result = set_web_credential_password(login, password)
    if not result.ok:
        return RedirectResponse(f"/admin/web-accounts?{urlencode({'error': result.error})}", status_code=303)
    return RedirectResponse("/admin/web-accounts", status_code=303)


@router.post("/web-accounts/{login}/delete", response_class=HTMLResponse)
async def web_accounts_delete(login: str, user: dict = Depends(require_super_admin)):
    remove_web_credential(login)
    return RedirectResponse("/admin/web-accounts", status_code=303)


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
# эта страница только редактирует соответствия персонаж(+омикрон) → фраза, ничего не постит.
#
# 2026-08-31: раньше страница была формой добавления + таблицей уже заданных фраз (нужно
# было заранее знать персонажа). Переделано в один сплошной список ВСЕХ персонажей, у
# которых В ИГРЕ есть омикрон (database.get_all_unit_omicron_skills, тот же источник, что
# game_units.has_omicron) — сразу видно, у кого фраза уже задана, а у кого нет, без
# отдельного поиска/формы. У персонажа с несколькими омикронами (isOmicronTier на
# нескольких способностях) первая строка — фраза "по умолчанию" (skill_id='', используется
# для любого его омикрона, пока не переопределена), плюс по строке на каждый конкретный
# омикрон с собственным override — см. database.get_omicron_phrase за порядком резолва.
# =====================================================================
def _omicron_phrase_status(own_phrase: str, default_phrase: str, is_override_row: bool) -> tuple:
    if own_phrase:
        return ("badge-ok", "✅ своя" if is_override_row else "✅ задана")
    if is_override_row and default_phrase:
        return ("badge-neutral", "↳ по умолчанию")
    return ("badge-neutral", "— не задана")


# Порядок режимов в выпадающем фильтре — сперва самые актуальные для гильдии (ВГ/ТБ/ВА/рейд),
# остальные (см. services/units_sync.py::_OMICRON_MODE_LABELS) — по алфавиту следом.
_OMICRON_MODE_FILTER_ORDER = ["ВГ", "ТБ", "ВА", "рейд", "арена", "PvE", "PvE-ивент", "покорение", "галактический вызов", "все режимы"]


def _sort_modes(modes) -> list:
    order = {m: i for i, m in enumerate(_OMICRON_MODE_FILTER_ORDER)}
    return sorted(modes, key=lambda m: (order.get(m, len(order)), m))


@router.get("/omicron-phrases", response_class=HTMLResponse)
async def omicron_phrases_page(request: Request, user: dict = Depends(require_super_admin)):
    skills_by_base = database.get_all_unit_omicron_skills()
    skill_info = database.get_skill_display_info([sid for sids in skills_by_base.values() for sid in sids])
    names = database.get_game_unit_names(list(skills_by_base.keys()))

    phrase_map = {(r[1], r[2]): r for r in database.get_all_omicron_phrases()}  # (character_key, skill_id) -> row
    username_cache = {}

    def _updated_by_name(discord_id):
        if not discord_id:
            return None
        if discord_id not in username_cache:
            username_cache[discord_id] = database.get_username_for_discord_id(discord_id)
        return username_cache[discord_id]

    characters = []
    all_modes = set()
    for base_id, skill_ids in skills_by_base.items():
        name = names.get(base_id) or base_id
        default_row = phrase_map.get((base_id, ""))
        default_phrase = default_row[3] if default_row else ""

        omicrons = []
        char_modes = set()
        for skill_id in skill_ids:
            ability_name, _ability_id, ability_type, omicron_mode = skill_info.get(skill_id, (None, None, None, None))
            extra = " / ".join(p for p in (ability_type, omicron_mode) if p)
            row = phrase_map.get((base_id, skill_id))
            own_phrase = row[3] if row else ""
            badge_class, badge_text = _omicron_phrase_status(own_phrase, default_phrase, is_override_row=True)
            if omicron_mode:
                char_modes.add(omicron_mode)
                all_modes.add(omicron_mode)
            omicrons.append({
                "skill_id": skill_id,
                "label": ability_name or skill_id,
                "extra": extra,
                "mode": omicron_mode or "",
                "phrase": own_phrase,
                "updated_by_name": _updated_by_name(row[4]) if row else None,
                "badge_class": badge_class,
                "badge_text": badge_text,
            })
        omicrons.sort(key=lambda o: o["label"].lower())

        default_badge_class, default_badge_text = _omicron_phrase_status(default_phrase, default_phrase, is_override_row=False)
        characters.append({
            "base_id": base_id,
            "name": name,
            "default_phrase": default_phrase,
            "default_updated_by_name": _updated_by_name(default_row[4]) if default_row else None,
            "default_badge_class": default_badge_class,
            "default_badge_text": default_badge_text,
            "omicrons": omicrons,
            "modes": " ".join(_sort_modes(char_modes)),
            "multi": len(omicrons) > 1,
        })
    characters.sort(key=lambda c: c["name"].lower())

    return templates.TemplateResponse(request, "admin_omicron_phrases.html", {
        "user": user,
        "characters": characters,
        "modes": _sort_modes(all_modes),
        "error": request.query_params.get("error"),
        "synced": request.query_params.get("synced"),
    })


@router.post("/omicron-phrases/set", response_class=HTMLResponse)
async def omicron_phrases_set(
    character_key: str = Form(...),
    skill_id: str = Form(""),
    phrase: str = Form(""),
    user: dict = Depends(require_super_admin),
):
    character_key = character_key.strip()
    skill_id = skill_id.strip()
    phrase = phrase.strip()
    if not character_key:
        return RedirectResponse(f"/admin/omicron-phrases?{urlencode({'error': 'Некорректный персонаж.'})}", status_code=303)
    # Пустое поле = убрать фразу (единая точка входа вместо отдельных add/edit/delete —
    # так и default-строка, и override на конкретный омикрон правятся одной формой).
    if phrase:
        database.set_omicron_phrase(character_key, phrase, user["discord_id"], skill_id=skill_id)
    else:
        database.delete_omicron_phrase(character_key, skill_id=skill_id)
    return RedirectResponse("/admin/omicron-phrases", status_code=303)


@router.post("/omicron-phrases/sync", response_class=HTMLResponse)
async def omicron_phrases_sync(user: dict = Depends(require_super_admin)):
    # Тот же sync_units, что кнопка "Обновить справочник юнитов" на /tasks (см.
    # web/routes/tasks.py::sync_units_now) — он и так раз в час обновляет справочник
    # омикронов (services/units_sync.py::sync_units), кнопка здесь просто даёт запустить
    # его вручную сразу, не дожидаясь цикла, если появился новый персонаж/омикрон.
    comlink = _get_comlink()
    try:
        total = await sync_units(comlink)
    except Exception as e:
        return RedirectResponse(f"/admin/omicron-phrases?{urlencode({'error': f'Ошибка синхронизации: {e}'})}", status_code=303)
    return RedirectResponse(f"/admin/omicron-phrases?{urlencode({'synced': str(total)})}", status_code=303)
