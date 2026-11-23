"""Test-only account connection readiness and existing-session read pilot."""
import asyncio
import secrets
from pathlib import Path

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from services import game_account, ea_connect, game_account_readonly, game_lab, game_personal_modes
import database
from web.deps import require_officer_access, require_member_access

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parents[1] / "templates"))
PERSONAL_MODES = {"arena": "Великая арена", "conquest": "Завоевание"}


def _scope(user, *, account_read=False):
    args = (str(user["discord_id"]), user.get("guild_id"), user.get("bot_profile"), user.get("tier"))
    try:
        authorize = game_account.require_account_read if account_read else game_account.require_pilot
        authorize(*args)
    except game_account.AccountError as error:
        raise HTTPException(403, str(error)) from None
    return args


def _render(request, user, message=None, ally_code=None, *, reconnect_needed=False):
    args = _scope(user, account_read=True)
    request.session.setdefault("game_account_csrf", secrets.token_urlsafe(32))
    try:
        ally_code = game_account.select_account(args[0], args[1], ally_code=ally_code)
        account = game_account.status(*args, ally_code=ally_code) if args[1] == 1 and args[3] == "officer" else None
    except game_account.AccountError as error:
        account, message, ally_code = None, str(error), None
    return templates.TemplateResponse(request=request, name="game_account.html", context={
        "request": request, "user": user, "account": account, "message": message,
        "csrf": request.session["game_account_csrf"],
        "accounts": database.get_user_registrations(args[0], guild_id=args[1]), "selected_code": ally_code,
        "reconnect_path": game_account.link_path(args[0], args[1], ally_code) if reconnect_needed else None,
    }, headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"})


@router.get("")
async def page(request: Request, ally_code: str = None, user=Depends(require_member_access)):
    args = _scope(user, account_read=True)
    try:
        code = game_account.select_personal_account(args[0], args[1], ally_code or user.get("active_ally_code"))
    except game_account.AccountError as error:
        raise HTTPException(403, str(error)) from None
    return RedirectResponse(f"/player/{code}#ea-connection", status_code=303)


def _render_link(request, user, alt=False, message=None, linked=None):
    args = _scope(user, account_read=True)
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
        own = game_account.personal_accounts(args[0], args[1]).get(row["ally_code"])
        if own is None:
            raise HTTPException(404, "Привязка не найдена.")
        success = {"name": row["name"], "ally_code": row["ally_code"], "is_alt": not own[1]}
    return templates.TemplateResponse(request=request, name="game_account_link.html", context={
        "user": user, "is_alt": alt, "message": message, "success": success,
        "csrf": request.session["game_account_csrf"], "ea_available": ea_connect.available(),
        "ea_pending": ea_connect.pending(args[0], ea_connect.registration_target(args[0], alt, args[1]),
                                         include_email=True, include_checking=True),
    }, headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"})


@router.get("/link")
async def link_page(request: Request, alt: int = 0, linked: str = None, user=Depends(require_member_access)):
    if alt not in (0, 1) or set(request.query_params) - {"alt", "linked"}:
        raise HTTPException(400)
    return _render_link(request, user, bool(alt), linked=linked)


@router.get("/link/ea-start", include_in_schema=False)
@router.get("/link/ea-finish", include_in_schema=False)
async def link_resume(alt: int = 0, user=Depends(require_member_access)):
    _scope(user, account_read=True)
    if alt not in (0, 1):
        raise HTTPException(400)
    return RedirectResponse(f"/game-account/link?alt={alt}", status_code=303)


@router.post("/link/{step}")
async def link_action(step: str, request: Request, user=Depends(require_member_access)):
    args = _scope(user, account_read=True)
    if step not in ("ea-start", "ea-finish"):
        raise HTTPException(404)
    form = await request.form(max_files=0, max_fields=6)
    csrf = form.get("csrf")
    expected = request.session.get("game_account_csrf")
    if not expected or not isinstance(csrf, str) or not secrets.compare_digest(csrf, expected):
        raise HTTPException(403, "Обновите страницу и повторите действие.")
    alt = form.get("alt", "0")
    allowed = {"csrf", "alt", "ea_email", "consent"} if step == "ea-start" else {"csrf", "alt", "flow_id", "one_time_code"}
    if (alt not in ("0", "1") or set(form) - allowed
            or request.query_params.get("alt", alt) != alt):
        raise HTTPException(400)
    is_alt = alt == "1"
    try:
        if step == "ea-start":
            await ea_connect.start_registration(args[0], is_alt, str(form.get("ea_email", "")), form.get("consent") == "yes",
                                                guild_id=args[1])
            return RedirectResponse(f"/game-account/link?alt={alt}", status_code=303)
        from web.routes.registration import _get_comlink
        result = await ea_connect.finish_registration(args[0], is_alt, str(form.get("flow_id", "")), str(form.get("one_time_code", "")),
                                                      _get_comlink(), guild_id=args[1])
        return RedirectResponse(f"/game-account/link?alt={int(is_alt)}&linked={result['target']}", status_code=303)
    except game_account.AccountError as error:
        return _render_link(request, user, is_alt, str(error))


def connection_prompt(user, code, error=None, *, reconnect=False):
    owner, gid = str(user["discord_id"]), user["guild_id"]
    if isinstance(error, game_account.SessionExpired) and not reconnect:
        return {"connection_action": "login", "login_warning": game_account.LOGIN_WARNING}
    if error is not None or not game_account_readonly._connection(owner, code):
        return {"connection_action": "link", "link_path": game_account.link_path(owner, gid, code)}
    return {}


def _render_modules(request, user, code, *, snapshot=None, error=None, prompt=None):
    request.session.setdefault("game_account_csrf", secrets.token_urlsafe(32))
    return templates.TemplateResponse(request, "game_account_modules.html", {
        "user": user, "csrf": request.session["game_account_csrf"], "ally_code": code,
        "bank": snapshot.get("modules") if snapshot else None, "error": error,
        **(prompt or {}),
    }, headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"})


@router.get("/modules", response_class=HTMLResponse)
def modules_page(request: Request, ally_code: str = "", user=Depends(require_member_access)):
    args = _scope(user, account_read=True)
    try:
        code = game_account.select_personal_account(args[0], args[1], ally_code or user.get("active_ally_code"))
    except game_account.AccountError as error:
        raise HTTPException(403, str(error)) from None
    return _render_modules(request, user, code, prompt=connection_prompt(user, code))


@router.post("/modules", response_class=HTMLResponse)
async def modules_read(request: Request, csrf: str = Form(...), ally_code: str = Form(...),
                       reconnect: bool = Form(False),
                       user=Depends(require_member_access)):
    args = _scope(user, account_read=True)
    expected = request.session.get("game_account_csrf")
    if not expected or not secrets.compare_digest(csrf.encode(), expected.encode()):
        raise HTTPException(403)
    try:
        code = game_account.select_personal_account(args[0], args[1], ally_code)
    except game_account.AccountError as error:
        raise HTTPException(403, str(error)) from None
    try:
        if reconnect:
            await game_account.reconnect(*args, code, optimizer_read=True, personal=True)
        snapshot = await game_account_readonly.read(*args, code, member_read=True, personal=True)
    except game_account.ConnectionRequired as error:
        return _render_modules(request, user, code, prompt=connection_prompt(user, code, error, reconnect=reconnect))
    except game_account.AccountError as error:
        return _render_modules(request, user, code, error=str(error))
    return _render_modules(request, user, code, snapshot=snapshot)


def _personal_account(user, mode, ally_code):
    if mode not in PERSONAL_MODES:
        raise HTTPException(404)
    scope = _scope(user, account_read=True)
    try:
        code = game_account.select_personal_account(scope[0], scope[1], ally_code or user.get("active_ally_code"))
    except game_account.AccountError as error:
        raise HTTPException(403, str(error)) from None
    return scope, code


def _render_personal(request, user, mode, code, *, snapshot=None, loaded=False, error=None, prompt=None):
    request.session.setdefault("game_account_csrf", secrets.token_urlsafe(32))
    if mode == "arena" and snapshot:
        game_personal_modes.name_arena_units(snapshot)
    if mode == "conquest" and snapshot and snapshot["stamina"]:
        names = database.get_game_unit_names([row["unit"] for row in snapshot["stamina"]])
        for row in snapshot["stamina"]:
            row["name"] = names.get(row["unit"], row["unit"])
    return templates.TemplateResponse(request, "game_personal_mode.html", {
        "user": user, "mode": mode, "modes": PERSONAL_MODES, "title": PERSONAL_MODES[mode],
        "ally_code": code, "csrf": request.session["game_account_csrf"],
        "snapshot": snapshot, "loaded": loaded, "error": error, **(prompt or {}),
    }, headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"})


@router.get("/personal/{mode}", response_class=HTMLResponse)
def personal_page(request: Request, mode: str, ally_code: str = "", reconnect: bool = False,
                  user=Depends(require_member_access)):
    _, code = _personal_account(user, mode, ally_code)
    error = game_account.SessionExpired() if reconnect and game_account_readonly._connection(str(user["discord_id"]), code) else None
    return _render_personal(request, user, mode, code, prompt=connection_prompt(user, code, error))


@router.post("/personal/{mode}", response_class=HTMLResponse)
async def personal_read(request: Request, mode: str, csrf: str = Form(...), ally_code: str = Form(...),
                        reconnect: bool = Form(False),
                        user=Depends(require_member_access)):
    expected = request.session.get("game_account_csrf")
    if not expected or not secrets.compare_digest(csrf.encode(), expected.encode()):
        raise HTTPException(403, "Обновите страницу и повторите действие.")
    scope, code = await asyncio.to_thread(_personal_account, user, mode, ally_code)
    try:
        if reconnect:
            await game_account.reconnect(*scope, code, optimizer_read=True, personal=True)
        snapshot = await game_account_readonly.read(*scope, code, event=mode, member_read=True, personal=True)
    except game_account.ConnectionRequired as error:
        return _render_personal(request, user, mode, code,
                                prompt=connection_prompt(user, code, error, reconnect=reconnect))
    except game_account.AccountError as error:
        return _render_personal(request, user, mode, code, error=str(error))
    return await asyncio.to_thread(_render_personal, request, user, mode, code, snapshot=snapshot, loaded=True)


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
    except game_account.ConnectionRequired:
        return _render(request, user, ally_code=ally_code or None, reconnect_needed=True)
    except game_account.AccountError as error:
        return _render(request, user, str(error), ally_code=ally_code or None)
