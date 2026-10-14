"""Фарм деталей по картинке C3PO — веб-версия бывшей Discord-команды /фарм (убрана
2026-10-14). Картинку можно загрузить файлом или дать ссылку на сообщение C3PO в Discord.
Распознавание идёт до пары минут, поэтому в фоне: статус и результат — в bot_state
(ключ на пользователя), страница обновляется сама. Логика — services/gear_farm.py."""

import asyncio
import html
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path

import httpx
from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from markupsafe import Markup

import database
from services import feature_flags, gear_farm

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent.parent / "templates"))

DISCORD_API = "https://discord.com/api/v10"
MESSAGE_LINK_RE = re.compile(r"discord(?:app)?\.com/channels/(?:\d+|@me)/(\d+)/(\d+)")
MAX_IMAGE_BYTES = 8 * 1024 * 1024
WARNING_RATIO = 0.9
_BACKGROUND_TASKS = set()


class FarmError(Exception):
    pass


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
    """**жирный** → <strong>, остальное экранируется."""
    escaped = html.escape(text)
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


@router.get("", response_class=HTMLResponse)
async def farm_page(request: Request, user: dict = Depends(feature_flags.require_feature("gear_farm"))):
    job = _get_job(user)
    return templates.TemplateResponse(request, "gear_farm.html", {
        "user": user,
        "job": job,
        "result_html": _render_text(job["message"]) if job and job.get("message") else None,
        "error": request.query_params.get("error"),
    })


@router.post("", response_class=HTMLResponse)
async def farm_start(
    link: str = Form(default=""),
    image: UploadFile | None = File(default=None),
    user: dict = Depends(feature_flags.require_feature("gear_farm")),
):
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
