"""Ордер ТБ на 6 этапов из картинки «Strategy Summary» — общая часть бота
(/тб_ордер_из_картинки, cogs/tb_order_image.py) и сайта (/tb/order-plans): промпт для
vision-модели, русские названия планет, сборка текстов этапов. Плюс веб-версия всего
процесса через Discord REST (у веба нет шлюза disnake) — run_web_job."""

import asyncio
import json
import os
import re
from datetime import datetime, timezone

import httpx

import database

ORDER_HEADER_RE = re.compile(r"^##\s.+—\s*(\d+)\s*этап", re.MULTILINE)

ZONE_EMOJI = {"dark": "🔴", "mixed": "🟡", "light": "🔵", "bonus": "⚪"}

# Известные планеты "Восхода Империи" — русские названия сверены с TB_PLANET_CONFLICT
# в cogs/guild_events.py (для планет, которые там уже есть) плюс несколько новых.
PLANET_RU = {
    "mustafar": "Мустафар",
    "geonosis": "Джеонозис",
    "dathomir": "Датомир",
    "haven-class medical station": "Медстанция",
    "medical station": "Медстанция",
    "corellia": "Кореллия",
    "felucia": "Фелуция",
    "tatooine": "Татуин",
    "kessel": "Кессель",
    "vandor": "Вандор",
    "coruscant": "Корусант",
    "bracca": "Бракка",
    "kashyyyk": "Кашик",
    "lothal": "Лотал",
    "ring of kafrene": "Кольцо Кафрены",
    "scarif": "Скариф",
    "zeffo": "Зеффо",
    "mandalore": "Мандалор",
    "malachor": "Малахор",
}

PROMPT = """На картинке — снимок экрана вкладки "Strategy Summary" планировщика ТБ (Star Wars:
Galaxy of Heroes, "Восход Империи"). Таблица: строки Dark/Mixed/Light/Bonus,
колонки Round 1..Round 6.

В каждой заполненной ячейке — название планеты сверху (например "R5: Mustafar" —
верни только "Mustafar", без префикса "R#:"), и под ним три строки чисел. Нужно
только:
1. Название планеты (английское, как написано, без префикса "R#:").
2. Итоговый рейтинг ★ в правой нижней части ячейки (0-3 звезды). Если там пусто,
   стоит только число без иконок звёзд, или ячейка пустая — это 0 звёзд.
Всё остальное (проценты, "M"-значения, "Sandbagged"/"Mission Req" внизу) не нужно
и должно игнорироваться.

Для строки Bonus: если в колонке стоит "???" — зона ещё не открыта, верни для неё
пустой список. Если в колонке одна или несколько планет (может быть до 2, одна
под другой) — верни все, сверху вниз.

Верни СТРОГО JSON без markdown-обрамления (без ```), ровно такой структуры:
{
  "total_stars_header": <число из заголовка "Total Stars: N">,
  "rounds": [
    {
      "round": 1,
      "dark": {"planet": "...", "stars": 0} или null,
      "mixed": {"planet": "...", "stars": 0} или null,
      "light": {"planet": "...", "stars": 0} или null,
      "bonus": [{"planet": "...", "stars": 0}]
    }
  ]
}
Массив "rounds" должен содержать ровно 6 элементов (round: 1..6)."""


def _translate_planet(name: str) -> str:
    key = (name or "").strip().lower()
    key = re.sub(r"^r\d+:\s*", "", key)
    return PLANET_RU.get(key, (name or "?").strip())


def _stars_word(n: int) -> str:
    if n <= 0:
        return "0 звёзд"
    if n == 1:
        return "1 звезда"
    return f"{n} звезды"


def _overall_contribution(zone: str, stars: int) -> int:
    # Подтверждено пользователем: бонусная планета даёт 1 звезду в общий зачёт
    # только при полных 3★ на ней, иначе 0 (1-2★ там — другие награды).
    if zone == "bonus":
        return 1 if stars >= 3 else 0
    return max(0, stars)


