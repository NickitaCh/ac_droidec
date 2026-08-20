"""Веб-версия /дк_требования — управление требованиями к датакронам по сезонам ТБ и их
проверка против ростера гильдии. CRUD-логика (таблицы datacron_requirements/
datacron_focused_requirements) целиком живёт в database.py, этот роут только вызывает её
и рендерит — как web/routes/stat_plates.py. Чистые функции сопоставления/форматирования
(валидация, матчинг требований к датакронам игрока, подписи уровней) переиспользуются
напрямую из cogs.datacron_requirements, а не дублируются — тот же приём, что уже
используется в stat_plates.py для PRIORITY_*/OPERATOR_CHOICES/STAT_CHOICES.

Каталог сезонов/способностей (аналог bot.datacron_cache) веб-процесс строит и кэширует
сам через services/datacron_catalog.py, т.к. у него нет доступа к объекту bot — см.
web/routes/registration.py::_get_comlink() за тем же паттерном отдельного Comlink-клиента.

Отличие от Discord-команды /дк_требования редактировать: там патч-семантика (None =
не менять поле). Здесь формы всегда шлют все поля разом (как requirement_edit в
stat_plates.py) — редактирование полностью заменяет запись, без частичного патча."""

import asyncio
from pathlib import Path
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

import database
from cogs.datacron_requirements import (
    DATACRON_ANY,
    DATACRON_ANY_LABEL,
    DATACRON_MAX_STAT_REQUIREMENTS,
    DATACRON_NONE,
    DATACRON_NONE_LABEL,
    DATACRON_STAT_LABELS,
    PRIORITY_CHOICES,
    PRIORITY_EMOJI,
    PRIORITY_LABELS,
    PRIORITY_ORDER,
    PRIORITY_REQUIRED,
    _check_requirement_stats,
    _extract_player_base_datacrons,
    _extract_player_focused_datacrons,
    _focused_char_label,
    _is_valid_focused_character,
    _is_valid_level_value,
    _is_valid_season,
    _level_label,
    _match_counts,
    _match_requirements,
    _parse_stat_pair_params,
)
from services import datacron_catalog
from web.deps import require_officer_access

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent.parent / "templates"))

PRIORITY_OPTIONS = [(c.name, c.value) for c in PRIORITY_CHOICES]
# Бонусное множество стат-каталога уже мало и известно заранее (8 записей) — как и с
# level_options (см. datacron_season.html: "предпочитать plain <select>, когда набор
# маленький и известен на момент рендера"), рендерится сразу как <select>, без live-search.
STAT_OPTIONS = list(DATACRON_STAT_LABELS.items())  # [(stat_id, label), ...]


def _get_comlink():
    # Веб-процесс не поднимает диска-клиента бота — строит свой (тот же URL, тот же
    # comlink-сайдкар). Паттерн идентичен web/routes/registration.py::_get_comlink().
    from swgoh_comlink import SwgohComlink
    return SwgohComlink(url="http://localhost:3000")


async def _safe_catalog():
    """None, если каталог ещё не построен ни разу и Comlink недоступен — страницы
    должны обработать этот случай (показать "ещё загружается"), а не упасть."""
    try:
        return await datacron_catalog.get_catalog(_get_comlink())
    except Exception as e:
        print(f"⚠️ [web] Каталог датакронов недоступен: {e}")
        return None


def _stat_pairs_from_form(stat_ids: list, values: list):
    """Веб-форма шлёт до 5 пар (stat_id как строка или "", value как float или None) —
    приводим "" к None и переиспользуем ту же валидацию, что и Discord-команды
    (_parse_stat_pair_params), чтобы не дублировать правила (не более 1 раза на стат и т.п.)."""
    pairs = [(sid or None, val) for sid, val in zip(stat_ids, values)]
    return _parse_stat_pair_params(pairs)


def _redirect_list(error: str = None, notice: str = None):
    params = {}
    if error:
        params["error"] = error
    if notice:
        params["notice"] = notice
    qs = f"?{urlencode(params)}" if params else ""
    return RedirectResponse(f"/datacrons{qs}", status_code=303)


