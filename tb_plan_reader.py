# Чтение "какая планета на каком этапе" из опубликованного в Discord треда плана ТБ —
# вынесено из web/routes/guild_dashboard.py 2026-08-29, когда та же логика понадобилась
# tb_platoon_autofill.py (который запускается и с веб-роута, и из cogs/tb_order_image.py —
# см. план "Автозаполнение взводов ТБ + фильтры"). Раньше жило только в web-процессе с
# явным комментарием про "развязку от cogs/*" (см. история этого файла) — само чтение через
# bot-token REST, а не через живой bot-инстанс, делает функцию одинаково рабочей что из
# веб-процесса, что из процесса бота, так что дублировать её ещё раз для cogs/ не нужно.
#
# План хранит только discord thread_id (см. cogs/tb_order_image.py) — самого разбора
# "какая планета на каком этапе" нигде в БД не остаётся, только опубликованные ботом
# сообщения-блоки в этом треде ("## Восход Империи — N этап" + по кружку-эмодзи на
# планету). Поэтому эти сообщения перечитываются напрямую через Discord REST при каждом
# использовании.
import os
import re

import httpx

_TB_PLAN_HEADER_RE = re.compile(r"Восход\s+Импери\w*\s*[—\-]\s*(\d+)\s*этап", re.IGNORECASE)
_TB_PLAN_CIRCLE_CHARS = "🔴🟠🟡🟢🔵🟣⚫⚪"

# Русские названия планет, которые публикует cogs/tb_order_image.py::_translate_planet —
# обратный словарь к его PLANET_RU, ключи которого — названия из tb_platoon_data.py.
# Death Star/Hoth — стандартные локализации, НЕ подтверждены живым постом бота (эта
# гильдия пока не доходила до 6 этапа в реальном прогрессе) — если появятся в реальном
# ордере под другим названием, поправить здесь.
PLANET_NAME_TO_RU = {
    "Mustafar": "мустафар", "Corellia": "кореллия", "Coruscant": "корусант",
    "Geonosis": "джеонозис", "Felucia": "фелуция", "Bracca": "бракка",
    "Dathomir": "датомир", "Tatooine": "татуин", "Kashyyyk": "кашиик",
    "Haven-class Medical Station": "медстанция", "Kessel": "кессель", "Lothal": "лотал",
    "Malachor": "малакор", "Vandor": "вандор", "Ring of Kafrene": "кольцо кафрены",
    "Death Star": "звезда смерти", "Hoth": "хот", "Scarif": "скариф",
    "Zeffo": "зеффо", "Mandalore": "мандалор",
}
RU_TO_PLANET_NAME = {ru: en for en, ru in PLANET_NAME_TO_RU.items()}


def _scan_field(lines: list, start_i: int, field_ru: str, max_lines: int = 6):
    """Ищет "**{field_ru}:** значение" среди строк ПОСЛЕ start_i, в пределах текущего
    планетного блока (останавливается на следующей кружок-строке — начале следующей
    планеты/этапа — или через max_lines). Используется и для "Цель:" (отличить планету от
    произвольной офицерской заметки с тем же кружком-маркером — старая логика, раньше жила
    прямо в цикле), и для "Взводы:" (новое, 2026-09-01: см. no_platoons ниже)."""
    prefix = f"**{field_ru}:**".lower()
    for j in range(start_i + 1, min(start_i + 1 + max_lines, len(lines))):
        candidate = lines[j].strip()
        if candidate.startswith(">"):
            candidate = candidate[1:].strip()
        if not candidate:
            continue
        if candidate[:1] in _TB_PLAN_CIRCLE_CHARS:
            break
        if candidate.lower().startswith(prefix):
            return candidate[len(prefix):].strip()
    return None


