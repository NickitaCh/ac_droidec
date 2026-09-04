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
import time

import requests

import database

OPENROUTER_VISION_MODEL = "minimax/minimax-m3:free"
OPENROUTER_DAILY_REQUEST_LIMIT = 50

# Повтор при 429 — подтверждено вживую 2026-09-04: реальный запрос от пользователя словил
# 429, но диагностический перезапрос буквально через минуту (тем же ключом/моделью, и без
# картинки, и с ней) прошёл 200 без изменений с нашей стороны — это разовая перегрузка
# бесплатного shared-пула провайдера (в тот раз ответ пришёл через 'GMICloud'), не наш
# суточный лимит (счётчик в БД был всего 2 из 50). OpenRouter не всегда шлёт Retry-After на
# 429 — если есть, используем его (с потолком, чтобы не морозить ephemeral-ответ надолго),
# если нет — фиксированный бэкофф.
_RETRY_BACKOFF_SECONDS = [2, 5]  # пауза перед 2-й и 3-й попыткой (итого до 3 попыток)
_RETRY_AFTER_CAP_SECONDS = 10


def call_vision_json(image_bytes: bytes, mime_type: str, api_key: str, prompt: str) -> dict:
    import base64

    b64 = base64.b64encode(image_bytes).decode()
    request_json = {
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
    }
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}

    response = None
    for attempt in range(len(_RETRY_BACKOFF_SECONDS) + 1):
        response = requests.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers=headers,
            json=request_json,
            timeout=60,
        )
        if response.status_code != 429 or attempt == len(_RETRY_BACKOFF_SECONDS):
            break
        wait_seconds = _RETRY_BACKOFF_SECONDS[attempt]
        retry_after = response.headers.get("Retry-After")
        if retry_after:
            try:
                wait_seconds = min(float(retry_after), _RETRY_AFTER_CAP_SECONDS)
            except ValueError:
                pass
        time.sleep(wait_seconds)

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
