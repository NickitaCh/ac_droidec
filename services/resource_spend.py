"""Счётчик потраченных ресурсов (деталей снаряжения + материалов реликвии) за период —
по фиче-реквесту AC Ricardo в Discord-треде ас-задачи 2026-09-21: "за неделю, за месяц и
за три месяца... даже командой в дискорде, чтоб любой игрок прописал и ему в ответ 'за
последний месяц потрачено столько-то ресов'".

Никаких новых обращений к Comlink на игрока не нужно — считает поверх УЖЕ трекаемых
guild_activity_events 'gear'/'relic'-событий (см. services/activity_diff.py, пишутся раз в
час из player_units_sync_loop) и двух статических game-data справочников:
- unit_gear_tier_recipe (per-character, из services/units_sync.py::sync_units) — какие именно
  6 деталей нужны на каждый тир снаряжения.
- relic_promotion_recipe (глобальный, из services/equipment_sync.py::sync_equipment) — сколько
  "данных сигнала" (RM_001-004), переработанных материалов "Мусорщика" (SCV_xxx) и кредитов
  (GRIND, не показывается — не "деталь") на каждый тир реликвии.

Оба рецепта детерминированы по (base_id, старый_тир, новый_тир) — реального расхода из
инвентаря игрока Comlink не отдаёт (и никогда не отдавал, в этом проекте таких данных нет
нигде), поэтому это ТОЧНЫЙ, не оценочный, расчёт: если игрок прошёл тир снаряжения/реликвии
X→Y, он гарантированно потратил ровно те детали/материалы, которые требует официальный
рецепт тира."""

from datetime import datetime, timedelta, timezone

import database

MSK = timezone(timedelta(hours=3))

PERIOD_LABELS = {
    "week": "неделю",
    "month": "месяц",
    "3months": "3 месяца",
}
# 'week' — скользящее окно (7 последних дней, календарной "недели" в игре нет). 'month'/
# '3months' — ПО КАЛЕНДАРЮ, с 1 числа (по явному запросу пользователя 2026-09-21: "считать
# по месяцам, с каждого 1 числа"), а не скользящее окно 30/90 дней — иначе "месяц" 1-го
# числа почти пустой, а 28-го показывает почти два месяца расхода.
PERIOD_WEEK_DAYS = 7
# Ингредиенты рецепта реликвии (relic_promotion_recipe) делятся на три вида по префиксу
# id: "GRIND" — кредиты (не деталь, не показывается), "RM_xxx" — "данные сигнала" (то, что
# в гильдии называют "сигналами"), "SCV_xxx" — материалы, которые можно скрафтить у
# "Мусорщика" из деталей снаряжения (см. game_scavenger_recipes/equipment_sync.py) —
# добавлено 2026-09-21 по запросу пользователя: "апнул релик — потратил не только
# сигналы, но и какие-то детали", отдельным блоком от сигналов.
SIGNAL_MATERIAL_PREFIX = "RM_"
CRAFT_MATERIAL_PREFIX = "SCV_"


def period_date_from(period: str, today: "datetime.date | None" = None) -> str:
    today = today or datetime.now(MSK).date()
    if period == "week":
        return (today - timedelta(days=PERIOD_WEEK_DAYS)).isoformat()
    if period == "3months":
        month_index = today.month - 1 - 2  # 0-based, 2 полных месяца назад + текущий
        year = today.year + month_index // 12
        month = month_index % 12 + 1
        return today.replace(year=year, month=month, day=1).isoformat()
    return today.replace(day=1).isoformat()  # "month" — с 1 числа текущего месяца


def _gear_tier_pairs(events: list) -> set[tuple[str, int]]:
    """events: [(base_id, old_value, new_value), ...] gear-событий. Возвращает набор
    (base_id, тир) на каждый ПРОЙДЕННЫЙ тир (старый+1..новый включительно) — если игрок
    прыгнул через несколько тиров между синками активности, считаются все промежуточные."""
    pairs = set()
    for base_id, old_value, new_value in events:
        try:
            old_tier, new_tier = int(old_value), int(new_value)
        except (TypeError, ValueError):
            continue
        for tier in range(old_tier + 1, new_tier + 1):
            pairs.add((base_id, tier))
    return pairs


