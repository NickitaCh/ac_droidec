"""Сокращённые описания бонусов датакронов — данные для /admin/datacron-shorts и
черновики через OpenRouter.

Полные игровые тексты бонусов — абзацы, ломающие и Discord-вывод /дк_требования, и
веб-таблицы. Сокращение ищется так (cogs/datacron_requirements.py::_ability_short_template):
ручная правка из таблицы datacron_ability_shorts → встроенный ABILITY_SHORT_OVERRIDES →
автосокращение (первое предложение). Эта страница правит первый слой; ИИ только
предлагает черновик в поле формы — в БД попадает лишь то, что человек сохранил сам
(модель иногда перевирает цифры/условия механик)."""

import difflib
import os
import re

import database
from cogs.datacron_requirements import (
    ABILITY_SHORT_OVERRIDES,
    ABILITY_SHORT_SOURCE_AUTO,
    ABILITY_SHORT_SOURCE_BUILTIN,
    ABILITY_SHORT_SOURCE_CUSTOM,
    DATACRON_LEVELS,
    _ability_short_template,
    _fill_target,
)
from services import openrouter_vision

MAX_SHORT_LEN = 300
GENERATE_BATCH_SIZE = 40
_FEW_SHOT_LIMIT = 12  # минимум примеров (добивается образцами стиля)
_FEW_SHOT_MAX = 24  # потолок похожих примеров на пачку — промпт не должен раздуваться
_SIMILAR_PER_ITEM = 2
_SIMILAR_MIN_RATIO = 0.5


def season_rows(season: dict, custom_shorts: dict) -> dict:
    """{3: [row, ...], 6: [...], 9: [...]} — одна строка на ability_id (одна и та же
    способность может висеть на нескольких ветках — ветки перечисляются в строке)."""
    rows = {level: [] for level in DATACRON_LEVELS}
    for ability_id, info in season["abilities"].items():
        branches = info["branches"]
        first_branch = branches[0] if branches else ""
        full = info["full"]
        fallback = ABILITY_SHORT_OVERRIDES.get(ability_id)
        fallback_source = ABILITY_SHORT_SOURCE_BUILTIN
        if fallback is None:
            fallback, fallback_source = _ability_short_template(ability_id, full, {})
        template, source = _ability_short_template(ability_id, full, custom_shorts)
        rows[info["level"]].append({
            "ability_id": ability_id,
            "branches": branches,
            "full": _fill_target(full, first_branch) if full else "",
            "custom": custom_shorts.get(ability_id, ""),
            "fallback": _fill_target(fallback, first_branch),
            "fallback_source": fallback_source,
            "source": source,
            # То, что реально выводится в /дк_требования и на сайте прямо сейчас.
            "effective": _fill_target(template, first_branch),
            "branch_label": first_branch,
        })
    for level_rows in rows.values():
        level_rows.sort(key=lambda r: (", ".join(r["branches"]), r["ability_id"]))
    return rows


def count_missing(season: dict, custom_shorts: dict) -> int:
    """Сколько бонусов сезона держатся только на автосокращении."""
    return sum(
        1 for ability_id, info in season["abilities"].items()
        if _ability_short_template(ability_id, info["full"], custom_shorts)[1] == ABILITY_SHORT_SOURCE_AUTO
    )


def _norm_for_similarity(text: str) -> str:
    """Числа → «#»: «восстанавливают 2%» и «восстанавливают 10%» — один шаблон фразы."""
    return re.sub(r"\d+(?:[.,]\d+)?", "#", text or "").lower()


def _few_shot_examples(catalog: dict, custom_shorts: dict, items: list) -> list:
    """Пары «полный текст → сокращение» из уже принятых (ручные правки + встроенные).
    Для каждого сокращаемого бонуса берутся самые похожие по тексту (многие бонусы из
    сезона в сезон повторяются с другой фракцией/цифрами — идея Ricardo, чат 2026-09-30),
    остаток добивается образцами стиля, ручные правки первыми. Похожий ≠ тот же: модель
    сама решает, совпадает ли механика (см. правило в _build_prompt)."""
    known = {**ABILITY_SHORT_OVERRIDES, **custom_shorts}
    wanted = {ability_id for ability_id, _full in items}
    pairs = {}  # ability_id -> (full, short, is_custom)
    for season in catalog["seasons"].values():
        for ability_id, info in season["abilities"].items():
            if ability_id in known and ability_id not in wanted and info["full"]:
                pairs[ability_id] = (info["full"], known[ability_id], ability_id in custom_shorts)
    if not pairs:
        return [(None, short) for short in list(ABILITY_SHORT_OVERRIDES.values())[:_FEW_SHOT_LIMIT]]

    normalized = {aid: _norm_for_similarity(full) for aid, (full, _s, _c) in pairs.items()}
    scored = {}
    for _ability_id, full in items:
        target = _norm_for_similarity(full)
        best = sorted(
            ((difflib.SequenceMatcher(None, target, text).ratio(), aid) for aid, text in normalized.items()),
            reverse=True,
        )[:_SIMILAR_PER_ITEM]
        for ratio, aid in best:
            if ratio >= _SIMILAR_MIN_RATIO:
                scored[aid] = max(scored.get(aid, 0), ratio)
    chosen = [aid for aid, _r in sorted(scored.items(), key=lambda kv: -kv[1])][:_FEW_SHOT_MAX]
    for aid in sorted(pairs, key=lambda a: not pairs[a][2]):  # образцы стиля, ручные первыми
        if len(chosen) >= _FEW_SHOT_LIMIT:
            break
        if aid not in chosen:
            chosen.append(aid)
    seen, result = set(), []
    for aid in chosen:
        full, short, _is_custom = pairs[aid]
        if short not in seen:
            seen.add(short)
            result.append((full, short))
    return result


