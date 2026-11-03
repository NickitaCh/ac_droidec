"""All alliance mutations are scoped to the current officer's guild."""

import asyncio
from pathlib import Path
from urllib.parse import urlencode, urlsplit

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

import database
from services import alliances
from web.deps import require_officer_access

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent.parent / "templates"))


def _gid(user):
    if not user.get("guild_id"):
        raise HTTPException(403, "Выберите подключённую гильдию.")
    return int(user["guild_id"])


def _redirect(**message):
    return RedirectResponse("/alliances?" + urlencode(message), status_code=303)


@router.get("", response_class=HTMLResponse)
async def index(request: Request, user: dict = Depends(require_officer_access)):
    ctx = await asyncio.to_thread(alliances.page, _gid(user))
    return templates.TemplateResponse(request, "alliances.html", {
        "user": user, **ctx, "error": request.query_params.get("error"),
        "notice": request.query_params.get("notice"),
    })


@router.get("/shared/{share_id}", response_class=HTMLResponse)
async def shared(request: Request, share_id: int, user: dict = Depends(require_officer_access)):
    try:
        kind, snapshot = await asyncio.to_thread(alliances.preview, _gid(user), share_id)
    except alliances.AllianceError as exc:
        return _redirect(error=str(exc))
    if kind == "plate":
        from web.routes import stat_plates as plates
        for row in snapshot["requirements"]:
            row["character_name"] = database.get_game_unit_name(row["character_key"]) or row["character_key"]
            stat = row["stat_name"]
            if stat == plates.STAT_OMICRON:
                text = "Омикрон: %s — разблокирован" % plates._omicron_ability_name(row["skill_id"])
            elif stat == plates.STAT_MOD_PRIMARY:
                text = "Основа: " + plates._mod_primary_text(row["mod_slot"], row["threshold_value"], row["skill_id"])
            elif stat == plates.STAT_SET:
                text = "Сет: " + plates._mod_set_text(row["skill_id"], row["threshold_value"])
            elif stat in (plates.STAT_SET_ANY, plates.STAT_SET_ONLY):
                text = "Сеты: " + plates._set_list_text(stat, row["skill_id"])
            elif row["compare_character_key"]:
                text = "%s %s %s%s" % (plates._stat_ru(stat), row["operator"], plates._unit_name(row["compare_character_key"]), plates._compare_offset_text(row["operator"], row["threshold_value"]))
            else:
                text = "%s %s %s" % (plates._stat_ru(stat), row["operator"], plates._fmt_value(row["threshold_value"]))
            row["value_display"] = text
    elif kind in ("dc_base", "dc_focused"):
        from web.routes.datacrons import _safe_catalog
        from cogs.datacron_requirements import _level_label, DATACRON_STAT_LABELS
        catalog = await _safe_catalog()
        row = snapshot["row"]
        season = ((catalog or {}).get("seasons") or {}).get(int(row["set_id"]))
        snapshot["bonuses"] = [(level, _level_label(season.get("level%s" % level, []), row["level%s_value" % level])) for level in (3, 6, 9)] if kind == "dc_base" and season else []
        snapshot["character_name"] = database.get_game_unit_name(row.get("character_key", "")) if kind == "dc_focused" else ""
        for stat in snapshot["stats"]:
            stat["label"] = DATACRON_STAT_LABELS.get(stat["stat_id"], str(stat["stat_id"]))
    else:
        snapshot["display"] = database.get_skill_display_info([x["skill_id"] for x in snapshot["priority"]])
    return templates.TemplateResponse(request, "alliance_shared.html", {"user": user, "kind": kind, "snapshot": snapshot, "kinds": alliances.KINDS})


def _perform(gid, actor, action, form):
    number = lambda key: int(form.get(key) or 0)
    checked = lambda key: form.get(key) == "1"
    if action == "create":
        alliances.create(gid, form.get("name", ""), actor)
        return "Альянс создан. Теперь можно приглашать гильдии."
    if action == "rename":
        alliances.rename(gid, form.get("name", ""))
        return "Название сохранено."
    if action == "invite":
        alliances.invite(gid, number("target"), actor)
        return "Приглашение отправлено офицерам гильдии в вебе."
    if action in ("accept", "decline"):
        alliances.respond(gid, number("invite_id"), action == "accept")
        return "Вы вступили в альянс." if action == "accept" else "Приглашение отклонено."
    if action == "cancel":
        alliances.cancel_invite(gid, number("invite_id"))
        return "Приглашение отменено."
    if action in ("permissions", "remove", "transfer", "leave"):
        alliances.membership(gid, gid if action == "leave" else number("target"), action, checked("can_invite"), checked("can_manage"))
        return "Изменения сохранены. При выходе гильдии подключения сохраняются как независимые копии."
    if action == "share":
        kind, key = form.get("resource", "|").split("|", 1)
        alliances.share(gid, kind, key, form.getlist("recipients"), checked("allow_copy"), checked("allow_link"), checked("all_members"))
        return "Настройки обмена сохранены."
    if action == "revoke":
        alliances.revoke(gid, number("share_id"))
        return "Обмен закрыт. Полученные ранее версии остаются у гильдий; новые обновления недоступны."
    if action == "consume":
        key = alliances.consume(gid, number("share_id"), form.get("mode"), form.get("name", ""), actor, checked("replace"))
        return "Данные добавлены в настройки вашей гильдии: %s." % key
    if action == "detach":
        alliances.detach(gid, number("link_id"))
        return "Обновления отключены. Копию теперь можно редактировать."
    raise alliances.AllianceError("Неизвестное действие.")


@router.post("/{action}")
async def mutate(request: Request, action: str, user: dict = Depends(require_officer_access)):
    origin = request.headers.get("origin")
    if origin and urlsplit(origin).netloc != request.url.netloc:
        raise HTTPException(403, "Запрос должен быть отправлен с этого сайта.")
    form = await request.form()
    try:
        message = await asyncio.to_thread(_perform, _gid(user), str(user["discord_id"]), action, form)
    except (alliances.AllianceError, ValueError) as exc:
        return _redirect(error=str(exc))
    return _redirect(notice=message)
