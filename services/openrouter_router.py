"""Роутер бесплатных моделей OpenRouter: сам выбирает рабочую модель и переключается,
когда модель пропала, перегружена или отвечает мусором.

Зачем: бесплатные модели на OpenRouter живут недолго. Закреплённая minimax-m3:free
пропала к 2026-09-30 (404), и /фарм с /тб_ордер_из_картинки молча перестали работать.
Встроенный роутер OpenRouter (`openrouter/free`) не годится: однажды он отдал запрос
content-safety классификатору, который вернул "User Safety: safe" вместо JSON.

Как устроено:
- Кандидаты берутся из живого списка GET /api/v1/models (без ключа), кэш 6 ч в памяти +
  копия в bot_state на случай, если список временно недоступен. Только `:free`-модели с
  нужным вводом (картинка для vision), без классификаторов/эмбеддингов/кодовых моделей.
- Порядок: сначала модель, которая последней ответила успешно (липкость), затем
  проверенные вручную PREFERRED, затем остальные по доле успехов и размеру контекста.
- Здоровье каждой модели (таблица openrouter_model_health, общая для бота и веба):
  после сбоя модель «остывает» — 404/403/400 «нет такой модели» на сутки, 429 на 5 минут,
  5xx/таймаут на 10 минут, невалидный ответ — от 30 минут с удвоением до 12 часов.
- Ответ считается успешным, только если это JSON-объект и его принял validate()
  вызывающего кода (например, в ответе есть обязательные поля).

Суточный счётчик запросов (database.record_openrouter_request) растёт на каждый
ответ 200, даже если ответ потом отбракован: запрос уже израсходован."""

import json
import re
import time

import requests

import database

KIND_VISION = "vision"
KIND_TEXT = "text"

MODELS_URL = "https://openrouter.ai/api/v1/models"
CHAT_URL = "https://openrouter.ai/api/v1/chat/completions"

# Проверенные вручную модели — идут раньше непроверенных (но после последней успешной).
PREFERRED = {
    # 2026-09-30: 19/19 валидных сокращений ДК, ~75 с.
    KIND_TEXT: ("nvidia/nemotron-3-super-120b-a12b:free",),
    KIND_VISION: (),
}

# Подстроки id/названия, которые точно не подходят для наших задач.
_EXCLUDE_MARKERS = ("content-safety", "safety", "guard", "moderation", "embed", "code", "lyria", "tts", "whisper")

_MODELS_TTL_SECONDS = 6 * 60 * 60
_MODELS_STATE_KEY = "openrouter_free_models"

_COOLDOWN_GONE = 24 * 60 * 60
_COOLDOWN_RATE = 5 * 60
_COOLDOWN_SERVER = 10 * 60
_COOLDOWN_BAD_OUTPUT_BASE = 30 * 60
_COOLDOWN_BAD_OUTPUT_MAX = 12 * 60 * 60

_models_cache = None
_models_cached_at = 0.0


class RouterError(Exception):
    """Ни одна модель не дала годного ответа. attempts — [(model, причина), ...]."""

    def __init__(self, message, attempts):
        super().__init__(message)
        self.attempts = attempts


class _BadOutput(Exception):
    pass


# ---------------------------------------------------------------------------
# Список моделей
# ---------------------------------------------------------------------------
def _slim(model: dict) -> dict:
    arch = model.get("architecture") or {}
    return {
        "id": model["id"],
        "name": model.get("name") or model["id"],
        "input": arch.get("input_modalities") or [],
        "output": arch.get("output_modalities") or [],
        "context": model.get("context_length") or 0,
        "params": model.get("supported_parameters") or [],
        "expires": model.get("expiration_date"),
    }