def gear_pieces_spent(guild_id: int, ally_code: str, date_from: str, date_to: str | None = None) -> dict:
    """Возвращает {"total": int, "upgrades": int, "by_equipment": {equipment_id: qty}} —
    сколько отдельных деталей снаряжения потрачено на все пройденные за период тиры
    (все персонажи игрока разом). upgrades — число событий-апгрейдов (для контекста в
    отчёте), не равно total (один апгрейд = 6 деталей, если тир пройден за раз, больше —
    если пропущено несколько тиров между синками)."""
    events = database.get_guild_activity_events(
        guild_id, ally_code=ally_code, action_type="gear",
        date_from=date_from, date_to=date_to, limit=10000,
    )
    triples = [(base_id, old_value, new_value) for _ally, base_id, _action, old_value, new_value, _date, _scraped in events]
    pairs = _gear_tier_pairs(triples)
    recipe_items = database.get_gear_recipe_items(list(pairs))

    by_equipment: dict[str, int] = {}
    for equipment_ids in recipe_items.values():
        for equipment_id in equipment_ids:
            by_equipment[equipment_id] = by_equipment.get(equipment_id, 0) + 1

    return {
        "total": sum(by_equipment.values()),
        "upgrades": len(triples),
        "by_equipment": by_equipment,
    }


def relic_materials_spent(guild_id: int, ally_code: str, date_from: str, date_to: str | None = None) -> dict:
    """Возвращает {"upgrades": int, "signals": {"total", "by_material"}, "craft":
    {"total", "by_material"}} — "данные сигнала" (RM_001-004) и отдельным блоком
    переработанные материалы "Мусорщика" (SCV_xxx), потраченные на все пройденные за
    период тиры реликвии (все персонажи игрока разом). Тиры реликвии — общая для всех
    персонажей шкала (в отличие от снаряжения), поэтому один и тот же диапазон тиров у
    разных персонажей стоит одинаково — суммируется напрямую, без привязки к base_id.
    Один проход по событиям и рецептам на оба блока — не дублирует запросы."""
    events = database.get_guild_activity_events(
        guild_id, ally_code=ally_code, action_type="relic",
        date_from=date_from, date_to=date_to, limit=10000,
    )
    by_signal: dict[str, int] = {}
    by_craft: dict[str, int] = {}
    upgrades = 0
    for _ally, _base_id, _action, old_value, new_value, _date, _scraped in events:
        try:
            old_tier, new_tier = int(old_value), int(new_value)
        except (TypeError, ValueError):
            continue
        if new_tier <= old_tier:
            continue
        upgrades += 1
        ingredients = database.get_relic_promotion_recipe_range(old_tier, new_tier)
        for material_id, qty in ingredients.items():
            if material_id.startswith(SIGNAL_MATERIAL_PREFIX):
                by_signal[material_id] = by_signal.get(material_id, 0) + qty
            elif material_id.startswith(CRAFT_MATERIAL_PREFIX):
                by_craft[material_id] = by_craft.get(material_id, 0) + qty

    return {
        "upgrades": upgrades,
        "signals": {"total": sum(by_signal.values()), "by_material": by_signal},
        "craft": {"total": sum(by_craft.values()), "by_material": by_craft},
    }


def build_report(guild_id: int, ally_code: str, period: str) -> dict:
    """{"period_label", "date_from", "gear", "relic"} — сводка для /ресурсы и веб-паритета
    (/activity/resources). relic — см. relic_materials_spent (signals+craft)."""
    date_from = period_date_from(period)
    return {
        "period_label": PERIOD_LABELS.get(period, PERIOD_LABELS["month"]),
        "date_from": date_from,
        "gear": gear_pieces_spent(guild_id, ally_code, date_from),
        "relic": relic_materials_spent(guild_id, ally_code, date_from),
    }


def build_guild_report(guild_id: int, period: str) -> list[dict]:
    """[{"ally_code", "name", "gear", "relic"}, ...] — для веб-страницы
    /activity/resources, все зарегистрированные игроки гильдии разом, отсортировано по
    общей сумме (деталей+сигналов+крафт-материалов) по убыванию. gear/relic — те же
    структуры, что возвращают gear_pieces_spent/relic_materials_spent (полные
    by_equipment/by_material внутри — нужны для попапа с полным списком на клике по
    игроку, см. web/templates/activity_resources.html). N отдельных запросов (по одному
    на игрока) — тот же приемлемый паттерн, что уже используют другие гильдийские отчёты
    в этом проекте (напр. _build_guild_report в cogs/stat_requirements.py)."""
    date_from = period_date_from(period)
    rows = []
    for _discord_id, ally_code, name in database.get_all_user_mappings(guild_id):
        gear = gear_pieces_spent(guild_id, ally_code, date_from)
        relic = relic_materials_spent(guild_id, ally_code, date_from)
        rows.append({"ally_code": ally_code, "name": name, "gear": gear, "relic": relic})
    rows.sort(key=lambda r: r["gear"]["total"] + r["relic"]["signals"]["total"] + r["relic"]["craft"]["total"], reverse=True)
    return rows
