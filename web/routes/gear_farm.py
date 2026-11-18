"""Фарм деталей по картинке C3PO — веб-версия бывшей Discord-команды /фарм (убрана
2026-10-14). Картинку можно загрузить файлом или дать ссылку на сообщение C3PO в Discord.
Доступно офицерам AbsoluteChaos в TEST. Распознавание идёт до пары минут, поэтому в фоне: статус и результат — в bot_state
(ключ на пользователя), страница обновляется сама. Логика — services/gear_farm.py."""

import asyncio
import html
import json
import os
import re
import secrets
from datetime import datetime, timezone
from pathlib import Path

import httpx
from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from markupsafe import Markup

import database
from services import farm_inventory, game_account, game_account_readonly, gear_farm, journey_guide
from web.deps import require_member_access, require_officer_access
from web.routes.game_account import connection_prompt

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent.parent / "templates"))

DISCORD_API = "https://discord.com/api/v10"
MESSAGE_LINK_RE = re.compile(r"discord(?:app)?\.com/channels/(?:\d+|@me)/(\d+)/(\d+)")
MAX_IMAGE_BYTES = 8 * 1024 * 1024
WARNING_RATIO = 0.9
_BACKGROUND_TASKS = set()


def require_farm_access(user: dict = Depends(require_officer_access)) -> dict:
    try:
        game_account.require_pilot(
            user["discord_id"], user.get("guild_id"), user.get("bot_profile"), user.get("tier"),
        )
    except game_account.AccountError as error:
        raise HTTPException(status_code=403, detail=str(error)) from error
    return user


class FarmError(Exception):
    pass


def require_inventory_access(user: dict = Depends(require_member_access)) -> dict:
    try:
        game_account.require_account_read(user["discord_id"], user["guild_id"], user["bot_profile"], user["tier"])
    except game_account.AccountError as error:
        raise HTTPException(403, str(error)) from None
    return user


def _job_key(user: dict) -> str:
    return f"gear_farm_job:{user['discord_id']}"


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _get_job(user: dict) -> dict | None:
    raw = database.get_bot_state(_job_key(user), guild_id=0)
    try:
        return json.loads(raw) if raw else None
    except ValueError:
        return None


def _set_job(user: dict, job: dict) -> None:
    database.set_bot_state(_job_key(user), json.dumps({**job, "updated_at": _now()}, ensure_ascii=False), guild_id=0)


