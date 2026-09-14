"""Вебхук ЮKassa об оплате подписки гильдии — см. services/payments.py (там же
докстринг про то, почему у ЮKassa нет подписи и как вместо неё проверяется
подлинность). Намеренно БЕЗ авторизации сессией (как qa_checklist.py) — это
внешний колбэк от провайдера, не страница дашборда; подлинность обеспечивает
сам handle_webhook (перезапрос платежа по id через API), а не что-то в этом
роуте."""

from fastapi import APIRouter, Request
from fastapi.responses import PlainTextResponse

from services.payments import handle_webhook

router = APIRouter()


@router.post("/webhook", response_class=PlainTextResponse)
async def webhook(request: Request):
    try:
        payload = await request.json()
    except Exception:
        return PlainTextResponse("invalid body", status_code=400)

    result = await handle_webhook(payload)
    if not result.ok:
        # Непустой статус, отличный от 200, — ЮKassa сама повторит уведомление
        # позже (см. докстринг services/payments.py), поэтому тут не 200.
        return PlainTextResponse(result.error or "error", status_code=502)
    return PlainTextResponse("OK")
