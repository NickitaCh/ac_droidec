"""Оплата подписки гильдии через ЮKassa (self-employed-friendly, разовые периоды
месяц/год — см. план C:\\Users\\Nick\\.claude\\plans\\melodic-crunching-dream.md,
писался под Prodamus; провайдера сменили на ЮKassa 2026-09-14, кабинет открыт —
архитектура вокруг provider-обвязки не поменялась, вся специфика по-прежнему
собрана в этом одном файле).

ЮKassa REST API v3 (https://yookassa.ru/developers/api), Basic Auth (shopId:
secretKey из личного кабинета). Создание платежа — POST /v3/payments с
confirmation.type="redirect": пользователь платит на их хостед-странице
confirmation_url, после чего возвращается на return_url. guild_id платёж несёт
в metadata (в отличие от Prodamus, order_id тут генерирует САМА ЮKassa — в него
ничего не закодировать), поэтому pending-запись пишется в БД сразу при создании
ссылки (database.record_payment) — вебхуку останется только найти guild_id по
order_id = id платежа.

ВАЖНО про вебхук: у ЮKassa нет подписи/HMAC-заголовка на уведомлениях (проверено
по https://yookassa.ru/developers/using-api/webhooks, 2026-09-14) — официально
рекомендованный (и единственный) способ проверки подлинности — перезапросить
платёж по id через сам API (GET /v3/payments/{id}, тот же Basic Auth) и
доверять ТОЛЬКО этому authoritative-ответу, а не телу входящего запроса. Именно
так и сделано в handle_webhook/_fetch_payment ниже — тело вебхука используется
только чтобы достать id платежа."""

import os
import uuid
from dataclasses import dataclass

import httpx

import database

PROVIDER = "yookassa"
API_BASE = "https://api.yookassa.ru/v3"

# Совпадает 1-в-1 с тарифами, опубликованными на сайте (web/templates/base.html) —
# менять только вместе (см. план — тот же принцип, что был у Prodamus).
PERIOD_CHOICES = {
    30: 500,
    365: 5000,
}

# Дублирует main.py::SITE_URL — тот же паттерн, что и другие дублирования
# constants между процессами бота/веба, см. services/dashboard_data.py::SITE_URL
# и CLAUDE.md про COMLINK_URL/ALLOWED_OFFICER_ROLE_ID. return_url — просто
# "куда вернуться после оплаты", отдельной страницы благодарности пока нет.
SITE_URL = "https://swgoh-sng.ru"
DEFAULT_RETURN_URL = f"{SITE_URL}/start"


def _shop_id() -> str:
    return os.getenv("YOOKASSA_SHOP_ID", "")


def _secret_key() -> str:
    return os.getenv("YOOKASSA_SECRET_KEY", "")


def _auth():
    return (_shop_id(), _secret_key())


@dataclass
class PaymentLinkResult:
    ok: bool
    url: str = None
    order_id: str = None
    error: str = None


async def build_payment_link(guild_id: int, guild_name: str, period_days: int, return_url: str = None) -> PaymentLinkResult:
    if period_days not in PERIOD_CHOICES:
        return PaymentLinkResult(ok=False, error=f"Неизвестный период подписки: {period_days} дней.")
    if not _shop_id() or not _secret_key():
        return PaymentLinkResult(ok=False, error="Оплата временно недоступна (не настроен платёжный кабинет).")

    amount = PERIOD_CHOICES[period_days]
    body = {
        "amount": {"value": f"{amount:.2f}", "currency": "RUB"},
        "capture": True,
        "confirmation": {"type": "redirect", "return_url": return_url or DEFAULT_RETURN_URL},
        "description": f"Подписка AC Droidec: {guild_name} ({period_days} дн.)"[:128],
        # guild_id/period_days тут — единственный способ вебхуку узнать, какую
        # гильдию продлевать (см. докстринг файла); ЮKassa возвращает metadata
        # как есть в объекте платежа и в уведомлении.
        "metadata": {"guild_id": str(guild_id), "period_days": str(period_days)},
    }
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(
                f"{API_BASE}/payments", json=body, auth=_auth(),
                headers={"Idempotence-Key": uuid.uuid4().hex},
            )
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        return PaymentLinkResult(ok=False, error=f"Не удалось создать платёж в ЮKassa: {e}")

    order_id = data.get("id")
    confirmation_url = (data.get("confirmation") or {}).get("confirmation_url")
    if not order_id or not confirmation_url:
        return PaymentLinkResult(ok=False, error="ЮKassa вернула неожиданный ответ без ссылки на оплату.")

    database.record_payment(
        guild_id=guild_id, order_id=order_id, provider=PROVIDER,
        amount=str(amount), currency="RUB", period_days=period_days,
        status="pending", raw_payload=str(data),
    )
    return PaymentLinkResult(ok=True, url=confirmation_url, order_id=order_id)


