"""Веб-only "Конструктор" (/mod-builder) — гипотетическая сборка модов для персонажа,
которого никто ещё не прокачал: сколько модов какого сета надето (точный бонус сета из
реальных данных игры) + вручную оценённая сумма вклада в каждый стат от primary+secondary
роллов (то же допущение, что RELIC_PROJECTION_FLAT_OFFSET в cogs/stat_requirements.py, но
для любого стата, а не только четырёх захардкоженных). Расчёт — stat_engine.
build_hypothetical_unit + apply_manual_stat_totals (см. их докстринги за подробным "почему
не точные 6 слотов с primary/secondary"). Отдельная фича, не порт Discord-команды — той
не существует, поэтому не в stat_forecast.py (тот держит только веб-версии /статы и
/статы_релик)."""

from pathlib import Path
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

import database
import stat_engine
import services.stat_forecast as stat_forecast
from cogs.stat_requirements import STAT_CHOICES
from web.deps import require_officer_access

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent.parent / "templates"))

MOD_SET_CHOICES = sorted(((int(set_id), name) for set_id, name in stat_engine.MOD_SET_IDS.items()))
STAT_NAME_CHOICES = [(c.name, c.value) for c in STAT_CHOICES if c.value != "Relic"]


def _get_comlink():
    # Как в services/stat_forecast.py — веб-процесс не поднимает диска-клиента, строит свой.
    from swgoh_comlink import SwgohComlink
    return SwgohComlink(url="http://localhost:3000")


def _fmt_value(value: float) -> str:
    """Не %g — тот уходит в научную нотацию за 1e6 (Health/Protection на релике легко
    больше миллиона, в отличие от порогов в /статы_требования, откуда скопирован этот
    формат исторически)."""
    if value == int(value):
        return str(int(value))
    return f"{value:.2f}".rstrip("0").rstrip(".")


def _char_label(base_id: str) -> str:
    return database.get_game_unit_name(base_id) or base_id


def _sets_summary(set_counts: dict) -> str:
    set_names = dict(MOD_SET_CHOICES)
    parts = [f"{count}×{set_names.get(int(set_id), f'#{set_id}')}" for set_id, count in sorted(set_counts.items(), key=lambda kv: int(kv[0]))]
    return ", ".join(parts) if parts else "—"


def _stats_summary(manual_stats: dict) -> str:
    return ", ".join(f"{name} +{_fmt_value(value)}" for name, value in manual_stats.items()) if manual_stats else "—"


def _preset_rows(guild_id: int):
    return [
        {"id": pid, "name": name, "sets_summary": _sets_summary(sets), "stats_summary": _stats_summary(stats)}
        for pid, name, sets, stats, _created_by, _created_at in database.get_all_stat_mod_presets(guild_id=guild_id)
    ]


def _history_rows(guild_id: int):
    return [
        {
            "id": hid, "char_name": _char_label(char_key), "relic": relic,
            "sets_summary": _sets_summary(sets), "stats_summary": _stats_summary(stats), "created_at": created_at,
        }
        for hid, char_key, relic, sets, stats, _created_by, created_at in database.get_stat_hypothetical_history(guild_id=guild_id)
    ]


def _parse_set_counts(mapping) -> dict:
    """mapping — Starlette QueryParams (GET) или FormData (POST), обе умеют .get()."""
    counts = {}
    for set_id, _ in MOD_SET_CHOICES:
        raw = mapping.get(f"set_{set_id}")
        if not raw:
            continue
        try:
            n = int(raw)
        except ValueError:
            continue
        if n > 0:
            counts[set_id] = n
    return counts


def _parse_manual_stats(mapping) -> dict:
    """Строки stat_name/stat_value идут параллельными повторяющимися полями (см.
    data-row-group в шаблоне) — .getlist() зипует их по порядку."""
    names = mapping.getlist("stat_name")
    values = mapping.getlist("stat_value")
    totals = {}
    for name, raw_value in zip(names, values):
        if not name or not raw_value:
            continue
        try:
            value = float(raw_value)
        except ValueError:
            continue
        totals[name] = totals.get(name, 0) + value
    return totals


def _redirect_qs(mapping) -> str:
    """Пересобирает querystring из текущего запроса (character/relic/сеты/статы) — чтобы
    после сохранения пресета редирект вернул пользователя на тот же расчёт."""
    pairs = []
    character = mapping.get("character")
    if character:
        pairs.append(("character", character))
    relic = mapping.get("relic")
    if relic:
        pairs.append(("relic", relic))
    for set_id, _ in MOD_SET_CHOICES:
        val = mapping.get(f"set_{set_id}")
        if val:
            pairs.append((f"set_{set_id}", val))
    for name, value in zip(mapping.getlist("stat_name"), mapping.getlist("stat_value")):
        if name and value:
            pairs.append(("stat_name", name))
            pairs.append(("stat_value", value))
    return urlencode(pairs)