def _build_prompt(items: list, examples: list) -> str:
    lines = [
        "Ты сокращаешь описания бонусов датакронов из игры Star Wars: Galaxy of Heroes для",
        "компактного списка требований гильдии. Правила:",
        "- По-русски, одной строкой, обычно 40–120 символов, максимум 200.",
        "- Сохраняй ВСЕ числа, длительности (ходы), условия и ограничения — нельзя терять механику.",
        "- Субъект (фракцию/сторону/персонажа) НЕ повторяй: перед текстом уже стоит «Ветка: ».",
        "- Стиль: «условие → эффект», стандартные сокращения игроков: ШХ (шкала хода), ХП,",
        "  осн./особая способность, крит., доп. ход, «не увернуться», «нельзя снять».",
        "- Плейсхолдер {0} (название ветки) в ответе не используй.",
        "- Многие бонусы повторяются из сезона в сезон с другой фракцией или цифрами. Если",
        "  бонус совпадает с примером по механике (то же условие и тот же эффект) — возьми",
        "  формулировку примера, заменив только цифры. Если условие или эффект другие — пиши",
        "  заново, не подгоняй под похожий пример.",
        "",
        "Примеры уже принятых сокращений (сначала самые похожие на сокращаемые):",
    ]
    for full, short in examples:
        if full:
            lines.append(f"ПОЛНЫЙ: {full}")
        lines.append(f"КОРОТКО: {short}")
        lines.append("")
    lines.append("Сократи следующие бонусы. Ответ — только JSON-объект вида {\"<id>\": \"<сокращение>\", ...}")
    lines.append("со всеми id из списка ниже и ничем больше.")
    lines.append("")
    for ability_id, full in items:
        lines.append(f"{ability_id}: {full}")
    return "\n".join(lines)


def generate_drafts(catalog: dict, set_id: int, ability_ids: list) -> tuple:
    """Синхронная (вызывать через asyncio.to_thread). Возвращает (drafts, error):
    drafts — {ability_id: текст}, ничего не сохраняет."""
    api_key = os.getenv("OPENROUTER_API_KEY")
    if not api_key:
        return {}, "Ключ OpenRouter не настроен (OPENROUTER_API_KEY)."
    season = catalog["seasons"].get(set_id)
    if not season:
        return {}, "Сезон не найден в справочнике."
    items = [
        (ability_id, season["abilities"][ability_id]["full"])
        for ability_id in ability_ids
        if ability_id in season["abilities"] and season["abilities"][ability_id]["full"]
    ]
    if not items:
        return {}, "Нечего сокращать — у выбранных бонусов нет игрового текста."

    batches = [items[i:i + GENERATE_BATCH_SIZE] for i in range(0, len(items), GENERATE_BATCH_SIZE)]
    remaining = openrouter_vision.OPENROUTER_DAILY_REQUEST_LIMIT - database.get_openrouter_requests_today()
    if remaining < len(batches):
        return {}, f"Суточный лимит OpenRouter почти исчерпан (осталось {max(remaining, 0)} запросов, нужно {len(batches)})."

    custom_shorts = database.get_datacron_ability_shorts()
    drafts = {}
    for batch in batches:
        examples = _few_shot_examples(catalog, custom_shorts, batch)
        wanted = {ability_id for ability_id, _full in batch}
        try:
            answer = openrouter_vision.call_text_json(
                _build_prompt(batch, examples), api_key,
                validate=lambda d, w=wanted: True if w & set(d) else "нет ни одного запрошенного id",
            )
        except Exception as e:
            if drafts:
                return drafts, f"Часть черновиков не получена: {e}"
            return {}, f"Ошибка OpenRouter: {e}"
        if not isinstance(answer, dict):
            continue
        for ability_id, text in answer.items():
            if ability_id in wanted and isinstance(text, str) and text.strip():
                drafts[ability_id] = " ".join(text.split())[:MAX_SHORT_LEN]
    if not drafts:
        return {}, "Модель не вернула ни одного сокращения — попробуйте ещё раз."
    return drafts, None


SOURCE_LABELS = {
    ABILITY_SHORT_SOURCE_CUSTOM: "ручное",
    ABILITY_SHORT_SOURCE_BUILTIN: "встроенное",
    ABILITY_SHORT_SOURCE_AUTO: "авто",
}

SOURCE_BADGES = {
    ABILITY_SHORT_SOURCE_CUSTOM: "badge-ok",
    ABILITY_SHORT_SOURCE_BUILTIN: "badge-neutral",
    ABILITY_SHORT_SOURCE_AUTO: "badge-warn",
}