async def _fetch_payment(order_id: str) -> dict | None:
    """Authoritative-запрос статуса платежа через сам API — единственная реальная
    проверка подлинности вебхука (см. докстринг файла). None — сбой сети/API,
    не "платёж не найден" (тот тоже приходит как HTTP-ошибка, raise_for_status
    её поднимет, попадёт в тот же except)."""
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(f"{API_BASE}/payments/{order_id}", auth=_auth())
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        print(f"⚠️ [Оплата] Не удалось перезапросить платёж {order_id} в ЮKassa: {e}")
        return None


@dataclass
class WebhookResult:
    ok: bool
    error: str = None
    guild_id: int = None
    already_processed: bool = False


async def handle_webhook(payload: dict) -> WebhookResult:
    """payload — распарсенное JSON-тело вебхука ЮKassa ({"type":"notification",
    "event":"payment.succeeded", "object": {...}}) — используется ТОЛЬКО чтобы
    достать id платежа (object.id), дальше решение принимается исключительно по
    authoritative-ответу _fetch_payment, не по телу этого запроса (см. докстринг
    файла). ok=False здесь означает "не подтверждено, вебхук-роут не должен
    отвечать 200" — ЮKassa сама повторит уведомление позже (до 24ч).

    Кабинет ЮKassa может быть настроен слать и другие события, кроме payment.* —
    refund.succeeded/payment_method.active несут в object.id id ВОЗВРАТА/СПОСОБА
    ОПЛАТЫ, а не платежа (наш единственный обрабатываемый случай), и попытка
    переспросить такой id как платёж (_fetch_payment) просто 404-нется. Явно
    игнорируем (ok=True, без похода в API) всё, что не начинается с "payment." —
    не ошибка, эти события нам не нужны, отвечаем 200 и не заставляем ЮKassa
    ретраить их зря."""
    event = payload.get("event") or ""
    if not event.startswith("payment."):
        return WebhookResult(ok=True)

    order_id = (payload.get("object") or {}).get("id")
    if not order_id:
        return WebhookResult(ok=False, error="В уведомлении нет id платежа.")

    payment = await _fetch_payment(order_id)
    if payment is None:
        return WebhookResult(ok=False, error="Не удалось подтвердить платёж через API ЮKassa.")

    if not payment.get("paid") or payment.get("status") != "succeeded":
        # Промежуточный (pending/waiting_for_capture) или отменённый статус —
        # не ошибка, просто ещё не повод продлевать подписку.
        return WebhookResult(ok=True)

    metadata = payment.get("metadata") or {}
    try:
        guild_id = int(metadata.get("guild_id"))
        period_days = int(metadata.get("period_days"))
    except (TypeError, ValueError):
        return WebhookResult(ok=False, error=f"Платёж {order_id} без ожидаемых metadata.guild_id/period_days.")

    if not database.mark_payment_succeeded(order_id, raw_payload=str(payment)):
        # order_id либо уже был succeeded (повторный вебхук — не продлеваем
        # второй раз), либо не найден вовсе (не должно случаться — запись
        # создаётся в build_payment_link до редиректа на оплату).
        return WebhookResult(ok=True, guild_id=guild_id, already_processed=True)

    database.extend_guild_subscription(guild_id, period_days, source="paid")
    return WebhookResult(ok=True, guild_id=guild_id)