def _redirect_season(set_id: int, error: str = None, notice: str = None):
    params = {}
    if error:
        params["error"] = error
    if notice:
        params["notice"] = notice
    qs = f"?{urlencode(params)}" if params else ""
    return RedirectResponse(f"/datacrons/{set_id}{qs}", status_code=303)


# =====================================================================
# Список сезонов
# =====================================================================
@router.get("", response_class=HTMLResponse)
async def datacrons_list(request: Request, user: dict = Depends(require_officer_access)):
    guild_id = user["guild_id"]
    catalog = await _safe_catalog()

    seasons = []
    if catalog:
        for set_id in sorted(catalog["seasons"].keys(), reverse=True):
            data = catalog["seasons"][set_id]
            seasons.append({
                "set_id": set_id,
                "display_name": data["display_name"],
                "base_count": database.count_datacron_requirements_by_set(set_id, guild_id=guild_id),
                "focused_count": database.count_datacron_focused_requirements_by_set(set_id, guild_id=guild_id),
            })

    return templates.TemplateResponse(request, "datacrons.html", {
        "user": user,
        "seasons": seasons,
        "catalog_loaded": catalog is not None,
        "error": request.query_params.get("error"),
        "notice": request.query_params.get("notice"),
    })


# "/check" ЗАРЕГИСТРИРОВАН РАНЬШЕ "/{set_id}" НАМЕРЕННО: Starlette матчит GET-роуты
# в порядке регистрации, а не по специфичности — если бы "/{set_id}" шёл первым,
# он перехватывал бы GET /datacrons/check (set_id="check") и падал 422 при попытке
# привести "check" к int, до "/check" очередь бы не дошла вообще.
@router.get("/check", response_class=HTMLResponse)
async def check_form(request: Request, user: dict = Depends(require_officer_access)):
    guild_id = user["guild_id"]
    catalog = await _safe_catalog()
    seasons = []
    if catalog:
        seasons = [
            {"set_id": sid, "display_name": data["display_name"]}
            for sid, data in sorted(catalog["seasons"].items(), key=lambda kv: -kv[0])
        ]
    players = sorted({name for _discord_id, _ally_code, name in database.get_all_user_mappings(guild_id)}, key=str.lower)

    season_param = request.query_params.get("season")
    target_param = request.query_params.get("target") or ""
    guild_mode = target_param == "__guild__"
    player_param = target_param if target_param and not guild_mode else None

    result = None
    error = None
    if season_param:
        try:
            set_id = int(season_param)
        except ValueError:
            set_id = None
        if set_id is None or not catalog or set_id not in catalog["seasons"]:
            error = "Некорректный сезон."
        else:
            requirements = database.get_datacron_requirements_by_set(set_id, guild_id=guild_id)
            focused_requirements = database.get_datacron_focused_requirements_by_set(set_id, guild_id=guild_id)
            if not requirements and not focused_requirements:
                error = "У этого сезона нет сохранённых требований."
            elif guild_mode:
                result = await _build_guild_report(set_id, requirements, focused_requirements, guild_id)
            else:
                ally_code = None
                player_name = player_param
                if not player_name:
                    reg = database.get_user_registration(user["discord_id"], guild_id=guild_id)
                    if reg:
                        ally_code, player_name = reg
                    else:
                        error = "Вы не зарегистрированы и не выбрали игрока — выберите игрока из списка либо зарегистрируйтесь на /registration."
                else:
                    for _discord_id, code, name in database.get_all_user_mappings(guild_id):
                        if name == player_name:
                            ally_code = code
                            break
                    if ally_code is None:
                        error = f"Игрок «{player_name}» не найден среди зарегистрированных."
                if ally_code and not error:
                    result = await _build_player_report(catalog, set_id, ally_code, player_name, requirements, focused_requirements, guild_id)
                    if result and result.get("error"):
                        error = result["error"]
                        result = None

    return templates.TemplateResponse(request, "datacron_check.html", {
        "user": user,
        "seasons": seasons,
        "players": players,
        "season_param": season_param,
        "player_param": player_param,
        "guild_mode": guild_mode,
        "result": result,
        "error": error,
        "priority_order": PRIORITY_ORDER,
        "priority_labels": PRIORITY_LABELS,
        "priority_emoji": PRIORITY_EMOJI,
    })


