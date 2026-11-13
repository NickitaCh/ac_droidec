"""Test-only account connection readiness and existing-session read pilot."""
import secrets
from pathlib import Path

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates

from services import game_account
import database
from web.deps import require_officer_access

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parents[1] / "templates"))


def _scope(user):
    args = (str(user["discord_id"]), user.get("guild_id"), user.get("bot_profile"), user.get("tier"))
    try:
        game_account.require_pilot(*args)
    except game_account.AccountError as error:
        raise HTTPException(403, str(error)) from None
    return args


def _render(request, user, message=None, ally_code=None):
    args = _scope(user)
    request.session.setdefault("game_account_csrf", secrets.token_urlsafe(32))
    try:
        ally_code = game_account.select_account(args[0], args[1], ally_code=ally_code)
        account = game_account.status(*args, ally_code=ally_code)
    except game_account.AccountError as error:
        account, message = None, str(error)
    return templates.TemplateResponse(request=request, name="game_account.html", context={
        "request": request, "user": user, "account": account, "message": message,
        "csrf": request.session["game_account_csrf"],
        "accounts": database.get_user_registrations(args[0], guild_id=args[1]), "selected_code": ally_code,
    }, headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"})


@router.get("")
async def page(request: Request, ally_code: str = None, user=Depends(require_officer_access)):
    return _render(request, user, ally_code=ally_code)


@router.post("/{action}")
async def act(action: str, request: Request, csrf: str = Form(""), ally_code: str = Form(""), user=Depends(require_officer_access)):
    args = _scope(user)
    expected = request.session.get("game_account_csrf")
    if not expected or not secrets.compare_digest(csrf, expected):
        raise HTTPException(403, "Обновите страницу и повторите действие.")
    try:
        if action == "read":
            await game_account.refresh(*args, ally_code=ally_code or None)
            return _render(request, user, "Инвентарь прочитан. Игровые действия не выполнялись.", ally_code=ally_code or None)
        if action == "disconnect":
            game_account.disconnect(*args, ally_code=ally_code or None)
            return RedirectResponse("/game-account?ally_code=" + game_account.select_account(args[0], args[1], ally_code=ally_code or None), status_code=303)
        raise HTTPException(404)
    except game_account.AccountError as error:
        return _render(request, user, str(error), ally_code=ally_code or None)
