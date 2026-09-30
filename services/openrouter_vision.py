"""Общий вызов OpenRouter (vision и текст) — заменил Mistral (services/mistral_vision.py,
оставлен в проекте на будущее, вдруг снова заработает) после того, как выяснилось: с сентября
2026 обычный API-ключ Mistral на Free-тарифе даёт 0 запросов/мин без включённого
Pay-As-You-Go (проверено вживую, x-ratelimit-limit-req-minute: 0 на каждом запросе).

Проверено вживую с этого VPS (не угадано):
- OpenRouter доступен с этого хостинга (в отличие от Gemini/Groq — заблокированы по IP
  датацентра), привязка карты для free-моделей не нужна.
- Модель больше НЕ закреплена: закреплённая minimax/minimax-m3:free пропала к 2026-09-30
  (404), и /фарм с /тб_ордер_из_картинки перестали работать. Выбор и переключение модели —
  services/openrouter_router.py.

Бесплатный лимит OpenRouter без покупки кредитов (их документация): 20 запросов/мин,
50 запросов/сутки — считаем по СУТКАМ через database.record_openrouter_request/
get_openrouter_requests_today (счёт ведёт роутер)."""

import base64

import database
from services import openrouter_router

OPENROUTER_DAILY_REQUEST_LIMIT = 50


def call_vision_json(image_bytes: bytes, mime_type: str, api_key: str, prompt: str, validate=None) -> dict:
    """validate(data) -> True | False | "причина" — отбраковать ответ и попробовать другую
    модель (например, в ответе нет обязательных полей)."""
    b64 = base64.b64encode(image_bytes).decode()
    messages = [{
        "role": "user",
        "content": [
            {"type": "text", "text": prompt},
            {"type": "image_url", "image_url": {"url": f"data:{mime_type};base64,{b64}"}},
        ],
    }]
    return openrouter_router.call_json(
        openrouter_router.KIND_VISION, messages, api_key, validate=validate, temperature=0, timeout=90,
    )


def call_text_json(prompt: str, api_key: str, timeout: int = 150, validate=None) -> dict:
    """Текстовые задачи (напр. черновики сокращений бонусов ДК, services/datacron_shorts.py)."""
    return openrouter_router.call_json(
        openrouter_router.KIND_TEXT, [{"role": "user", "content": prompt}], api_key,
        validate=validate, temperature=0.2, timeout=timeout,
    )


def daily_used_ratio(daily_limit: int) -> float:
    """Доля исчерпанного дневного лимита запросов (0.0 и выше)."""
    if daily_limit <= 0:
        return 0.0
    return database.get_openrouter_requests_today() / daily_limit