def free_models(force_refresh: bool = False) -> list:
    global _models_cache, _models_cached_at
    now = time.time()
    if not force_refresh and _models_cache is not None and now - _models_cached_at < _MODELS_TTL_SECONDS:
        return _models_cache
    try:
        data = requests.get(MODELS_URL, timeout=30).json()["data"]
        models = [
            _slim(m) for m in data
            if m.get("id", "").endswith(":free")
            and str((m.get("pricing") or {}).get("prompt")) == "0"
            and str((m.get("pricing") or {}).get("completion")) == "0"
        ]
        _models_cache, _models_cached_at = models, now
        database.set_bot_state(_MODELS_STATE_KEY, json.dumps(models, ensure_ascii=False), guild_id=0)
        return models
    except Exception as e:
        print(f"⚠️ [OpenRouter] Список моделей недоступен: {e}")
        if _models_cache is not None:
            return _models_cache
        saved = database.get_bot_state(_MODELS_STATE_KEY, guild_id=0)
        _models_cache = json.loads(saved) if saved else []
        _models_cached_at = now - _MODELS_TTL_SECONDS + 10 * 60  # повторить через 10 минут
        return _models_cache


def _suitable(model: dict, kind: str) -> bool:
    ident = f"{model['id']} {model['name']}".lower()
    if any(marker in ident for marker in _EXCLUDE_MARKERS):
        return False
    if "text" not in model["output"]:
        return False
    if model.get("expires"):
        try:
            if time.strftime("%Y-%m-%d") >= str(model["expires"])[:10]:
                return False
        except Exception:
            pass
    if kind == KIND_VISION:
        return "image" in model["input"]
    return "text" in model["input"]


def ranked_candidates(kind: str) -> list:
    """[(model_dict, health_dict), ...] в порядке попыток; модели «на остывании» — в конце,
    по времени окончания остывания (если живых нет совсем, всё равно есть что попробовать)."""
    health = database.get_openrouter_model_health(kind)
    preferred = PREFERRED.get(kind, ())
    now = time.time()
    models = [m for m in free_models() if _suitable(m, kind)]

    last_ok_model = max(
        (mid for mid, h in health.items() if h["last_ok_at"]),
        key=lambda mid: health[mid]["last_ok_at"],
        default=None,
    )

    def sort_key(m):
        h = health.get(m["id"], {})
        ok, fail = h.get("ok_count") or 0, h.get("fail_count") or 0
        success_ratio = (ok + 1) / (ok + fail + 2)  # сглаженная доля успехов, новичок = 0.5
        return (
            0 if m["id"] == last_ok_model else 1,
            preferred.index(m["id"]) if m["id"] in preferred else len(preferred),
            -success_ratio,
            -m["context"],
        )

    ready, cooling = [], []
    for m in sorted(models, key=sort_key):
        h = health.get(m["id"], {})
        (cooling if (h.get("cooldown_until") or 0) > now else ready).append((m, h))
    cooling.sort(key=lambda mh: mh[1].get("cooldown_until") or 0)
    return ready + cooling


