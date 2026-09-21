"""Счётчик потраченных ресурсов (деталей снаряжения + "сигналов"/материалов реликвии) за
период — по фиче-реквесту AC Ricardo в Discord-треде ас-задачи 2026-09-21: "за неделю, за
месяц и за три месяца... даже командой в дискорде, чтоб любой игрок прописал и ему в ответ
'за последний месяц потрачено столько-то ресов'".

Никаких новых обращений к Comlink на игрока не нужно — считает поверх УЖЕ трекаемых
guild_activity_events 'gear'/'relic'-событий (см. services/activity_diff.py, пишутся раз в
час из player_units_sync_loop) и двух статических game-data справочников:
- unit_gear_tier_recipe (per-character, из services/units_sync.py::sync_units) — какие именно
  6 деталей нужны на каждый тир снаряжения.
- relic_promotion_recipe (глобальный, из services/equipment_sync.py::sync_equipment) — сколько
  "данных сигнала" (RM_001-004)/кредитов/переработанных материалов на каждый тир реликвии.

Оба рецепта детерминированы по (base_id, старый_тир, новый_тир) — реального расхода из
инвентаря игрока Comlink не отдаёт (и никогда не отдавал, в этом проекте таких данных нет
нигде), поэтому это ТОЧНЫЙ, не оценочный, расчёт: если игрок прошёл тир снаряжения X→Y,
он гарантированно потратил ровно те детали, которые требует официальный рецепт тира."""

from datetime import datetime, timedelta, timezone

import database

MSK = timezone(timedelta(hours=3))

PERIOD_LABELS = {
    "week": "неделю",
    "month": "месяц",
    "3months": "3 месяца",
}
PERIOD_DAYS = {
    "week": 7,
    "month": 30,
    "3months": 90,
}
# 'RM_'-префикс — "данные сигнала" (см. CLAUDE.md/services/equipment_sync.py) — то, что в
# гильдии называют "сигналами". Остальные ингредиенты рецепта реликвии (GRIND=кредиты,
# SCV_xxx=переработанные материалы) не показываются в /ресурсы — запрос был именно про
# детальки+сигналы, не про полный расход.
SIGNAL_MATERIAL_PREFIX = "RM_"


def period_date_from(period: str, today: "datetime.date | None" = None) -> str:
    today = today or datetime.now(MSK).date()
    days = PERIOD_DAYS.get(period, PERIOD_DAYS["month"])
    return (today - timedelta(days=days)).isoformat()


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


def signals_spent(guild_id: int, ally_code: str, date_from: str, date_to: str | None = None) -> dict:
    """Возвращает {"total": int, "upgrades": int, "by_material": {material_id: qty}} —
    "данные сигнала" (RM_001-004), потраченные на все пройденные за период тиры реликвии
    (все персонажи игрока разом). Тиры реликвии — общая для всех персонажей шкала (в
    отличие от снаряжения), поэтому один и тот же диапазон тиров у разных персонажей стоит
    одинаково — суммируется напрямую, без привязки к base_id."""
    events = database.get_guild_activity_events(
        guild_id, ally_code=ally_code, action_type="relic",
        date_from=date_from, date_to=date_to, limit=10000,
    )
    by_material: dict[str, int] = {}
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
            if not material_id.startswith(SIGNAL_MATERIAL_PREFIX):
                continue
            by_material[material_id] = by_material.get(material_id, 0) + qty

    return {
        "total": sum(by_material.values()),
        "upgrades": upgrades,
        "by_material": by_material,
    }


def build_report(guild_id: int, ally_code: str, period: str) -> dict:
    """{"period_label", "date_from", "gear", "signals"} — сводка для /ресурсы и, при
    необходимости, веб-паритета (см. /activity/players)."""
    date_from = period_date_from(period)
    return {
        "period_label": PERIOD_LABELS.get(period, PERIOD_LABELS["month"]),
        "date_from": date_from,
        "gear": gear_pieces_spent(guild_id, ally_code, date_from),
        "signals": signals_spent(guild_id, ally_code, date_from),
    }
