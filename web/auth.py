"""Discord OAuth2 (authorization code flow) — переиспользует то же Discord-приложение,
что и сам бот (тот же DISCORD_TOKEN из .env), никакого отдельного бота не заводим.

Права доступа резолвятся НЕ по Discord-ролям, а по discord_id напрямую через
guild_resolver.resolve_access (игровой ранг из Comlink, закэшированный в БД, +
супер-админы + ручные гранты) — после получения identity через OAuth ни одного
дополнительного запроса к Discord API не требуется."""

import os
import secrets
from pathlib import Path
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

import database
import guild_resolver

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent / "templates"))

DISCORD_API = "https://discord.com/api/v10"

DISCORD_CLIENT_ID = os.getenv("DISCORD_CLIENT_ID")
DISCORD_CLIENT_SECRET = os.getenv("DISCORD_CLIENT_SECRET")


def _safe_next(value: str | None) -> str:
    """Только относительный путь на нашем же сайте — иначе ?next= превращается
    в открытый редирект (например на //evil.example или https://evil.example)."""
    if not value or not value.startswith("/") or value.startswith("//"):
        return "/"
    return value

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
    request.session["post_login_redirect"] = _safe_next(request.query_params.get("next"))
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
    return RedirectResponse(request.session.pop("post_login_redirect", "/"))


@router.get("/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/")


# =====================================================================
# ВХОД ПО ЛОГИНУ/ПАРОЛЮ — второй способ входа рядом с Discord OAuth выше,
# для офицеров, у которых нет доступа к Discord (блокировки РФ). Учётки
# заводит только супер-админ (web/routes/admin.py, /admin/web-accounts,
# database.create_web_credential) — самостоятельной регистрации нет.
# Права после входа резолвятся ТАК ЖЕ, как при OAuth: по discord_id,
# привязанному к учётке, через guild_resolver.resolve_access — учётка
# логин/пароль сама по себе прав не несёт, это только альтернативный
# способ подтвердить, что за discord_id стоит именно этот человек.
# =====================================================================
@router.get("/login/password", response_class=HTMLResponse)
async def login_password_form(request: Request):
    return templates.TemplateResponse(request, "login_password.html", {
        "user": request.session.get("user"),
        "error": request.query_params.get("error"),
        "next": _safe_next(request.query_params.get("next")),
    })


@router.post("/login/password")
async def login_password_submit(request: Request, login: str = Form(...), password: str = Form(...), next: str = Form("/")):
    next_url = _safe_next(next)
    discord_id = database.verify_web_credential(login.strip(), password)
    if not discord_id:
        error_params = {"error": "Неверный логин или пароль."}
        if next_url != "/":
            error_params["next"] = next_url
        return RedirectResponse(f"/login/password?{urlencode(error_params)}", status_code=303)

    access = guild_resolver.resolve_access(discord_id)
    # У логин/паролевых учёток нет Discord username из OAuth — берём лучшее
    # известное имя (см. database.get_username_for_discord_id), а если ни разу
    # не встречался нигде (первый вход) — сам логин как временное отображаемое имя.
    username = database.get_username_for_discord_id(discord_id) or login.strip()
    database.log_web_access(discord_id, username, access["guild_id"], access["tier"], access["is_super_admin"])

    request.session["user"] = {
        "discord_id": discord_id,
        "username": username,
        "avatar": None,
        "guild_id": access["guild_id"],
        "tier": access["tier"],
        "is_super_admin": access["is_super_admin"],
    }
    return RedirectResponse(next_url, status_code=303)