# =====================================================================
# Детальная страница сезона: список требований + формы добавления
# =====================================================================
@router.get("/{set_id}", response_class=HTMLResponse)
async def season_detail(request: Request, set_id: int, user: dict = Depends(require_officer_access)):
    guild_id = user["guild_id"]
    catalog = await _safe_catalog()
    season_data = catalog["seasons"].get(set_id) if catalog else None
    if season_data is None:
        return _redirect_list(error=f"Сезон {set_id} не найден в справочнике (завершён либо справочник ещё загружается).")

    base_rows = database.get_datacron_requirements_by_set(set_id, guild_id=guild_id)
    focused_rows = database.get_datacron_focused_requirements_by_set(set_id, guild_id=guild_id)

    stats_by_req = database.get_datacron_requirement_stats_by_set(set_id, guild_id=guild_id)
    base_reqs = []
    for row in base_rows:
        req_id, _, pack, l3, l6, l9, comment, created_by, created_at, priority, stats = row
        stat_reqs = stats_by_req.get(req_id, [])
        base_reqs.append({
            "id": req_id, "pack": pack,
            "level3": l3, "level6": l6, "level9": l9,
            "level3_label": _level_label(season_data["level3"], l3),
            "level6_label": _level_label(season_data["level6"], l6),
            "level9_label": _level_label(season_data["level9"], l9),
            "comment": comment, "stats": stats, "priority": priority,
            "priority_label": PRIORITY_LABELS.get(priority, priority),
            "priority_emoji": PRIORITY_EMOJI.get(priority, ""),
            # Уровни, к которым имеет смысл предложить "добавить альтернативу" —
            # только там, где уже стоит конкретная способность (не ANY/NONE).
            "alt_levels": [n for n, v in ((3, l3), (6, l6), (9, l9)) if v not in (DATACRON_ANY, DATACRON_NONE)],
            "stat_reqs": [{"stat_id": sid, "label": DATACRON_STAT_LABELS.get(sid, str(sid)), "min_value": val} for sid, val in stat_reqs],
        })
    focused_reqs = []
    for row in focused_rows:
        req_id, _, pack, character_key, required_level, comment, created_by, created_at, priority = row
        focused_reqs.append({
            "id": req_id, "pack": pack, "character_key": character_key,
            "character_label": _focused_char_label(catalog, set_id, character_key),
            "required_level": required_level, "comment": comment, "priority": priority,
            "priority_label": PRIORITY_LABELS.get(priority, priority),
            "priority_emoji": PRIORITY_EMOJI.get(priority, ""),
        })

    return templates.TemplateResponse(request, "datacron_season.html", {
        "user": user,
        "set_id": set_id,
        "display_name": season_data["display_name"],
        "base_reqs": base_reqs,
        "focused_reqs": focused_reqs,
        "has_focused_catalog": bool(season_data.get("focused")),
        "priority_options": PRIORITY_OPTIONS,
        "priority_default": PRIORITY_REQUIRED,
        # Полный список бонусов по уровню — теперь рендерится сразу как <select>,
        # без live-search виджета (был неочевиден: пустое поле, пока не начнёшь печатать).
        "level_options": {3: season_data["level3"], 6: season_data["level6"], 9: season_data["level9"]},
        "datacron_any": DATACRON_ANY,
        "datacron_any_label": DATACRON_ANY_LABEL,
        "datacron_none": DATACRON_NONE,
        "datacron_none_label": DATACRON_NONE_LABEL,
        "stat_options": STAT_OPTIONS,
        "stat_slots": range(1, DATACRON_MAX_STAT_REQUIREMENTS + 1),
        "error": request.query_params.get("error"),
        "notice": request.query_params.get("notice"),
    })