def validate_response(data: dict):
    """Для роутера (services/openrouter_router.py): ответ, из которого не собираются 6
    этапов, отбраковывается — пробуем другую модель (не больше 2 отбракованных ответов,
    чтобы не жечь суточный лимит, если картинка и правда не та)."""
    try:
        blocks, _total = build_order_blocks(data)
    except Exception as e:
        return f"не разобрать этапы: {e}"
    return True if len(blocks) == 6 else f"этапов {len(blocks)} вместо 6"


def build_order_blocks(data: dict):
    """Возвращает (список из 6 текстов этапов, сумма звёзд в общий зачёт)."""
    rounds = data.get("rounds") or []
    blocks = []
    total = 0
    for round_data in rounds:
        phase = round_data.get("round")
        entries = []
        for zone in ("dark", "mixed", "light"):
            cell = round_data.get(zone)
            if cell and cell.get("planet"):
                entries.append((zone, cell.get("planet"), int(cell.get("stars") or 0), cell))
        for cell in round_data.get("bonus") or []:
            if cell and cell.get("planet"):
                entries.append(("bonus", cell.get("planet"), int(cell.get("stars") or 0), cell))

        active = []
        for zone, planet, stars, cell in entries:
            total += _overall_contribution(zone, stars)
            if phase == 6 and stars <= 0:
                continue
            active.append((zone, planet, stars, cell))

        lines = [f"## Восход Империи — {phase} этап"]
        if not active:
            lines.append("> (нет активных планет на этом этапе)")
        for i, (zone, planet, stars, cell) in enumerate(active):
            preload = cell.get("preload")
            if cell.get("no_platoons"):  # план с сайта: на этой планете этапа взводы не ставим
                vzvod = "нет"
            elif stars > 0:
                vzvod = "все"
            elif zone == "bonus":
                vzvod = "только по боту"
            else:
                vzvod = "по боту"
            lines.append(f"> {ZONE_EMOJI[zone]} **{_translate_planet(planet)}**")
            lines.append(f"> **Цель:** {_stars_word(stars)}")
            if stars <= 0 and preload:  # план с сайта (services/tb_plan.py): до скольких копим
                lines.append(f"> **Копим:** до {preload / 1e6:.0f} млн")
            lines.append(f"> **Взводы:** {vzvod}")
            lines.append("> **БЗ:** обязательно")
            if i != len(active) - 1:
                lines.append("> ")
                lines.append("> ")
        blocks.append("\n".join(lines))
    return blocks, total




AUTOFILL_REASON_LABELS = {
    "no_owner": "нет владельцев", "no_eligible_owner": "нет подходящих доноров",
    "unit_excluded": "юнит исключён фильтром", "unit_not_resolved": "юнит не распознан",
}


def summarize_autofill(result) -> str:
    """Строка «Взводы подготовлены: N/M …» по результату tb_platoon_autofill.autofill_plan."""
    reasons = {}
    for outcome in result.unfilled:
        if outcome.reason == "held_back":
            continue
        reasons[outcome.reason] = reasons.get(outcome.reason, 0) + 1
    note = f"🧩 Взводы подготовлены: {result.filled_slots}/{result.total_slots} слотов занято"
    if result.held_back:
        note += f", {result.held_back} намеренно не добито («держим»)"
    if reasons:
        note += " — не хватает (" + ", ".join(f"{AUTOFILL_REASON_LABELS.get(r, r)}: {c}" for r, c in reasons.items()) + ")"
    return note


# ---------------------------------------------------------------------------
# Веб-версия: тот же процесс через Discord REST, в фоне (распознавание идёт до
# минуты и дольше — HTTP-запрос столько не ждёт). Статус — в bot_state.
# ---------------------------------------------------------------------------

