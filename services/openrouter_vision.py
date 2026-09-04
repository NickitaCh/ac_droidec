"""Общий вызов OpenRouter vision — заменил Mistral (services/mistral_vision.py, оставлен
в проекте на будущее, вдруг снова заработает) после того, как выяснилось: с сентября
2026 обычный API-ключ Mistral на Free-тарифе даёт 0 запросов/мин без включённого
Pay-As-You-Go (проверено вживую, x-ratelimit-limit-req-minute: 0 на каждом запросе) —
раньше (август 2026) работало без этого требования, что-то изменилось на их стороне.

Проверено вживую с этого VPS перед переключением (не угадано):
- OpenRouter технически доступен с этого хостинга (в отличие от Gemini/Groq — оба
  заблокированы по IP датацентра/сети, см. предыдущее обсуждение) — HTTP 200 с реальным
  vision-ответом.
- Не привязка карты не нужна для free-моделей (`sk-or-v1-...` ключ создаётся без биллинга).
- Роутер `openrouter/free` (авто-выбор среди ~24 бесплатных моделей) НЕНАДЁЖЕН для наших
  задач — на 3 тестовых вызова один раз попал на content-safety классификатор
  ('nvidia/nemotron-3.5-content-safety:free'), который просто ответил "User Safety: safe"
  и проигнорировал реальный запрос, JSON не распарсился. Поэтому здесь модель ЗАКРЕПЛЕНА
  (не роутер) — 'minimax/minimax-m3:free', подтверждена 2/2 стабильных валidных JSON-ответа
  подряд с картинкой + response_format=json_object.

Бесплатный лимит OpenRouter без покупки кредитов (их документация, не угадано):
20 запросов/мин, 50 запросов/сутки — здесь считаем расход по СУТКАМ (не по деньгам, как
у Mistral) через database.record_openrouter_request/get_openrouter_requests_today."""

import json
import re

import requests

import database

OPENROUTER_VISION_MODEL = "minimax/minimax-m3:free"
OPENROUTER_DAILY_REQUEST_LIMIT = 50


def call_vision_json(image_bytes: bytes, mime_type: str, api_key: str, prompt: str) -> dict:
    import base64

    b64 = base64.b64encode(image_bytes).decode()
    response = requests.post(
        "https://openrouter.ai/api/v1/chat/completions",
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json={
            "model": OPENROUTER_VISION_MODEL,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {"type": "image_url", "image_url": {"url": f"data:{mime_type};base64,{b64}"}},
                    ],
                }
            ],
            "response_format": {"type": "json_object"},
            "temperature": 0,
        },
        timeout=60,
    )
    response.raise_for_status()
    payload = response.json()

    database.record_openrouter_request()

    text = payload["choices"][0]["message"]["content"].strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip())
    return json.loads(text)


def daily_used_ratio(daily_limit: int) -> float:
    """Доля исчерпанного дневного лимита запросов (0.0 и выше)."""
    if daily_limit <= 0:
        return 0.0
    return database.get_openrouter_requests_today() / daily_limit
