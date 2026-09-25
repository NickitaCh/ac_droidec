"""Портреты юнитов: /unit-img/<base_id> — 128×128 картинка с CDN swgoh.gg по имени
текстуры из game_units.thumbnail_name (services/units_sync.py). Скачивается один раз
и кэшируется на диске (cache/unit_img/, bind-mount /opt/bot — переживает рестарты),
дальше отдаётся как статика. Горячих ссылок на swgoh.gg в шаблонах нет специально —
их CDN может сменить схему, а кэш у нас останется."""
import asyncio
import re
from pathlib import Path

import httpx
from fastapi import APIRouter
from fastapi.responses import FileResponse, RedirectResponse

import database

router = APIRouter()

CACHE_DIR = Path("cache/unit_img")
CDN_URL = "https://game-assets.swgoh.gg/textures/{name}.png"
FALLBACK_URL = "/static/img/game/portrait-fallback.svg"
_BASE_ID_RE = re.compile(r"^[A-Z0-9_]{1,64}$")
_TEXTURE_RE = re.compile(r"^tex\.[a-z0-9_]{1,80}$")
_CACHE_HEADERS = {"Cache-Control": "public, max-age=604800"}
_locks: dict[str, asyncio.Lock] = {}


async def _download(name: str, dest: Path) -> bool:
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(CDN_URL.format(name=name))
    except httpx.HTTPError:
        return False
    if resp.status_code != 200 or not resp.headers.get("content-type", "").startswith("image/"):
        return False
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(".tmp")
    tmp.write_bytes(resp.content)
    tmp.replace(dest)
    return True


@router.get("/unit-img/{base_id}")
async def unit_image(base_id: str):
    if not _BASE_ID_RE.match(base_id):
        return RedirectResponse(FALLBACK_URL)
    path = CACHE_DIR / f"{base_id}.png"
    if path.exists():
        return FileResponse(path, media_type="image/png", headers=_CACHE_HEADERS)

    name = await asyncio.to_thread(database.get_unit_thumbnail, base_id)
    if not name or not _TEXTURE_RE.match(name):
        return RedirectResponse(FALLBACK_URL)

    lock = _locks.setdefault(base_id, asyncio.Lock())
    async with lock:
        if not path.exists() and not await _download(name, path):
            return RedirectResponse(FALLBACK_URL)
    return FileResponse(path, media_type="image/png", headers=_CACHE_HEADERS)