# ---------------------------------------------------------------------------
# Вызов
# ---------------------------------------------------------------------------
def _parse_json_object(text: str) -> dict:
    text = (text or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start < 0 or end <= start:
            raise _BadOutput("ответ не JSON")
        try:
            value = json.loads(text[start:end + 1])
        except json.JSONDecodeError:
            raise _BadOutput("ответ не JSON")
    if not isinstance(value, dict):
        raise _BadOutput("ответ не JSON-объект")
    return value


def _bad_output_cooldown(consecutive_fails: int) -> float:
    return min(_COOLDOWN_BAD_OUTPUT_BASE * 2 ** max(consecutive_fails, 0), _COOLDOWN_BAD_OUTPUT_MAX)


def call_json(kind: str, messages: list, api_key: str, *, validate=None, temperature: float = 0,
              timeout: int = 90, max_models: int = 4, max_bad_outputs: int = 2,
              deadline_seconds: float = 300) -> dict:
    """Синхронный вызов (из async-кода — через asyncio.to_thread). Возвращает JSON-объект
    ответа. validate(data) -> bool | str: False или строка-причина = ответ отбракован,
    пробуем следующую модель. max_bad_outputs ограничивает попытки на отбракованных
    ответах: если картинка действительно не та, не стоит жечь суточный лимит на все модели."""
    started = time.time()
    attempts = []
    bad_outputs = 0
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}

    for model, health in ranked_candidates(kind)[:max_models]:
        if time.time() - started > deadline_seconds:
            attempts.append((model["id"], "не хватило времени"))
            break
        request_json = {"model": model["id"], "messages": messages, "temperature": temperature}
        if "response_format" in model["params"] or "structured_outputs" in model["params"]:
            request_json["response_format"] = {"type": "json_object"}

        t0 = time.time()
        try:
            response = requests.post(CHAT_URL, headers=headers, json=request_json, timeout=timeout)
        except requests.RequestException as e:
            reason = f"сеть/таймаут: {type(e).__name__}"
            database.record_openrouter_model_result(model["id"], kind, False, error=reason, cooldown_seconds=_COOLDOWN_SERVER)
            attempts.append((model["id"], reason))
            continue

        status = response.status_code
        if status != 200:
            try:
                body = str(response.json()["error"]["message"])[:200]
            except Exception:
                body = response.text[:200]
            if status in (401, 402):
                # Проблема с ключом/балансом — другие модели не помогут, модель не штрафуем.
                raise RouterError(f"OpenRouter отклонил ключ ({status}): {body}", attempts + [(model["id"], str(status))])
            if status == 429:
                cooldown = _COOLDOWN_RATE
            elif status in (403, 404) or (status == 400 and "not a valid model" in body.lower()):
                # 404 — модель убрали с free; 403 — «only available on agentic harnesses»
                # (так отвечали thinkingmachines/inkling*:free 2026-09-30). Сутки не трогаем.
                cooldown = _COOLDOWN_GONE
            else:
                cooldown = _COOLDOWN_SERVER
            reason = f"HTTP {status}: {body}"
            # «Модели нет/недоступна» — свойство модели, а не задачи: ставим паузу сразу
            # для всех видов, чтобы текстовый вызов не наступал на те же грабли.
            for affected_kind in ((KIND_VISION, KIND_TEXT) if cooldown == _COOLDOWN_GONE else (kind,)):
                database.record_openrouter_model_result(model["id"], affected_kind, False, error=reason, cooldown_seconds=cooldown)
            attempts.append((model["id"], f"HTTP {status}"))
            continue

        database.record_openrouter_request()
        try:
            payload = response.json()
            if payload.get("error"):
                raise _BadOutput(f"ошибка провайдера: {str(payload['error'])[:150]}")
            content = ((payload.get("choices") or [{}])[0].get("message") or {}).get("content")
            data = _parse_json_object(content)
            verdict = validate(data) if validate else True
            if verdict is not True and verdict is not None:
                raise _BadOutput(verdict if isinstance(verdict, str) else "ответ не прошёл проверку")
        except (_BadOutput, ValueError) as e:
            bad_outputs += 1
            reason = str(e)
            database.record_openrouter_model_result(
                model["id"], kind, False, error=reason,
                cooldown_seconds=_bad_output_cooldown(health.get("consecutive_fails") or 0),
            )
            attempts.append((model["id"], reason))
            if bad_outputs >= max_bad_outputs:
                break
            continue

        database.record_openrouter_model_result(model["id"], kind, True, latency=round(time.time() - t0, 1))
        if attempts:
            print(f"🔀 [OpenRouter] {kind}: ответила {model['id']} после {len(attempts)} неудач: {attempts}")
        return data

    if not attempts:
        raise RouterError("Нет доступных бесплатных моделей OpenRouter.", attempts)
    summary = "; ".join(f"{mid.split('/')[-1]} — {why}" for mid, why in attempts)
    raise RouterError(f"Ни одна модель не справилась ({summary})", attempts)