# =====================================================================
# Добавление требований
# =====================================================================
@router.post("/{set_id}/add", response_class=HTMLResponse)
async def add_base_requirement(
    set_id: int,
    pack: str = Form(""),
    priority: str = Form(PRIORITY_REQUIRED),
    level3: str = Form(DATACRON_NONE),
    level6: str = Form(DATACRON_NONE),
    level9: str = Form(DATACRON_NONE),
    stats: str = Form(""),
    stat1: str = Form(""), value1: float = Form(None),
    stat2: str = Form(""), value2: float = Form(None),
    stat3: str = Form(""), value3: float = Form(None),
    stat4: str = Form(""), value4: float = Form(None),
    stat5: str = Form(""), value5: float = Form(None),
    comment: str = Form(""),
    user: dict = Depends(require_officer_access),
):
    guild_id = user["guild_id"]
    catalog = await _safe_catalog()
    if not _is_valid_season(catalog, set_id):
        return _redirect_season(set_id, error="Сезон не найден в справочнике.")
    for level_num, value in ((3, level3), (6, level6), (9, level9)):
        if not _is_valid_level_value(catalog, set_id, level_num, value):
            return _redirect_season(set_id, error=f"Некорректное значение уровня {level_num} — выберите вариант из подсказок.")
    if level3 == DATACRON_NONE and level6 == DATACRON_NONE and level9 == DATACRON_NONE:
        return _redirect_season(set_id, error="Укажите хотя бы один уровень (3/6/9).")
    stat_pairs, stat_error = _stat_pairs_from_form([stat1, stat2, stat3, stat4, stat5], [value1, value2, value3, value4, value5])
    if stat_error:
        return _redirect_season(set_id, error=stat_error)

    req_id = database.add_datacron_requirement(
        set_id, pack.strip() or None, level3, level6, level9, comment.strip() or None,
        user["discord_id"], priority, guild_id=guild_id, stats=stats.strip() or None,
    )
    if stat_pairs:
        database.set_datacron_requirement_stats(req_id, stat_pairs, guild_id=guild_id)
    return _redirect_season(set_id, notice="Требование добавлено.")


@router.post("/{set_id}/add-focused", response_class=HTMLResponse)
async def add_focused_requirement(
    set_id: int,
    character_key: str = Form(...),
    required_level: int = Form(...),
    pack: str = Form(""),
    priority: str = Form(PRIORITY_REQUIRED),
    comment: str = Form(""),
    user: dict = Depends(require_officer_access),
):
    guild_id = user["guild_id"]
    catalog = await _safe_catalog()
    if not _is_valid_season(catalog, set_id):
        return _redirect_season(set_id, error="Сезон не найден в справочнике.")
    if not _is_valid_focused_character(catalog, set_id, character_key):
        return _redirect_season(set_id, error="Некорректный персонаж — выберите вариант из подсказок.")
    if required_level < 1:
        return _redirect_season(set_id, error="Уровень должен быть не меньше 1.")

    database.add_datacron_focused_requirement(
        set_id, pack.strip() or None, character_key, required_level, comment.strip() or None,
        user["discord_id"], priority, guild_id=guild_id,
    )
    return _redirect_season(set_id, notice="Спец. требование добавлено.")


