"""Инлайн-подсказки: «@бот дарт джа» в любом чате показывает список подходящих юнитов
(с портретами), выбор отправляет готовую команду «/unit Дарт Джа-Джа» от имени
игрока — бот её получает и отвечает, как на обычную. Это аналог автодополнения
параметров в Discord. Работает, только если в @BotFather включён инлайн-режим
(/setinline).

Первое слово запроса выбирает команду: «юнит …» (по умолчанию), «гильдия …»
(/guildunit), «готовность …» (/ready), «фракция …» (/faction). Кнопки «🔎 Найти» в
ответах бота (tg_bot/more.py::find_button) открывают сюда же с этим словом."""

from tg_bot import core, search, store
from tg_bot.api import TelegramError

RESULTS_MAX = 30  # Telegram показывает до 50; больше — только листать

# слово в начале запроса → (команда, что ищем, подсказка при пустом запросе)
MODES = {
    "юнит": ("/unit", "units", "Персонаж или корабль"),
    "unit": ("/unit", "units", "Персонаж или корабль"),
    "гильдия": ("/guildunit", "units", "У кого в гильдии есть юнит"),
    "guildunit": ("/guildunit", "units", "У кого в гильдии есть юнит"),
    "готовность": ("/ready", "guides", "Цель «Руководства»"),
    "ready": ("/ready", "guides", "Цель «Руководства»"),
    "фракция": ("/faction", "factions", "Фракция"),
    "faction": ("/faction", "factions", "Фракция"),
}


def _parse(query: str) -> tuple[str, str, str]:
    first, _, rest = query.strip().partition(" ")
    mode = MODES.get(first.casefold())
    if mode:
        return mode[0], mode[1], rest.strip()
    return "/unit", "units", query.strip()


def _article(result_id: str, title: str, description: str, command: str, thumb: str | None = None) -> dict:
    item = {
        "type": "article", "id": result_id[:64], "title": title[:100], "description": description[:200],
        "input_message_content": {"message_text": command},
    }
    if thumb:
        item.update({"thumbnail_url": thumb, "thumbnail_width": 64, "thumbnail_height": 64})
    return item


def build_results(app: core.App, query: str) -> list[dict]:
    command, kind, text = _parse(query)
    results = []
    if kind == "units":
        if not text:
            return []
        for base_id, name, ship in search.units(text, limit=RESULTS_MAX, exact_only=False):
            results.append(_article(f"u:{base_id}", name, "Корабль 🚀" if ship else "Персонаж",
                                    f"{command} {name}", f"{app.site_url}/unit-img/{base_id}"))
    elif kind == "guides":
        for g in search.guides(text, limit=RESULTS_MAX, exact_only=False):
            results.append(_article(f"g:{g['base_id']}", g["name"], g["kind_label"], f"{command} {g['name']}",
                                    f"{app.site_url}/unit-img/{g['base_id']}"))
    elif kind == "factions":
        for cid, name, count in search.factions(text, limit=RESULTS_MAX, exact_only=False):
            results.append(_article(f"f:{cid}", name, f"Персонажей: {count}", f"{command} {name}"))
    return results


async def handle_inline(app: core.App, inline_query: dict):
    store.touch_user(inline_query["from"])
    query = inline_query.get("query") or ""
    results = build_results(app, query)
    _, kind, text = _parse(query)
    params = {}
    if not results:
        hint = "Начните вводить имя персонажа" if not text else f"Ничего не найдено: «{text}»"
        params = {"button": {"text": hint, "start_parameter": "help"}}
    try:
        await app.api.call("answerInlineQuery", inline_query_id=inline_query["id"], results=results,
                           cache_time=60, is_personal=False, **params)
    except TelegramError as e:
        print(f"⚠️ [TG подсказки] «{query}»: {e.description}")


core.INLINE_HANDLER["handler"] = handle_inline
