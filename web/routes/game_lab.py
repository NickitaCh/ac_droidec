"""Super-admin laboratory; only explicitly enrolled test accounts."""
import secrets
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from starlette.datastructures import UploadFile

from services import game_lab, game_account, ea_connect
from web.deps import require_super_admin

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parents[1] / "templates"))


def _scope(user):
    if not user.get("is_super_admin") or user.get("bot_profile") != "test" or user.get("view_as"):
        raise HTTPException(403, "Испытания доступны только суперадминам в тестовой версии.")


def _render(request, user, target="notest", message=None, probes=None, report=None):
    _scope(user)
    accounts = game_lab.accounts()
    selected = next((account for account in accounts if account["id"] == target), None)
    if selected is None:
        raise HTTPException(404, "Аккаунт не добавлен к испытаниям.")
    try:
        selected = game_lab.current(target)
    except game_account.AccountError as error:
        selected = {**selected, "state": "error", "summary": None}
        message = str(error)
    request.session.setdefault("game_lab_csrf", secrets.token_urlsafe(32))
    capabilities = game_lab.catalog()
    summary = selected.get("summary")
    from datetime import datetime, timezone, timedelta
    read_at = datetime.fromtimestamp(summary["read_at"], timezone(timedelta(hours=3))).strftime("%d.%m.%Y %H:%M МСК") if summary and summary.get("read_at") else None
    return templates.TemplateResponse(request=request, name="game_lab.html", context={
        "user": user, "accounts": accounts, "account": selected, "message": message,
        "csrf": request.session["game_lab_csrf"], "capabilities": capabilities, "probes": probes, "report": report,
        "groups": list(dict.fromkeys(item[1] for item in game_lab.CAPABILITIES)),
        "states": game_lab.STATES, "read_at": read_at,
        "ea_available": ea_connect.available(), "ea_pending": ea_connect.pending(user["discord_id"], target),
    }, headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"})


@router.get("")
async def page(request: Request, target: str = "notest", user=Depends(require_super_admin)):
    if set(request.query_params) - {"target"}:
        raise HTTPException(400, "Выберите аккаунт из списка испытаний.")
    return _render(request, user, target)


@router.post("/{action}")
async def action(action: str, request: Request, user=Depends(require_super_admin)):
    _scope(user)
    if action not in {"add", "read", "probe", "connect", "remove", "ea-start", "ea-finish", "ea-disconnect"}:
        raise HTTPException(404)
    form = await request.form(max_files=1, max_fields=8)
    csrf = form.get("csrf")
    expected = request.session.get("game_lab_csrf")
    if not expected or not isinstance(csrf, str) or not secrets.compare_digest(csrf, expected):
        raise HTTPException(403, "Обновите страницу и повторите действие.")
    allowed = {"csrf", "ally_code", "consent"} if action == "add" else {"csrf", "target"}
    if action == "connect":
        allowed |= {"provider", "session_secret", "consent"}
    if action == "ea-start":
        allowed |= {"ea_email", "consent"}
    if action == "ea-finish":
        allowed |= {"flow_id", "one_time_code"}
    if set(form) - allowed:
        raise HTTPException(400, "Неизвестные параметры формы.")
    target = form.get("target", "notest")
    if not isinstance(target, str):
        raise HTTPException(400)
    if action != "add" and not any(item["id"] == target for item in game_lab.accounts()):
        raise HTTPException(404, "Аккаунт не добавлен к испытаниям.")
    try:
        if action == "ea-start":
            await ea_connect.start(user["discord_id"], target, str(form.get("ea_email", "")), form.get("consent") == "yes")
            return _render(request, user, target, "EA отправила код. Введите код из последнего письма.")
        if action == "ea-finish":
            await ea_connect.finish(user["discord_id"], target, str(form.get("flow_id", "")), str(form.get("one_time_code", "")))
            return _render(request, user, target, "EA подключена. Доступ сохранён; игровая сессия будет восстанавливаться при следующих запросах.")
        if action == "ea-disconnect":
            ea_connect.disconnect(target)
            if target == "notest":
                game_account.disconnect(game_lab.NOTEST_OWNER, 1, "test", ally_code=game_lab.NOTEST_CODE)
            else:
                with game_lab._db() as conn:
                    conn.execute("UPDATE game_lab_accounts SET secret=NULL,provider=NULL,state='unconnected',summary=NULL WHERE id=?", (target,))
            return _render(request, user, target, "Сохранённый доступ удалён. Для дальнейшего чтения понадобится новое подключение.")
        if action == "add":
            from web.routes.registration import _get_comlink
            target = await game_lab.add_account(user["discord_id"], str(form.get("ally_code", "")),
                                                form.get("consent") == "yes", _get_comlink())
            return RedirectResponse("/admin/game-lab?target=" + target, status_code=303)
        if action == "read":
            report = []
            await game_lab.refresh(target, report=report)
            return _render(request, user, target, "Данные прочитаны из игры.", report=report)
        if action == "probe":
            if not ea_connect.linked(target):
                raise game_account.AccountError("Для этой проверки подключите аккаунт через EA.")
            probes = await ea_connect.read(target, probes=True)
            return _render(request, user, target, probes=probes)
        if action == "connect":
            session = form.get("session_secret")
            if not isinstance(session, UploadFile):
                raise game_account.AccountError("Выберите файл игровой сессии.")
            try:
                raw = await session.read(game_lab.MAX_SESSION_BYTES + 1)
            finally:
                await session.close()
            await game_lab.connect(target, str(form.get("provider", "")), raw, form.get("consent") == "yes")
            return _render(request, user, target, "Сессия проверена и подключена. Данные сохранены в зашифрованном виде.")
        game_lab.remove(target)
        return RedirectResponse("/admin/game-lab", status_code=303)
    except game_account.AccountError as error:
        return _render(request, user, target, str(error))