# =====================================================================
# Добавить альтернативу существующему уровню (аналог /дк_требования добавить_альтернативу)
# =====================================================================
@router.post("/{set_id}/requirements/{req_id}/add-alt", response_class=HTMLResponse)
async def add_alt(set_id: int, req_id: int, level: int = Form(...), value: str = Form(...), user: dict = Depends(require_officer_access)):
    guild_id = user["guild_id"]
    if level not in (3, 6, 9):
        return _redirect_season(set_id, error="Некорректный уровень (должен быть 3, 6 или 9).")
    row = database.get_datacron_requirement(req_id, guild_id=guild_id)
    if not row:
        return _redirect_season(set_id, error=f"Требование #{req_id} не найдено.")
    _, row_set_id, pack, l3, l6, l9, comment, _, _, priority, stats = row

    catalog = await _safe_catalog()
    if value in (DATACRON_ANY, DATACRON_NONE) or not _is_valid_level_value(catalog, row_set_id, level, value):
        return _redirect_season(set_id, error="Некорректный вариант — выберите из подсказок.")

    current = {3: l3, 6: l6, 9: l9}.get(level, DATACRON_NONE)
    if current == DATACRON_ANY:
        return _redirect_season(set_id, error="У этого уровня уже стоит «Любой» — добавлять альтернативы некуда.")
    existing_parts = current.split(",") if current != DATACRON_NONE else []
    if value in existing_parts:
        return _redirect_season(set_id, notice="Этот вариант уже указан в требовании.")

    new_value = value if current == DATACRON_NONE else f"{current},{value}"
    new_l3, new_l6, new_l9 = l3, l6, l9
    if level == 3:
        new_l3 = new_value
    elif level == 6:
        new_l6 = new_value
    else:
        new_l9 = new_value

    database.update_datacron_requirement(req_id, row_set_id, pack, new_l3, new_l6, new_l9, comment, priority, guild_id=guild_id, stats=stats)
    return _redirect_season(set_id, notice=f"Требование #{req_id} дополнено альтернативой.")


# =====================================================================
# Редактирование / удаление (полная замена — без патч-семантики Discord-версии)
# =====================================================================
@router.post("/{set_id}/requirements/{req_id}/edit", response_class=HTMLResponse)
async def edit_base_requirement(
    set_id: int,
    req_id: int,
    pack: str = Form(""),
    priority: str = Form(...),
    stats: str = Form(""),
    stat1: str = Form(""), value1: float = Form(None),
    stat2: str = Form(""), value2: float = Form(None),
    stat3: str = Form(""), value3: float = Form(None),
    stat4: str = Form(""), value4: float = Form(None),
    stat5: str = Form(""), value5: float = Form(None),
    comment: str = Form(""),
    user: dict = Depends(require_officer_access),
):
    guild_id = user["guild_id"]
    row = database.get_datacron_requirement(req_id, guild_id=guild_id)
    if not row:
        return _redirect_season(set_id, error=f"Требование #{req_id} не найдено.")
    _, row_set_id, _cur_pack, l3, l6, l9, _cur_comment, _, _, _cur_priority, _cur_stats = row
    stat_pairs, stat_error = _stat_pairs_from_form([stat1, stat2, stat3, stat4, stat5], [value1, value2, value3, value4, value5])
    if stat_error:
        return _redirect_season(set_id, error=stat_error)
    database.update_datacron_requirement(req_id, row_set_id, pack.strip() or None, l3, l6, l9, comment.strip() or None, priority, guild_id=guild_id, stats=stats.strip() or None)
    database.set_datacron_requirement_stats(req_id, stat_pairs, guild_id=guild_id)  # полная замена (веб = не патч-семантика)
    return _redirect_season(set_id, notice=f"Требование #{req_id} обновлено.")


@router.post("/{set_id}/requirements/{req_id}/delete", response_class=HTMLResponse)
async def delete_base_requirement(set_id: int, req_id: int, user: dict = Depends(require_officer_access)):
    database.delete_datacron_requirement(req_id, guild_id=user["guild_id"])
    return _redirect_season(set_id, notice=f"Требование #{req_id} удалено.")


