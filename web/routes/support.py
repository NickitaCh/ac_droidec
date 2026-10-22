"""«Поддержать» (/donate) и обращения с сайта (/feedback), 2026-10-22.

/donate — донат любой суммой через ЮKassa (services/payments.py::build_donation_link), открыт
и гостям. /feedback — обращение «ошибка / не нравится / идея» от любого залогиненного:
текст, журнал (ошибки браузера из static/feedback.js + ошибки сервера этого пользователя из
web_errors, их пишет обработчик в web/app.py) и файл. Супер-админам — в личку и на
/admin/feedback, ответ уходит автору в личку и виден ему на /feedback."""

import asyncio
from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

import database
from services import discord_rest
from services.payments import DONATION_MAX, DONATION_MIN, DONATION_PRESETS, SITE_URL, build_donation_link
from web.deps import get_current_user, get_current_user_optional, require_super_admin

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent.parent / "templates"))

FEEDBACK_KINDS = {"bug": "Ошибка", "dislike": "Не нравится", "idea": "Идея"}
FEEDBACK_STATUSES = {"new": "Новое", "in_progress": "В работе", "done": "Решено", "rejected": "Не будем делать"}
FEEDBACK_PER_HOUR = 10
MAX_MESSAGE = 4000
MAX_LOG = 200_000
MAX_ATTACHMENT = 5 * 1024 * 1024


def _background(coro):
    """Уведомление в Discord не должно задерживать ответ страницы."""
    task = asyncio.create_task(coro)
    task.add_done_callback(lambda t: t.exception() and print(f"⚠️ [Обращения] {t.exception()!r}"))


# ----------------------------------------------------------------- донаты

@router.get("/donate", response_class=HTMLResponse)
async def donate_page(request: Request, user: dict | None = Depends(get_current_user_optional)):
    return templates.TemplateResponse(request, "donate.html", {
        "user": user,
        "presets": DONATION_PRESETS,
        "min_amount": DONATION_MIN,
        "max_amount": DONATION_MAX,
        "supporters": database.get_donation_supporters(),
        "thanks": bool(request.query_params.get("thanks")),
        "error": None,
        "show_footer": True,
    })


@router.post("/donate")
async def donate_submit(
    request: Request,
    amount: str = Form(...),
    display_name: str = Form(default=""),
    show_name: str = Form(default=""),
    comment: str = Form(default=""),
    user: dict | None = Depends(get_current_user_optional),
):
    try:
        value = int("".join(ch for ch in amount if ch.isdigit()) or 0)
    except ValueError:
        value = 0
    name = display_name.strip()[:40] if show_name else ""
    link = await build_donation_link(
        value,
        discord_id=str(user["discord_id"]) if user and user.get("discord_id") else None,
        username=user.get("username") if user else None,
        display_name=name or None,
        comment=comment.strip()[:300] or None,
    )
    if not link.ok:
        return templates.TemplateResponse(request, "donate.html", {
            "user": user, "presets": DONATION_PRESETS, "min_amount": DONATION_MIN, "max_amount": DONATION_MAX,
            "supporters": database.get_donation_supporters(), "thanks": False, "error": link.error, "show_footer": True,
        })
    return RedirectResponse(link.url, status_code=303)


@router.get("/admin/donations", response_class=HTMLResponse)
async def admin_donations(request: Request, user: dict = Depends(require_super_admin)):
    donations = database.get_donations()
    paid = [d for d in donations if d["status"] == "succeeded"]
    return templates.TemplateResponse(request, "admin_donations.html", {
        "user": user,
        "donations": donations,
        "paid_total": sum(d["amount"] for d in paid),
        "paid_count": len(paid),
    })


# ----------------------------------------------------------------- обращения

def _server_log_lines(discord_id: str) -> list[str]:
    lines = []
    for e in database.get_recent_web_errors(discord_id):
        lines.append(f"[{e['created_at_msk']} МСК] сервер · ошибка №{e['id']} · {e['method']} {e['path']}")
        lines.extend("    " + line for line in (e["error"] or "").strip().splitlines()[-25:])
    return lines


@router.get("/feedback/log", response_class=JSONResponse)
async def feedback_server_log(user: dict = Depends(get_current_user)):
    """Ошибки сервера на страницах этого пользователя за сутки — подмешиваются в журнал обращения."""
    return {"lines": _server_log_lines(str(user["discord_id"]))}


@router.get("/feedback", response_class=HTMLResponse)
async def feedback_page(request: Request, user: dict = Depends(get_current_user)):
    return templates.TemplateResponse(request, "feedback.html", {
        "user": user,
        "reports": database.get_bug_reports(discord_id=str(user["discord_id"]), limit=50),
        "kinds": FEEDBACK_KINDS,
        "statuses": FEEDBACK_STATUSES,
        "sent": request.query_params.get("sent"),
        "error": request.query_params.get("error"),
    })


