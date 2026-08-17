"""Discord OAuth2 (authorization code flow) — переиспользует то же Discord-приложение,
что и сам бот (тот же DISCORD_TOKEN из .env), никакого отдельного бота не заводим.

Права доступа резолвятся НЕ по Discord-ролям, а по discord_id напрямую через
guild_resolver.resolve_access (игровой ранг из Comlink, закэшированный в БД, +
супер-админы + ручные гранты) — после получения identity через OAuth ни одного
дополнительного запроса к Discord API не требуется."""

import os
import secrets
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse

import database
import guild_resolver

router = APIRouter()

DISCORD_API = "https://discord.com/api/v10"

DISCORD_CLIENT_ID = os.getenv("DISCORD_CLIENT_ID")
DISCORD_CLIENT_SECRET = os.getenv("DISCORD_CLIENT_SECRET")

OAUTH_NOT_CONFIGURED_HTML = """
<!doctype html><html><body style="font-family: sans-serif; max-width: 40em; margin: 4em auto;">
<h2>Discord OAuth ещё не настроен</h2>
<p>В .env на сервере не заданы DISCORD_CLIENT_ID / DISCORD_CLIENT_SECRET.</p>
</body></html>
"""


def _redirect_uri(request: Request) -> str:
    """Складывается из хоста реального запроса — работает и для http://IP:порт
    (пока без домена), и для будущего домена без хардкода в коде."""
    return str(request.url_for("auth_callback"))


@router.get("/login")
async def login(request: Request):
    if not DISCORD_CLIENT_ID:
        return HTMLResponse(OAUTH_NOT_CONFIGURED_HTML, status_code=503)

    state = secrets.token_urlsafe(16)
    request.session["oauth_state"] = state
    params = {
        "client_id": DISCORD_CLIENT_ID,
        "redirect_uri": _redirect_uri(request),
        "response_type": "code",
        "scope": "identify",
        "state": state,
    }
    return RedirectResponse(f"{DISCORD_API}/oauth2/authorize?{urlencode(params)}")


@router.get("/callback", name="auth_callback")
async def callback(request: Request):
    if not DISCORD_CLIENT_ID or not DISCORD_CLIENT_SECRET:
        return HTMLResponse(OAUTH_NOT_CONFIGURED_HTML, status_code=503)

    code = request.query_params.get("code")
    state = request.query_params.get("state")
    if not code or not state or state != request.session.get("oauth_state"):
        return RedirectResponse("/?error=oauth_state")
    request.session.pop("oauth_state", None)

    async with httpx.AsyncClient(timeout=15.0) as client:
        token_resp = await client.post(
            f"{DISCORD_API}/oauth2/token",
            data={
                "client_id": DISCORD_CLIENT_ID,
                "client_secret": DISCORD_CLIENT_SECRET,
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": _redirect_uri(request),
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        if token_resp.status_code != 200:
            return RedirectResponse("/?error=oauth_token")
        access_token = token_resp.json()["access_token"]

        user_resp = await client.get(
            f"{DISCORD_API}/users/@me",
            headers={"Authorization": f"Bearer {access_token}"},
        )
        if user_resp.status_code != 200:
            return RedirectResponse("/?error=oauth_user")
        discord_user = user_resp.json()

    discord_id = str(discord_user["id"])
    access = guild_resolver.resolve_access(discord_id)
    username = discord_user.get("username", "?")
    database.log_web_access(discord_id, username, access["guild_id"], access["tier"], access["is_super_admin"])

    request.session["user"] = {
        "discord_id": discord_id,
        "username": username,
        "avatar": discord_user.get("avatar"),
        "guild_id": access["guild_id"],
        "tier": access["tier"],
        "is_super_admin": access["is_super_admin"],
    }
    return RedirectResponse("/")


@router.get("/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/")