@router.post("/{set_id}/focused/{req_id}/edit", response_class=HTMLResponse)
async def edit_focused_requirement(
    set_id: int,
    req_id: int,
    required_level: int = Form(...),
    pack: str = Form(""),
    priority: str = Form(...),
    comment: str = Form(""),
    user: dict = Depends(require_officer_access),
):
    guild_id = user["guild_id"]
    row = database.get_datacron_focused_requirement(req_id, guild_id=guild_id)
    if not row:
        return _redirect_season(set_id, error=f"Спец. требование F{req_id} не найдено.")
    if required_level < 1:
        return _redirect_season(set_id, error="Уровень должен быть не меньше 1.")
    _, row_set_id, _cur_pack, character_key, _cur_level, _cur_comment, _, _, _cur_priority = row
    database.update_datacron_focused_requirement(req_id, row_set_id, pack.strip() or None, character_key, required_level, comment.strip() or None, priority, guild_id=guild_id)
    return _redirect_season(set_id, notice=f"Спец. требование F{req_id} обновлено.")


@router.post("/{set_id}/focused/{req_id}/delete", response_class=HTMLResponse)
async def delete_focused_requirement(set_id: int, req_id: int, user: dict = Depends(require_officer_access)):
    database.delete_datacron_focused_requirement(req_id, guild_id=user["guild_id"])
    return _redirect_season(set_id, notice=f"Спец. требование F{req_id} удалено.")


# =====================================================================
# Очистка сезона — единый POST с подтверждением через JS confirm() на клиенте
# (счётчики уже видны на странице сезона, в отличие от Discord, где ephemeral-команда
# не может показать их иначе как отдельным ответом — здесь двухшаговый POST не нужен).
# =====================================================================
@router.post("/{set_id}/clear", response_class=HTMLResponse)
async def clear_season(set_id: int, user: dict = Depends(require_officer_access)):
    guild_id = user["guild_id"]
    deleted_base = database.delete_datacron_requirements_by_set(set_id, guild_id=guild_id)
    deleted_focused = database.delete_datacron_focused_requirements_by_set(set_id, guild_id=guild_id)
    return _redirect_list(notice=f"Сезон {set_id}: удалено {deleted_base} обычных + {deleted_focused} спец. требований.")


# =====================================================================
# Живой поиск фокус-персонажей (используется JS-виджетом data-live-search) —
# список бонусов уровня 3/6/9 больше не ищется через этот паттерн, см.
# datacron_season.html: рендерится сразу как <select> из level_options
# (season_detail), список фокус-персонажей обычно намного длиннее, там
# live-search по-прежнему оправдан.
# =====================================================================
@router.get("/api/focused-characters/{season}", response_class=JSONResponse)
async def focused_characters_search(season: int, q: str = "", user: dict = Depends(require_officer_access)):
    catalog = await _safe_catalog()
    season_data = catalog["seasons"].get(season) if catalog else None
    if not season_data:
        return []
    search = q.lower().strip()
    options = []
    for char_key, label, max_tier in season_data.get("focused", []):
        full_label = f"{label} (макс. уровень {max_tier})"
        if not search or search in full_label.lower():
            options.append({"value": char_key, "label": full_label})
    return options[:50]