@router.post("/feedback")
async def feedback_submit(
    request: Request,
    kind: str = Form(default="bug"),
    message: str = Form(...),
    page_url: str = Form(default=""),
    log_text: str = Form(default=""),
    attachment: UploadFile | None = File(default=None),
    user: dict = Depends(get_current_user),
):
    wants_json = "application/json" in request.headers.get("accept", "")

    def fail(text: str, status: int = 400):
        if wants_json:
            return JSONResponse({"ok": False, "error": text}, status_code=status)
        return RedirectResponse(f"/feedback?error={quote(text)}", status_code=303)

    if user.get("is_demo"):
        return fail("В демо-режиме обращения не отправляются.", 403)
    message = message.strip()
    if not message:
        return fail("Опишите, что случилось.")
    if kind not in FEEDBACK_KINDS:
        kind = "bug"
    discord_id = str(user["discord_id"])
    if database.count_recent_bug_reports(discord_id) >= FEEDBACK_PER_HOUR:
        return fail("Слишком много обращений за час — попробуйте позже.", 429)

    file_name = file_type = file_data = None
    if attachment is not None and attachment.filename:
        file_data = await attachment.read(MAX_ATTACHMENT + 1)
        if len(file_data) > MAX_ATTACHMENT:
            return fail("Файл больше 5 МБ.")
        file_name = attachment.filename[:120]
        file_type = attachment.content_type or "application/octet-stream"

    report_id = database.create_bug_report(
        discord_id=discord_id,
        username=user.get("username"),
        guild_id=user.get("guild_id"),
        kind=kind,
        page_url=page_url.strip()[:500],
        message=message[:MAX_MESSAGE],
        log_text=log_text[-MAX_LOG:] or None,
        user_agent=request.headers.get("user-agent", "")[:300],
        attachment_name=file_name,
        attachment_type=file_type,
        attachment=file_data,
    )
    preview = message if len(message) <= 600 else message[:600] + "…"
    extras = []
    if log_text:
        extras.append("журнал")
    if file_name:
        extras.append(f"файл {file_name}")
    _background(discord_rest.notify_super_admins(
        f"📨 Обращение №{report_id} · {FEEDBACK_KINDS[kind]} · {user.get('username') or discord_id}\n"
        f"{page_url[:200]}\n> " + preview.replace("\n", "\n> ")
        + (f"\n({', '.join(extras)})" if extras else "")
        + f"\n{SITE_URL}/admin/feedback#report-{report_id}"
    ))
    if wants_json:
        return {"ok": True, "id": report_id}
    return RedirectResponse(f"/feedback?sent={report_id}", status_code=303)


@router.get("/admin/feedback", response_class=HTMLResponse)
async def admin_feedback(request: Request, user: dict = Depends(require_super_admin)):
    status = request.query_params.get("status", "open")
    reports = database.get_bug_reports()
    if status == "open":
        reports = [r for r in reports if r["status"] in ("new", "in_progress")]
    elif status in FEEDBACK_STATUSES:
        reports = [r for r in reports if r["status"] == status]
    return templates.TemplateResponse(request, "admin_feedback.html", {
        "user": user,
        "reports": reports,
        "status": status,
        "kinds": FEEDBACK_KINDS,
        "statuses": FEEDBACK_STATUSES,
    })


@router.post("/admin/feedback/{report_id}")
async def admin_feedback_update(
    report_id: int,
    status: str = Form(...),
    reply: str = Form(default=""),
    back: str = Form(default="open"),
    user: dict = Depends(require_super_admin),
):
    report = database.get_bug_report(report_id)
    if report is None or status not in FEEDBACK_STATUSES:
        return RedirectResponse("/admin/feedback", status_code=303)
    reply = reply.strip()[:2000]
    database.update_bug_report(report_id, status, reply or None)
    status_changed = status != report["status"]
    reply_changed = reply and reply != (report["reply"] or "")
    if report["discord_id"] and (reply_changed or (status_changed and status in ("done", "rejected"))):
        text = f"📨 Ваше обращение №{report_id} — {FEEDBACK_STATUSES[status].lower()}."
        if reply:
            text += f"\n> " + reply.replace("\n", "\n> ")
        text += f"\n{SITE_URL}/feedback"
        _background(discord_rest.send_dm(report["discord_id"], text))
    return RedirectResponse(f"/admin/feedback?status={back}#report-{report_id}", status_code=303)


def _content_disposition(name: str) -> str:
    return f"attachment; filename*=UTF-8''{quote(name)}"


@router.get("/admin/feedback/{report_id}/log.txt")
async def admin_feedback_log(report_id: int, user: dict = Depends(require_super_admin)):
    report = database.get_bug_report(report_id)
    blob = database.get_bug_report_blob(report_id)
    if report is None or blob is None:
        return PlainTextResponse("Нет такого обращения.", status_code=404)
    head = [
        f"Обращение №{report_id} · {FEEDBACK_KINDS.get(report['kind'], report['kind'])} · {report['created_at_msk']} МСК",
        f"Автор: {report['username']} ({report['discord_id']}) · гильдия: {report['guild_name'] or '—'}",
        f"Страница: {report['page_url'] or '—'}",
        f"Браузер: {blob['user_agent'] or '—'}",
        "",
        report["message"],
        "",
        "---- журнал ----",
    ]
    text = "\n".join(head) + "\n" + (blob["log_text"] or "(журнал не приложен)")
    return PlainTextResponse(text, headers={"Content-Disposition": _content_disposition(f"report-{report_id}.txt")})


@router.get("/admin/feedback/{report_id}/file")
async def admin_feedback_file(report_id: int, user: dict = Depends(require_super_admin)):
    blob = database.get_bug_report_blob(report_id)
    if not blob or not blob["attachment"]:
        return PlainTextResponse("Файла нет.", status_code=404)
    # Картинки (кроме SVG) — открыть в браузере; остальное — только скачать, чтобы присланный
    # HTML/SVG не исполнился на нашем домене.
    ctype = blob["attachment_type"] or ""
    headers = {"X-Content-Type-Options": "nosniff"}
    if ctype.startswith("image/") and ctype != "image/svg+xml":
        return Response(blob["attachment"], media_type=ctype, headers=headers)
    headers["Content-Disposition"] = _content_disposition(blob["attachment_name"] or "file")
    return Response(blob["attachment"], media_type="application/octet-stream", headers=headers)
