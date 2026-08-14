"""Discord OAuth2 (authorization code flow) — переиспользует то же Discord-приложение,
что и сам бот (тот же DISCORD_TOKEN из .env), никакого отдельного бота не заводим.

Роли пользователя в конкретном Discord-сервере получаем НЕ через OAuth-скоуп
guilds.members.read (ограниченный/требует апрува от Discord), а серверным вызовом
GET /guilds/{id}/members/{user_id} с ботовым токеном — тот же паттерн, что уже
используют cogs (main.py::_check_allowed_role и guild_resolver.py), только роли
приходят по HTTP, а не из disnake.Member."""

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
DISCORD_BOT_TOKEN = os.getenv("DISCORD_TOKEN")

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
        role_ids = set()
        for guild_cfg in database.get_all_guild_configs():
            discord_guild_id = guild_cfg.get("discord_guild_id")
            if not discord_guild_id:
                continue
            member_resp = await client.get(
                f"{DISCORD_API}/guilds/{discord_guild_id}/members/{discord_id}",
                headers={"Authorization": f"Bot {DISCORD_BOT_TOKEN}"},
            )
            if member_resp.status_code == 200:
                role_ids.update(int(r) for r in member_resp.json().get("roles", []))

    # Веб-дашборд — только для офицеров (в отличие от слэш-команд бота, где
    # достаточно member_role_id): доступ сюда даём по officer_role_id гильдии.
    resolved_guild_id = guild_resolver.resolve_officer_guild_id_from_roles(role_ids)

    request.session["user"] = {
        "discord_id": discord_id,
        "username": discord_user.get("username", "?"),
        "avatar": discord_user.get("avatar"),
        "guild_id": resolved_guild_id,
    }
    return RedirectResponse("/")


@router.get("/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/")