def _render_text(text: str) -> Markup:
    """**жирный** → <strong>, [[eq:ID]] → иконка детали, остальное экранируется."""
    escaped = html.escape(text)
    escaped = re.sub(r"\[\[eq:([A-Za-z0-9_]{1,64})\]\]",
                     r'<img class="equip-icon" src="/equip-img/\1?v=2" alt="" loading="lazy" width="32" height="32">', escaped)
    return Markup(re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", escaped).replace("\n", "<br>"))


async def _image_from_link(link: str) -> tuple[bytes, str]:
    m = MESSAGE_LINK_RE.search(link or "")
    if not m:
        raise FarmError("Нужна прямая ссылка на сообщение (ПКМ по сообщению C3PO → «Копировать ссылку»).")
    token = os.environ.get("DISCORD_TOKEN")
    if not token:
        raise FarmError("DISCORD_TOKEN не настроен на сервере.")
    headers = {"Authorization": f"Bot {token}", "User-Agent": "DiscordBot (https://swgoh-sng.ru, 1.0)"}
    async with httpx.AsyncClient(headers=headers, timeout=30) as client:
        resp = await client.get(f"{DISCORD_API}/channels/{m.group(1)}/messages/{m.group(2)}")
        if resp.status_code in (403, 404):
            raise FarmError("Бот не видит это сообщение — нет доступа к каналу или сообщение удалено.")
        resp.raise_for_status()
        image = next((a for a in resp.json().get("attachments") or []
                      if (a.get("content_type") or "").startswith("image/")), None)
        if image is None:
            raise FarmError("В этом сообщении нет картинки.")
        img = await client.get(image["url"])
        img.raise_for_status()
        return img.content, (image.get("content_type") or "image/png").split(";")[0].strip()


async def _run(user: dict, image: bytes | None, mime: str | None, link: str) -> None:
    from services.openrouter_vision import call_vision_json
    try:
        if image is None:
            image, mime = await _image_from_link(link)
        data = await asyncio.to_thread(call_vision_json, image, mime, os.getenv("OPENROUTER_API_KEY"),
                                       gear_farm.PROMPT, gear_farm.validate_response)
        ok, text = gear_farm.build_plan_text(data)
        _set_job(user, {"state": "done" if ok else "error", "message": text})
    except FarmError as e:
        _set_job(user, {"state": "error", "message": str(e)})
    except Exception as e:
        _set_job(user, {"state": "error", "message": f"Ошибка распознавания: {e}"})


def _render_farm(request, user, inventory_account, *, inventory_result=None, inventory_error=None, retry=None):
    job = _get_job(user)
    if inventory_account:
        request.session.setdefault("farm_inventory_csrf", secrets.token_urlsafe(32))
    return templates.TemplateResponse(request, "gear_farm.html", {
        "user": user,
        "job": job,
        "result_html": _render_text(job["message"]) if job and job.get("message") else None,
        "error": request.query_params.get("error"),
        "inventory_csrf": request.session.get("farm_inventory_csrf") if inventory_account else None,
        "guide_options": journey_guide.guide_options() if inventory_account else (),
        "inventory_result": inventory_result,
        "inventory_error": inventory_error,
        "inventory_retry": retry,
        "inventory_link": not inventory_account and bool(game_account_readonly.own_main(user, connected_only=False, member_read=True)),
        "image_farm_allowed": user["guild_id"] == 1 and user["tier"] == "officer",
    }, headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"})


@router.get("", response_class=HTMLResponse)
async def farm_page(request: Request, user: dict = Depends(require_inventory_access)):
    return _render_farm(request, user, game_account_readonly.own_main(user, member_read=True))


def _validate_inventory_form(request, user, csrf):
    code = game_account_readonly.own_main(user, member_read=True)
    expected = request.session.get("farm_inventory_csrf")
    if not code or not expected or not secrets.compare_digest(csrf.encode(), expected.encode()):
        raise HTTPException(status_code=403)
    return code


async def _plan(request, user, code, targets=None, *, guide_id=None, requirements=None, reconnect=False):
    scope = (str(user["discord_id"]), user["guild_id"], user["bot_profile"], user["tier"], code)
    try:
        if reconnect:
            await game_account.reconnect(*scope, optimizer_read=True)
        snapshot = await game_account_readonly.read(*scope, include_supplies=True, member_read=True)
        result = await asyncio.to_thread(
            farm_inventory.build_report, snapshot, targets, guide_id=guide_id, requirements=requirements,
        )
    except game_account.ConnectionRequired as error:
        retry = connection_prompt(user, code, error, reconnect=reconnect)
        if guide_id:
            retry.update(path="/farm/guide", fields={"guide_id": guide_id})
        else:
            base, kind, target = targets[0]
            fields = {"base_id": base, "kind": kind, "target": target}
            if len(targets) > 1:
                base, kind, target = targets[1]
                fields.update(extra_base_id=base, extra_kind=kind, extra_target=target)
            retry.update(path="/farm/inventory", fields=fields)
        return _render_farm(request, user, code, retry=retry)
    except game_account.AccountError as error:
        return _render_farm(request, user, code, inventory_error=str(error))

    return _render_farm(request, user, code, inventory_result=result)


@router.post("/guide", response_class=HTMLResponse)
async def farm_guide(request: Request, guide_id: str = Form(..., max_length=64), csrf: str = Form(...),
                     reconnect: bool = Form(False),
                     user: dict = Depends(require_inventory_access)):
    code = _validate_inventory_form(request, user, csrf)
    requirements = database.get_journey_guide_requirements(guide_id)
    if not requirements:
        return _render_farm(request, user, code, inventory_error="В справочнике нет требований для выбранной цели.")
    return await _plan(request, user, code, guide_id=guide_id, requirements=requirements, reconnect=reconnect)


@router.post("/inventory", response_class=HTMLResponse)
async def farm_inventory(request: Request, base_id: str = Form(..., max_length=64),
                         kind: str = Form(...), target: int = Form(...), csrf: str = Form(...),
                         extra_base_id: str = Form("", max_length=64),
                         extra_kind: str = Form("gear"), extra_target: int = Form(13),
                         reconnect: bool = Form(False),
                         user: dict = Depends(require_inventory_access)):
    code = _validate_inventory_form(request, user, csrf)
    targets = [(base_id, kind, target)]
    if extra_base_id:
        if extra_base_id == base_id:
            return _render_farm(request, user, code, inventory_error="Выберите двух разных персонажей.")
        targets.append((extra_base_id, extra_kind, extra_target))
    if any(mode not in ("gear", "relic") or not 1 <= level <= (13 if mode == "gear" else 10)
           for _, mode, level in targets):
        return _render_farm(request, user, code, inventory_error="Выберите цель от G1 до G13 или от R1 до R10.")
    return await _plan(request, user, code, targets, reconnect=reconnect)


@router.post("", response_class=HTMLResponse)
async def farm_start(
    link: str = Form(default=""),
    image: UploadFile | None = File(default=None),
    user: dict = Depends(require_farm_access),
):
    if not user.get("is_super_admin"):
        raise HTTPException(status_code=403, detail="Распознавание C3PO доступно только суперадминам.")

    from urllib.parse import urlencode
    from services.openrouter_vision import daily_used_ratio, OPENROUTER_DAILY_REQUEST_LIMIT

    def fail(msg):
        return RedirectResponse(f"/farm?{urlencode({'error': msg})}", status_code=303)

    if not os.getenv("OPENROUTER_API_KEY"):
        return fail("OPENROUTER_API_KEY не настроен на сервере.")
    if daily_used_ratio(OPENROUTER_DAILY_REQUEST_LIMIT) >= WARNING_RATIO:
        return fail("Почти исчерпан дневной лимит распознавания картинок (~90%) — попробуйте завтра.")
    job = _get_job(user)
    if job and job.get("state") == "running":
        return fail("Распознавание уже идёт — дождитесь результата.")

    data, mime = None, None
    if image is not None and image.filename:
        data = await image.read(MAX_IMAGE_BYTES + 1)
        if len(data) > MAX_IMAGE_BYTES:
            return fail("Картинка больше 8 МБ.")
        mime = (image.content_type or "image/png").split(";")[0].strip()
        if not mime.startswith("image/"):
            return fail("Это не картинка.")
    elif not link.strip():
        return fail("Загрузите картинку или вставьте ссылку на сообщение C3PO.")

    _set_job(user, {"state": "running"})
    task = asyncio.create_task(_run(user, data, mime, link.strip()))
    _BACKGROUND_TASKS.add(task)
    task.add_done_callback(_BACKGROUND_TASKS.discard)
    return RedirectResponse("/farm", status_code=303)
