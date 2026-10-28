"""Тексты этапов ордера ТБ: сборка 6 сообщений «## Восход Империи — N этап» из таблицы раундов
(services/tb_plan.py::order_data), русские названия планет, сводка автозаполнения взводов.
«Ордер из картинки» (распознавание Strategy Summary моделью, /тб_ордер_из_картинки и форма на
сайте) удалён 2026-10-28 по решению пользователя — неактуален."""

import re


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
