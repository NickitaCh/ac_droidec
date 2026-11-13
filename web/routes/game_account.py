"""Test-only account connection readiness and existing-session read pilot."""
import secrets
from pathlib import Path

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates

from services import game_account, ea_connect, game_lab
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


def _render_link(request, user, alt=False, message=None, linked=None):
    args = _scope(user)
    request.session.setdefault("game_account_csrf", secrets.token_urlsafe(32))
    success = None
    if linked:
        # The URL can only display a successful connection belonging to this user.
        try:
            row = game_lab._row(linked)
        except game_account.AccountError:
            raise HTTPException(404, "Подключение не найдено.") from None
        if row["added_by"] != args[0] or not ea_connect.linked(linked):
            raise HTTPException(404, "Подключение не найдено.")
        own = next((r for r in database.get_user_registrations(args[0], guild_id=1) if str(r[0]) == row["ally_code"]), None)
        if own is None:
            raise HTTPException(404, "Привязка не найдена.")
        success = {"name": row["name"], "ally_code": row["ally_code"], "is_alt": not bool(own[2])}
    return templates.TemplateResponse(request=request, name="game_account_link.html", context={
        "user": user, "is_alt": alt, "message": message, "success": success,
        "csrf": request.session["game_account_csrf"], "ea_available": ea_connect.available(),
        "ea_pending": ea_connect.pending(args[0], ea_connect.registration_target(args[0], alt)),
    }, headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"})


@router.get("/link")
async def link_page(request: Request, alt: int = 0, linked: str = None, user=Depends(require_officer_access)):
    if alt not in (0, 1) or set(request.query_params) - {"alt", "linked"}:
        raise HTTPException(400)
    return _render_link(request, user, bool(alt), linked=linked)


@router.post("/link/{step}")
async def link_action(step: str, request: Request, user=Depends(require_officer_access)):
    args = _scope(user)
    if step not in ("ea-start", "ea-finish"):
        raise HTTPException(404)
    form = await request.form(max_files=0, max_fields=6)
    csrf = form.get("csrf")
    expected = request.session.get("game_account_csrf")
    if not expected or not isinstance(csrf, str) or not secrets.compare_digest(csrf, expected):
        raise HTTPException(403, "Обновите страницу и повторите действие.")
    alt = form.get("alt", "0")
    allowed = {"csrf", "alt", "ea_email", "consent"} if step == "ea-start" else {"csrf", "alt", "flow_id", "one_time_code"}
    if alt not in ("0", "1") or set(form) - allowed:
        raise HTTPException(400)
    is_alt = alt == "1"
    try:
        if step == "ea-start":
            await ea_connect.start_registration(args[0], is_alt, str(form.get("ea_email", "")), form.get("consent") == "yes")
            return _render_link(request, user, is_alt, "Код отправлен. Введите код из последнего письма EA.")
        from web.routes.registration import _get_comlink
        result = await ea_connect.finish_registration(args[0], is_alt, str(form.get("flow_id", "")), str(form.get("one_time_code", "")), _get_comlink())
        return RedirectResponse(f"/game-account/link?alt={int(is_alt)}&linked={result['target']}", status_code=303)
    except game_account.AccountError as error:
        return _render_link(request, user, is_alt, str(error))


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
