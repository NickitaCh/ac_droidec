"""Веб-версия /статы и /статы_релик — прогноз статов игрока на релик плейта и
пересчёт уже заданной в плейте нормы на другой релик. Сам расчёт целиком живёт
в cogs/stat_requirements.py (не дублируется) и services/stat_forecast.py (обёртки
+ процесс-локальный кэш StatCalc) — этот роут только собирает форму и рендерит
готовые текстовые блоки (те же моноширинные таблицы, что строит бот для Discord)."""

from pathlib import Path

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

import database
import services.stat_forecast as stat_forecast
from web.deps import require_officer_access

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent.parent / "templates"))


def _get_comlink():
    # main.py создаёт единственный SwgohComlink и вешает на bot.comlink — веб-процесс
    # не поднимает диска-клиента, поэтому строит свой (тот же URL, тот же comlink-сайдкар).
    from swgoh_comlink import SwgohComlink
    return SwgohComlink(url="http://localhost:3000")


def _roster_choices(guild_id: int):
    """[(ally_code, ingame_name), ...] по всему живому составу гильдии (не только
    зарегистрированным через /регистрация) — то же самое, что видит Discord-автокомплит
    игрока (bot.guild_roster_caches), только через персистентный кэш в user_mapping."""
    rows = database.get_all_user_mappings(guild_id)
    return sorted(((ally_code, name) for _, ally_code, name in rows), key=lambda p: p[1].lower())


@router.get("", response_class=HTMLResponse)
async def stats_check_form(
    request: Request,
    plate: str = "",
    ally_code: str = "",
    character: str = "",
    force_refresh: bool = False,
    user: dict = Depends(require_officer_access),
):
    guild_id = user["guild_id"]
    plates = database.get_all_stat_requirement_plates(guild_id=guild_id)
    roster = _roster_choices(guild_id)

    context = {
        "user": user,
        "plates": plates,
        "roster": roster,
        "selected_plate": plate,
        "selected_ally_code": ally_code,
        "selected_character": character,
        "force_refresh": force_refresh,
        "characters": [],
        "results": None,
        "guild_report": None,
        "loading": False,
        "error": None,
    }

    if not plate:
        return templates.TemplateResponse(request, "stats_check.html", context)

    char_keys = database.get_stat_requirement_characters(plate, guild_id=guild_id)
    context["characters"] = [(base_id, database.get_game_unit_name(base_id) or base_id) for base_id in char_keys]
    target_chars = [character] if character else char_keys

    try:
        comlink = _get_comlink()
        stat_calc = await stat_forecast.get_stat_calc(comlink)
    except Exception:
        context["loading"] = True
        return templates.TemplateResponse(request, "stats_check.html", context)

    target_ally_code = ally_code.strip()
    if not target_ally_code:
        # Игрок не выбран — проверка по всей гильдии (кэшированные данные, без live Comlink).
        context["guild_report"] = await stat_forecast.build_guild_report(comlink, stat_calc, plate, target_chars, guild_id=guild_id)
        return templates.TemplateResponse(request, "stats_check.html", context)

    player_label = next((name for code, name in roster if code == target_ally_code), target_ally_code)
    results = []
    for base_id in target_chars:
        outcome = await stat_forecast.evaluate_character_player(
            comlink, stat_calc, plate, base_id, target_ally_code, force_refresh, player_label, guild_id=guild_id,
        )
        if outcome is None:
            continue
        char_name, block, matched, total, updated_at = outcome
        results.append({
            "char_name": char_name, "block": block, "matched": matched, "total": total, "updated_at": updated_at,
        })
    context["results"] = results
    context["player_label"] = player_label
    return templates.TemplateResponse(request, "stats_check.html", context)


@router.get("/relic", response_class=HTMLResponse)
async def stats_relic_form(
    request: Request,
    plate: str = "",
    character: str = "",
    relic: int | None = None,
    user: dict = Depends(require_officer_access),
):
    guild_id = user["guild_id"]
    plates = database.get_all_stat_requirement_plates(guild_id=guild_id)

    context = {
        "user": user,
        "plates": plates,
        "selected_plate": plate,
        "selected_character": character,
        "selected_relic": relic,
        "characters": [],
        "result": None,
        "loading": False,
        "error": None,
    }

    if not plate:
        return templates.TemplateResponse(request, "stats_relic.html", context)

    char_keys = database.get_stat_requirement_characters(plate, guild_id=guild_id)
    context["characters"] = [(base_id, database.get_game_unit_name(base_id) or base_id) for base_id in char_keys]

    if not character or relic is None:
        return templates.TemplateResponse(request, "stats_relic.html", context)
    if not (0 <= relic <= 10):
        context["error"] = "Релик должен быть от 0 до 10."
        return templates.TemplateResponse(request, "stats_relic.html", context)

    try:
        comlink = _get_comlink()
        stat_calc = await stat_forecast.get_stat_calc(comlink)
    except Exception:
        context["loading"] = True
        return templates.TemplateResponse(request, "stats_relic.html", context)

    outcome = await stat_forecast.project_character_relic(comlink, stat_calc, plate, character, relic, guild_id=guild_id)
    if outcome is None:
        context["error"] = f"У персонажа {database.get_game_unit_name(character) or character} нет сохранённых требований в плейте «{plate}»."
        return templates.TemplateResponse(request, "stats_relic.html", context)

    char_name, block = outcome
    context["result"] = {"char_name": char_name, "block": block}
    return templates.TemplateResponse(request, "stats_relic.html", context)