async def fetch_plan_planets(plan: dict) -> tuple:
    """Возвращает (список {"planet": англ.название|None, "raw": как было в сообщении,
    "round": N, "no_platoons": bool}, текст_ошибки|None). Планета=None — распознали блок
    этапа, но название не нашлось в RU_TO_PLANET_NAME (новая планета/опечатка — raw всё
    равно показываем).

    no_platoons — офицер явно написал "**Взводы:** нет" под этой планетой (реальный пример,
    2026-09-01: "43 базовых минимума", 6 этап, Малакор — Цель 0 звёзд, Взводы: нет, БЗ: нет,
    т.к. это последний этап и добивать её больше негде — не путать со "Взводы: по боту"/
    "только по боту", которые означают "взвод всё равно нужен для очков территории", см.
    cogs/tb_order_image.py). Раньше это поле нигде не читалось — /tb/platoons и автозаполнение
    строили полный набор из 90 слотов для ЛЮБОЙ распознанной планеты, даже когда ордер прямым
    текстом говорит, что взводы на ней не нужны (прямая жалоба пользователя). Потребители
    (web/routes/guild_dashboard.py::tb_platoons, tb_platoon_autofill.py) должны пропускать
    планету с no_platoons=True при построении слотов."""
    token = os.environ.get("DISCORD_TOKEN")
    if not token:
        return [], "DISCORD_TOKEN не настроен — не могу прочитать тред плана."

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(
                f"https://discord.com/api/v10/channels/{plan['thread_id']}/messages",
                headers={"Authorization": f"Bot {token}"},
                params={"limit": 100},
            )
            resp.raise_for_status()
            messages = resp.json()
    except httpx.HTTPError as e:
        return [], f"Не удалось прочитать тред плана «{plan['name']}» в Discord: {e}"

    # Discord отдаёт сообщения от новых к старым — для дублей (повторная публикация
    # с "принудительно: True") берём первое встреченное (самое новое) на каждый этап.
    by_round: dict = {}
    for msg in messages:
        content = msg.get("content") or ""
        header = _TB_PLAN_HEADER_RE.search(content)
        if not header:
            continue
        round_num = int(header.group(1))
        by_round.setdefault(round_num, content)

    if not by_round:
        return [], f"В треде плана «{plan['name']}» не нашлось ни одного блока этапа."

    entries = []
    for round_num in sorted(by_round):
        lines = by_round[round_num].splitlines()
        for i, line in enumerate(lines):
            stripped = line.strip()
            if stripped.startswith(">"):
                # Блоки бота (cogs/tb_order_image.py::_build_order_blocks) — цитата Discord
                # ("> {emoji} **Планета**"); офицерские анонсы (guild_events.py) — без неё.
                # Поддерживаем оба.
                stripped = stripped[1:].strip()
            if not stripped or stripped[0] not in _TB_PLAN_CIRCLE_CHARS:
                continue
            name_ru = stripped[1:].strip(" *#").lower()
            planet = RU_TO_PLANET_NAME.get(name_ru)
            if planet is None:
                # Неизвестное название — либо реально новая/ещё не занесённая в словарь
                # планета, либо просто офицерская заметка, начинающаяся тем же цветным
                # кружком, что и планеты (живой пример, этап 3 плана "43 базовых
                # минимума": "⚪ **Текстовый гайд на мандалорское ОЗ:**" — офицер приложил
                # объёмный гайд по модингу тем же маркером). Отличаем по следующей
                # непустой строке: у настоящей планеты сразу за названием всегда идёт
                # "**Цель:** N звёзд" (см. cogs/tb_order_image.py и все реальные анонсы) —
                # у произвольной заметки там что угодно другое. Известные (сопоставленные
                # по словарю) названия эта проверка не трогает — только помогает не
                # засорять список неопознанными "планетами".
                # Проверяем РОВНО следующую непустую строку (не окно из нескольких, в
                # отличие от _scan_field ниже) — сохраняет старое, уже проверенное на
                # реальном ложном срабатывании "Текстовый гайд..." поведение: офицерский
                # произвольный текст не должен случайно совпасть с "Цель:" где-то дальше.
                next_line = ""
                for j in range(i + 1, len(lines)):
                    candidate = lines[j].strip()
                    if candidate.startswith(">"):
                        candidate = candidate[1:].strip()
                    if candidate:
                        next_line = candidate
                        break
                if not next_line.lower().startswith("**цель:**"):
                    continue
            # max_lines по умолчанию (6) здесь безопасен — "Взводы:" не используется для
            # разграничения планета/не-планета (это уже решено выше), только для no_platoons.
            vzvod = _scan_field(lines, i, "Взводы")
            entries.append({
                "planet": planet,
                "raw": stripped[1:].strip(" *#"),
                "round": round_num,
                "no_platoons": bool(vzvod) and vzvod.strip().lower().rstrip(".") == "нет",
            })
    return entries, None