@router.get("", response_class=HTMLResponse)
async def builder_form(request: Request, user: dict = Depends(require_officer_access)):
    guild_id = user["guild_id"]
    qp = request.query_params

    character = qp.get("character", "")
    relic_raw = qp.get("relic", "")
    set_counts = _parse_set_counts(qp)
    manual_stats = _parse_manual_stats(qp)

    history_id = qp.get("history_id")
    preset_id = qp.get("preset_id")
    reopened_from_history = False
    if history_id:
        entry = database.get_stat_hypothetical_history_entry(int(history_id), guild_id=guild_id)
        if entry:
            _, character, relic_int, set_counts, manual_stats, _, _ = entry
            relic_raw = str(relic_int)
            reopened_from_history = True
    elif preset_id:
        preset = database.get_stat_mod_preset(int(preset_id), guild_id=guild_id)
        if preset:
            _, _, set_counts, manual_stats, _, _ = preset

    # JSON-хранение (пресеты/история) отдаёт ключи sets как строки — приводим к int,
    # чтобы совпадало с MOD_SET_CHOICES/шаблоном; для set_counts из _parse_set_counts
    # ключи и так int, int(int) безвреден.
    set_counts = {int(k): v for k, v in set_counts.items()}

    context = {
        "user": user,
        "mod_sets": MOD_SET_CHOICES,
        "stat_name_choices": STAT_NAME_CHOICES,
        "selected_character": character,
        "selected_character_label": _char_label(character) if character else "",
        "selected_relic": relic_raw,
        "set_counts": set_counts,
        "manual_stat_rows": [(name, _fmt_value(value)) for name, value in manual_stats.items()] if manual_stats else [("", "")],
        "presets": _preset_rows(guild_id),
        "history": _history_rows(guild_id),
        "result": None,
        "loading": False,
        "error": qp.get("error"),
    }

    if not character or not relic_raw:
        return templates.TemplateResponse(request, "stat_builder.html", context)

    try:
        relic = int(relic_raw)
    except ValueError:
        context["error"] = "Некорректный уровень реликвии."
        return templates.TemplateResponse(request, "stat_builder.html", context)
    if not (0 <= relic <= 10):
        context["error"] = "Релик должен быть от 0 до 10."
        return templates.TemplateResponse(request, "stat_builder.html", context)

    try:
        comlink = _get_comlink()
        stat_calc = await stat_forecast.get_stat_calc(comlink)
    except Exception:
        context["loading"] = True
        return templates.TemplateResponse(request, "stat_builder.html", context)

    unit = stat_engine.build_hypothetical_unit(character, relic, set_counts)
    final_stats = stat_engine.calc_final_stats(stat_calc, unit)
    final_stats = stat_engine.apply_manual_stat_totals(final_stats, manual_stats)

    context["result"] = {
        "char_name": _char_label(character),
        "rows": [(label, _fmt_value(final_stats.get(value, 0))) for label, value in STAT_NAME_CHOICES],
    }

    if not reopened_from_history:
        database.add_stat_hypothetical_history(character, relic, set_counts, manual_stats, user["discord_id"], guild_id=guild_id)
        context["history"] = _history_rows(guild_id)

    return templates.TemplateResponse(request, "stat_builder.html", context)


@router.post("/presets/save", response_class=HTMLResponse)
async def preset_save(request: Request, user: dict = Depends(require_officer_access)):
    form = await request.form()
    name = (form.get("name") or "").strip()
    if not name:
        return RedirectResponse(f"/mod-builder?{urlencode({'error': 'Укажите имя пресета.'})}", status_code=303)

    set_counts = _parse_set_counts(form)
    manual_stats = _parse_manual_stats(form)
    ok = database.create_stat_mod_preset(name, set_counts, manual_stats, user["discord_id"], guild_id=user["guild_id"])
    qs = _redirect_qs(form)
    if not ok:
        error_qs = urlencode({"error": f"Пресет «{name}» уже существует."})
        return RedirectResponse(f"/mod-builder?{qs}&{error_qs}" if qs else f"/mod-builder?{error_qs}", status_code=303)
    return RedirectResponse(f"/mod-builder?{qs}" if qs else "/mod-builder", status_code=303)


@router.post("/presets/{preset_id}/delete", response_class=HTMLResponse)
async def preset_delete(preset_id: int, user: dict = Depends(require_officer_access)):
    database.delete_stat_mod_preset(preset_id, guild_id=user["guild_id"])
    return RedirectResponse("/mod-builder", status_code=303)
