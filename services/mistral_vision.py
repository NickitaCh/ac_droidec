"""Общий вызов Mistral vision (chat completions с картинкой, ответ строго JSON) —
использовался раздельно в cogs/tb_order_image.py и cogs/gear_farm.py, вынесен сюда, когда
понадобилось единое место учёта расхода (см. call_vision_json ниже) — если бы каждый cog
считал токены сам, легко забыть добавить это в один из них при следующей vision-команде.

Официального usage/billing API у Mistral на Free-тарифе нет (их Admin API требует
Enterprise Backoffice — проверено вживую при реализации этой фичи, backoffice.mistral.ai
на Free-аккаунте редиректит в обычную консоль, а обычный ключ на /v1/admin/* отвечает 401).
Поэтому расход считаем сами по полю usage в каждом ответе и сравниваем со своей оценкой
месячного бюджета (database.record_mistral_usage/get_mistral_usage_this_month)."""

import base64
import json
import re

import requests

import database

MISTRAL_VISION_MODEL = "mistral-medium-latest"

# Актуальные цены mistral-medium-latest — подтверждены сентябрь 2026 (сторонние
# price-tracking сайты; у Mistral нет публичного API для чтения своей же прайс-таблицы).
# Могут измениться — при заметном расхождении с реальным счётом Mistral (Billing в
# console.mistral.ai) перепроверить и обновить.
MISTRAL_INPUT_PRICE_PER_M_USD = 1.50
MISTRAL_OUTPUT_PRICE_PER_M_USD = 7.50


def call_vision_json(image_bytes: bytes, mime_type: str, api_key: str, prompt: str) -> dict:
    b64 = base64.b64encode(image_bytes).decode()
    response = requests.post(
        "https://api.mistral.ai/v1/chat/completions",
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json={
            "model": MISTRAL_VISION_MODEL,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {"type": "image_url", "image_url": f"data:{mime_type};base64,{b64}"},
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

    # usage.prompt_tokens/completion_tokens — подтверждено официальной схемой ответа
    # (models.UsageInfo), не угадано. Пишем даже если поле вдруг отсутствует (0/0) —
    # не роняем распознавание картинки из-за учёта расхода.
    usage = payload.get("usage") or {}
    database.record_mistral_usage(usage.get("prompt_tokens") or 0, usage.get("completion_tokens") or 0)

    text = payload["choices"][0]["message"]["content"].strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip())
    return json.loads(text)


def estimate_cost_usd(input_tokens: int, output_tokens: int) -> float:
    return (input_tokens / 1_000_000) * MISTRAL_INPUT_PRICE_PER_M_USD + \
           (output_tokens / 1_000_000) * MISTRAL_OUTPUT_PRICE_PER_M_USD


def budget_used_ratio(monthly_budget_usd: float) -> float:
    """Доля исчерпанного месячного бюджета по нашей собственной оценке (0.0 и выше,
    может превысить 1.0). Не официальный биллинг Mistral — см. докстринг модуля."""
    if monthly_budget_usd <= 0:
        return 0.0
    input_tokens, output_tokens = database.get_mistral_usage_this_month()
    return estimate_cost_usd(input_tokens, output_tokens) / monthly_budget_usd