DISCORD_API = "https://discord.com/api/v10"
JOB_KEY = "tb_order_image_job"
WARNING_RATIO = 0.9
_LINK_RE = re.compile(r"discord(?:app)?\.com/channels/(\d+|@me)/(\d+)")
_IMAGE_EXT = (".png", ".jpg", ".jpeg", ".webp", ".gif")


class OrderImageError(Exception):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def get_job(guild_id: int) -> dict | None:
    raw = database.get_bot_state(JOB_KEY, guild_id=guild_id)
    try:
        return json.loads(raw) if raw else None
    except ValueError:
        return None


def _set_job(guild_id: int, **fields) -> None:
    job = get_job(guild_id) or {}
    job.update(fields, updated_at=_now())
    database.set_bot_state(JOB_KEY, json.dumps(job, ensure_ascii=False), guild_id=guild_id)


def start_job(guild_id: int, plan_name: str) -> bool:
    """False — у гильдии уже идёт распознавание (второе параллельно не запускаем)."""
    job = get_job(guild_id)
    if job and job.get("state") == "running":
        updated = datetime.strptime(job["updated_at"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        # Подвисшая задача (веб перезапустили посреди работы) не блокирует навсегда.
        if (datetime.now(timezone.utc) - updated).total_seconds() < 600:
            return False
    database.set_bot_state(JOB_KEY, json.dumps({"state": "running", "plan_name": plan_name, "step": "Читаю тред…",
                                                "updated_at": _now()}, ensure_ascii=False), guild_id=guild_id)
    return True


def _thread_id_from_link(link: str) -> int | None:
    link = (link or "").strip()
    m = _LINK_RE.search(link)
    if m:
        return int(m.group(2))
    return int(link) if link.isdigit() else None


async def _bot_order_phases(client: httpx.AsyncClient, thread_id: int, bot_id: str) -> list:
    """Номера этапов, уже опубликованных ботом в треде (последние 200 сообщений)."""
    phases, before = set(), None
    for _ in range(2):
        params = {"limit": 100}
        if before:
            params["before"] = before
        resp = await client.get(f"{DISCORD_API}/channels/{thread_id}/messages", params=params)
        resp.raise_for_status()
        batch = resp.json()
        for msg in batch:
            if msg.get("author", {}).get("id") != bot_id:
                continue
            m = ORDER_HEADER_RE.match(msg.get("content") or "")
            if m:
                phases.add(int(m.group(1)))
        if len(batch) < 100:
            break
        before = batch[-1]["id"]
    return sorted(phases)


async def run_web_job(guild_id: int, link: str, plan_name: str, stars: int, force: bool, created_by: str) -> None:
    try:
        message = await _run(guild_id, link, plan_name, stars, force, created_by)
        _set_job(guild_id, state="done", message=message)
    except OrderImageError as e:
        _set_job(guild_id, state="error", message=str(e))
    except Exception as e:
        _set_job(guild_id, state="error", message=f"Непредвиденная ошибка: {e}")
        print(f"❌ [web/tb_order_image] guild {guild_id}: {e!r}")


async def _run(guild_id: int, link: str, plan_name: str, stars: int, force: bool, created_by: str) -> str:
    from services.openrouter_vision import call_vision_json, daily_used_ratio, OPENROUTER_DAILY_REQUEST_LIMIT
    import tb_platoon_autofill

    api_key = os.getenv("OPENROUTER_API_KEY")
    if not api_key:
        raise OrderImageError("OPENROUTER_API_KEY не настроен на сервере.")
    if daily_used_ratio(OPENROUTER_DAILY_REQUEST_LIMIT) >= WARNING_RATIO:
        raise OrderImageError("Почти исчерпан дневной лимит распознавания картинок (~90%). "
                              "Лимит сбрасывается в начале следующих суток (UTC).")
    thread_id = _thread_id_from_link(link)
    if thread_id is None:
        raise OrderImageError("Не удалось распознать ссылку на тред.")
    token = os.environ.get("DISCORD_TOKEN")
    if not token:
        raise OrderImageError("DISCORD_TOKEN не настроен на сервере.")
    headers = {"Authorization": f"Bot {token}", "User-Agent": "DiscordBot (https://swgoh-sng.ru, 1.0)"}

    async with httpx.AsyncClient(headers=headers, timeout=30) as client:
        me = await client.get(f"{DISCORD_API}/users/@me")
        me.raise_for_status()
        bot_id = me.json()["id"]
        resp = await client.get(f"{DISCORD_API}/channels/{thread_id}")
        if resp.status_code in (403, 404):
            raise OrderImageError("Тред не найден или у бота нет к нему доступа — проверьте ссылку.")
        resp.raise_for_status()
        thread_name = resp.json().get("name") or str(thread_id)

        resp = await client.get(f"{DISCORD_API}/channels/{thread_id}/messages", params={"after": 0, "limit": 1})
        resp.raise_for_status()
        first = resp.json()
        if not first:
            raise OrderImageError("В треде нет сообщений.")
        image = next((a for a in first[0].get("attachments") or []
                      if (a.get("content_type") or "").startswith("image/")
                      or (a.get("filename") or "").lower().endswith(_IMAGE_EXT)), None)
        if image is None:
            raise OrderImageError("В первом сообщении треда нет картинки.")

        if not force:
            existing = await _bot_order_phases(client, thread_id, bot_id)
            if existing:
                raise OrderImageError(
                    f"В треде уже есть ордер для этапов: {', '.join(map(str, existing))}. Повторный запуск создаст "
                    "дубли — если это осознанно (план поменялся), отметьте «Опубликовать заново».")

        _set_job(guild_id, step="Распознаю картинку (до пары минут)…")
        img = await client.get(image["url"])
        img.raise_for_status()
        mime = (image.get("content_type") or "image/png").split(";")[0].strip()
        data = await asyncio.to_thread(call_vision_json, img.content, mime, api_key, PROMPT, validate_response)
        try:
            blocks, total = build_order_blocks(data)
        except Exception as e:
            raise OrderImageError(f"Не удалось разобрать ответ модели: {e}")
        if len(blocks) != 6:
            raise OrderImageError(f"Распознано {len(blocks)} этапов вместо 6 — проверьте картинку, ничего не публикую.")

        _set_job(guild_id, step="Публикую этапы в тред…")
        for block in blocks:
            resp = await client.post(f"{DISCORD_API}/channels/{thread_id}/messages",
                                     json={"content": block, "allowed_mentions": {"parse": []}})
            if resp.status_code >= 400:
                raise OrderImageError(f"Публикация прервалась (не всё могло уйти в тред): Discord {resp.status_code}. "
                                      "План не сохранён — проверьте тред и при необходимости повторите с «Опубликовать заново».")
        posted = await _bot_order_phases(client, thread_id, bot_id)

    if posted != [1, 2, 3, 4, 5, 6]:
        missing = sorted(set(range(1, 7)) - set(posted))
        raise OrderImageError(f"Сообщения отправлены, но в треде не нашлись этапы: {', '.join(map(str, missing))}. "
                              "План не сохранён — проверьте тред вручную.")

    plan_id = database.save_tb_plan(guild_id, plan_name, thread_id, total, created_by=created_by)
    parts = []
    if total != stars:
        parts.append(f"⚠️ По распознанным данным сумма звёзд = {total}, а указано {stars}. Проверьте текст в треде.")
    parts.append(f"✅ Опубликовано 6 этапов в тред «{thread_name}». План сохранён как «{plan_name}» ({total} ★).")
    try:
        parts.append(summarize_autofill(await tb_platoon_autofill.autofill_plan(guild_id, plan_id)) + " — /tb/platoons.")
    except (ValueError, RuntimeError) as e:
        parts.append(f"⚠️ Не удалось автоматически расставить взводы: {e}. Расставьте вручную на /tb/platoons.")
    return "\n".join(parts)