# =====================================================================
# Проверка датакронов игрока/гильдии — GET-форма (в т.ч. чтобы результат
# можно было передать ссылкой), результат считается на лету через живой Comlink.
# =====================================================================
async def _build_player_report(catalog, set_id, ally_code, player_name, requirements, focused_requirements, guild_id):
    comlink = _get_comlink()
    try:
        player = await asyncio.wait_for(asyncio.to_thread(comlink.get_player, allycode=ally_code), timeout=15.0)
    except asyncio.TimeoutError:
        return {"error": "Запрос к Comlink занял слишком много времени."}
    except Exception as e:
        return {"error": f"Ошибка получения данных игрока: {e}"}

    season_data = catalog["seasons"].get(set_id) if catalog else None

    def level_label(level_num, value):
        options = season_data[f"level{level_num}"] if season_data else []
        return _level_label(options, value)

    rows_by_priority = {p: [] for p in PRIORITY_ORDER}
    totals = {p: {"matched": 0, "total": 0} for p in PRIORITY_ORDER}

    if requirements:
        owned = _extract_player_base_datacrons(player, set_id)
        pairs = _match_requirements(requirements, owned)
        stats_by_req = database.get_datacron_requirement_stats_by_set(set_id, guild_id=guild_id)
        for req, match in pairs:
            req_id, _, pack, l3, l6, l9, comment, _, _, priority, stats = req
            closed = []
            if match:
                m = match["levels"]
                closed = [level_label(n, m[n]) for n in (3, 6, 9) if m[n]]
            stat_reqs = stats_by_req.get(req_id)
            stat_checks = _check_requirement_stats(stat_reqs, match["stats"] if match else {}) if stat_reqs else []
            stat_checks_labeled = [
                {"label": DATACRON_STAT_LABELS.get(sid, str(sid)), "min_value": min_v, "actual": actual, "ok": ok}
                for sid, min_v, actual, ok in stat_checks
            ]
            bucket = rows_by_priority.get(priority, rows_by_priority[PRIORITY_REQUIRED])
            bucket.append({
                "kind": "base", "pack": pack,
                "l3": level_label(3, l3), "l6": level_label(6, l6), "l9": level_label(9, l9),
                "comment": comment, "stats": stats, "matched": bool(match), "closed": closed,
                "stat_checks": stat_checks_labeled,
                "stat_warning": bool(match) and any(not c["ok"] for c in stat_checks_labeled),
            })
            tb = totals.get(priority, totals[PRIORITY_REQUIRED])
            tb["total"] += 1
            if match:
                tb["matched"] += 1

    if focused_requirements:
        owned_focused = _extract_player_focused_datacrons(player, set_id)
        for req in focused_requirements:
            _, _, pack, character_key, required_level, comment, _, _, priority = req
            char_label = _focused_char_label(catalog, set_id, character_key)
            current_level = owned_focused.get(character_key, 0)
            ok = current_level >= required_level
            bucket = rows_by_priority.get(priority, rows_by_priority[PRIORITY_REQUIRED])
            bucket.append({
                "kind": "focused", "pack": pack, "character_label": char_label,
                "required_level": required_level, "current_level": current_level,
                "comment": comment, "matched": ok, "closed": [],
            })
            tb = totals.get(priority, totals[PRIORITY_REQUIRED])
            tb["total"] += 1
            if ok:
                tb["matched"] += 1

    return {"player_name": player_name, "rows_by_priority": rows_by_priority, "totals": totals}


async def _build_guild_report(set_id, requirements, focused_requirements, guild_id):
    roster = database.get_all_user_mappings(guild_id)  # [(discord_id, ally_code, name), ...]
    if not roster:
        return {"error": "Никто из гильдии не зарегистрирован (/registration) — проверять некого."}

    comlink = _get_comlink()
    semaphore = asyncio.Semaphore(6)

    async def fetch_one(ally_code, name):
        async with semaphore:
            try:
                player = await asyncio.wait_for(asyncio.to_thread(comlink.get_player, allycode=ally_code), timeout=15.0)
            except Exception:
                return name, None
            return name, player

    results = await asyncio.gather(*(fetch_one(ally_code, name) for _discord_id, ally_code, name in roster))

    missing_required, fully_compliant, failed = [], [], []
    for name, player in results:
        if player is None:
            failed.append(name)
            continue
        counts = _match_counts(player, set_id, requirements, focused_requirements)
        required = counts[PRIORITY_REQUIRED]
        if required["total"] > 0 and required["matched"] < required["total"]:
            parts = [f"{PRIORITY_LABELS[p]}: {counts[p]['matched']}/{counts[p]['total']}" for p in PRIORITY_ORDER if counts[p]["total"] > 0]
            missing_required.append({"name": name, "detail": ", ".join(parts)})
        else:
            fully_compliant.append(name)

    missing_required.sort(key=lambda r: r["name"].lower())
    fully_compliant.sort(key=str.lower)
    return {
        "guild_mode": True, "missing_required": missing_required,
        "fully_compliant": fully_compliant, "failed": failed, "total": len(results),
    }
