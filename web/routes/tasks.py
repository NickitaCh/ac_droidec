"""Веб-управление задачами на прокачку (аналог /задачи в Discord) — список с
фильтром/группировкой по игроку, единая форма постановки (игроки + один юнит или цель
«Руководства»), редактирование (значение цели + дедлайн), перенос в архив и удаление
(по одному и целой группой), плюс предзаполнение формы из гильдийского отчёта по плейту
(кнопка "+ задача" на /stats-check).
Аудит выполнения остаётся только в боте (cogs/tasks.py::tasks_audit_loop); просрочка
и напоминания — там же (tasks_notify_loop) — веб лишь читает уже проставленный ботом
статус и настраивает время рассылки (guilds.tasks_notify_time, см. /settings)."""

import asyncio
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

import database
from services.config_status import config_warning_html
from services.units_sync import sync_units
from services import discord_rest, feature_flags, journey_guide
from services.dashboard_data import failed_task_activity_url, task_status_badge
from web.deps import require_officer_access

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent.parent / "templates"))
MSK = ZoneInfo("Europe/Moscow")

TARGET_TYPE_OPTIONS = [
    ("stars", "⭐ Звёзды (1-7)"),
    ("relic", "♦️ Реликвия (0-10)"),
    ("gear", "⚙️ Снаряжение (1-13)"),
    ("omicron", "🧬 Омикрон"),
]
TARGET_TYPE_LABELS = dict(TARGET_TYPE_OPTIONS)

# Фильтр по статусу (?status=) и счётчики в шапке. ACTIVE делится на «Назначено» /
# «В работе» по in_progress; ACTIVE в ссылке (старые ссылки с главной) = оба сразу.
STATE_KEYS = ("ASSIGNED", "IN_PROGRESS", "FAILED", "COMPLETED")


def _task_state(status: str, in_progress) -> str:
    if status == "ACTIVE":
        return "IN_PROGRESS" if in_progress else "ASSIGNED"
    return status


def _status_label(status: str, in_progress) -> str:
    """ACTIVE различается на "Назначено"/"В работе" по in_progress — см.
    database.update_task_progress / cogs/tasks.py::TasksCog._status_label (тот же
    паттерн, продублирован здесь по общей для этого файла конвенции — см.
    _target_label ниже, который так же дублирует cogs/tasks.py)."""
    if status == "COMPLETED":
        return "Выполнено"
    if status == "FAILED":
        return "Просрочено"
    if status == "LEFT":
        return "Игрок выбыл"
    return "В работе" if in_progress else "Назначено"


def _progress_label(initial_value, current_value) -> str:
    # Всегда показываем и стартовое, и текущее значение (даже когда они совпадают) —
    # раньше при равенстве показывался один голый current_value, из-за чего пропадала
    # видимая информация о состоянии юнита на момент постановки задачи (Ricardo,
    # Discord-тред "Гайд по АС Боту", 2026-09-08).
    if initial_value is None or current_value is None:
        return "—"
    return f"{initial_value} → {current_value}"


def _format_resolved_at(resolved_at: str | None) -> str | None:
    """resolved_at хранится как datetime('now') (UTC) — конвертируем в МСК для показа
    времени завершения/просрочки задачи (Ricardo, тот же тред: "добавить информацию о
    времени, когда задача была выполнена"). Тот же паттерн конвертации, что
    services/dashboard_data.py::_sync_status (last_sync)."""
    if not resolved_at:
        return None
    try:
        dt = datetime.strptime(resolved_at, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc).astimezone(MSK)
    except ValueError:
        return None
    return dt.strftime("%d.%m.%Y %H:%M (МСК)")


def _get_comlink():
    # См. web/routes/registration.py::_get_comlink — веб-процесс не поднимает
    # диска-клиента, строит свой SwgohComlink на тот же сайдкар.
    from swgoh_comlink import SwgohComlink
    return SwgohComlink(url="http://localhost:3000")


def _fetch_unit_data(comlink, ally_code: str, base_id: str) -> dict | None:
    """Та же логика, что cogs/tasks.py::TasksCog._fetch_unit_data — синхронная
    версия (веб-процесс уже вызывает Comlink напрямую без to_thread, см.
    stat_forecast.py и другие роуты этого файла)."""
    try:
        player_data = comlink.get_player(ally_code)
    except Exception:
        return None
    roster = player_data.get('rosterUnit') or player_data.get('roster')
    if not roster:
        return None
    for u in roster:
        u_id = u.get('baseId') or u.get('definitionId', '').split(':')[0]
        if u_id == base_id:
            return u
    return None


def _current_progress_value(unit_data: dict, target_type: str, target_value: str) -> str:
    # stars/relic/gear — общая с ботом логика (смещение +2 у реликвии и т.п.).
    if target_type in ('stars', 'relic', 'gear'):
        return journey_guide.unit_progress(unit_data, target_type)
    if target_type == 'omicron':
        for skill in unit_data.get('skill', []):
            if skill.get('id') == target_value:
                return str(skill.get('tier', 0))
        return "0"
    return ""


def _is_target_completed(unit_data: dict, target_type: str, target_value: str, skill_thresholds: dict) -> bool:
    if target_type in ('stars', 'relic', 'gear'):
        return journey_guide.unit_meets(unit_data, target_type, target_value)
    if target_type == 'omicron':
        omicron_tier = skill_thresholds.get(target_value, (None, None))[1]
        if omicron_tier is None:
            return False
        return any(
            skill.get('id') == target_value and skill.get('tier', -1) >= omicron_tier
            for skill in unit_data.get('skill', [])
        )
    return False


def _omicron_options(base_id: str) -> list:
    skill_ids = database.get_all_unit_omicron_skills().get(base_id, [])
    if not skill_ids:
        return []
    info = database.get_skill_display_info(skill_ids)
    options = []
    for skill_id in skill_ids:
        name, _ability_id, ability_type, _omicron_mode = info.get(skill_id, (skill_id, None, None, None))
        label = f"{name or skill_id}" + (f" ({ability_type})" if ability_type else "")
        options.append({"skill_id": skill_id, "label": label})
    return options


def _target_label(target_type: str, target_value: str) -> str:
    if target_type == "stars":
        return f"⭐ Звёзды {target_value}"
    if target_type in ("relic", "gear"):
        return journey_guide.target_label(target_type, target_value)
    if target_type == "omicron":
        info = database.get_skill_display_info([target_value]).get(target_value)
        name = info[0] if info and info[0] else target_value
        return f"🧬 Омикрон: {name}"
    return target_value


def _validate_target(base_id: str, target_type: str, target_value: str) -> str | None:
    """None — ок, иначе текст ошибки для показа пользователю."""
    if target_type not in TARGET_TYPE_LABELS:
        return "Некорректный тип цели."
    target_value = target_value.strip()
    if not target_value:
        return "Не указано целевое значение."
    if target_type == "omicron":
        valid_skills = database.get_all_unit_omicron_skills().get(base_id, [])
        if target_value not in valid_skills:
            return "Для омикрона выберите конкретную способность из подсказки (не свободный текст)."
    elif not target_value.isdigit():
        return "Для звёзд/реликвии/снаряжения значение должно быть числом."
    return None


async def _mention_new_tasks(guild_id: int, lines_by_code: dict) -> None:
    """Тег игроков в канале задач о новых задачах — по флагу tasks_mention (включают
    гильдии сами, у AC выключен). Раньше это делала Discord-команда /задачи добавить;
    постановка теперь только на сайте (2026-10-14). Сбой отправки задачу не отменяет."""
    if not lines_by_code or not feature_flags.is_enabled(guild_id, "tasks_mention"):
        return
    channel_id = (database.get_guild_config(guild_id) or {}).get("tasks_log_channel_id")
    if not channel_id:
        return
    lines, mention_ids = [], []
    for code, text in lines_by_code.items():
        discord_id = database.get_discord_id_for_ally(code, guild_id=guild_id)
        if discord_id:
            mention_ids.append(str(discord_id))
            lines.append(f"<@{discord_id}> — {text}")
    try:
        for chunk in discord_rest.chunk_lines("📝 Новые задачи на прокачку:", lines):
            await discord_rest.send_message(str(channel_id), chunk, mention_ids)
    except Exception as e:
        print(f"⚠️ [web/tasks] Не удалось тегнуть игроков о новых задачах: {e}")


def _validate_deadline(deadline: str) -> str | None:
    """None — ок, иначе текст ошибки. Дедлайн вводится датой (не количеством дней,
    см. Discord-тред "Гайд по АС Боту", 2026-09-04) — <input type="date"> в шаблоне
    уже даёт и календарь, и ручной ввод, нормализуя формат до ГГГГ-ММ-ДД."""
    deadline = deadline.strip()
    try:
        parsed = datetime.strptime(deadline, "%Y-%m-%d").date()
    except ValueError:
        return "Некорректная дата дедлайна (ГГГГ-ММ-ДД)."
    if parsed < datetime.now().date():
        return "Дедлайн не может быть в прошлом."
    return None


@router.get("", response_class=HTMLResponse)
async def tasks_list(request: Request, user: dict = Depends(feature_flags.require_feature("tasks"))):
    guild_id = user["guild_id"]
    status_filter = request.query_params.get("status")
    view = request.query_params.get("view", "flat")

    rows = database.get_all_tasks(guild_id)
    # Имена — включая выбывших из гильдии (их задачи в архиве со статусом LEFT), а список
    # для выбора игрока в формах — только текущий состав.
    names_by_code = database.get_player_names(guild_id)
    roster_names = {code: name for _, code, name in database.get_all_user_mappings(guild_id)}
    departed_codes = set(database.get_departed_players(guild_id))
    unit_names = database.get_game_unit_names([r[2] for r in rows])
    creator_ids = {r[11] for r in rows if r[11]}
    creator_names = {cid: (database.get_username_for_discord_id(cid) or cid) for cid in creator_ids}

    all_ctx = []
    for (task_id, ally_code, base_id, target_type, target_value, deadline, status, batch_id,
         initial_value, current_value, in_progress, created_by, resolved_at, archived_at, is_personal,
         date_created) in rows:
        archived = database.is_task_archived(resolved_at, status, archived_at)
        all_ctx.append({
            "task_id": task_id, "ally_code": ally_code, "base_id": base_id,
            "player_name": names_by_code.get(ally_code, ally_code),
            "unit_name": unit_names.get(base_id) or base_id,
            "target_type": target_type, "target_value": target_value,
            "target_label": _target_label(target_type, target_value),
            "deadline": deadline, "status": status,
            "state": _task_state(status, in_progress),
            "status_label": _status_label(status, in_progress),
            "status_badge": task_status_badge(status, in_progress),
            "progress": _progress_label(initial_value, current_value),
            "created_by_name": creator_names.get(created_by, "—"),
            "batch_id": batch_id,
            "archived": archived,
            "can_archive": status in ("COMPLETED", "FAILED") and not archived,
            "departed": ally_code in departed_codes,
            "personal": bool(is_personal),
            "resolved_at_text": _format_resolved_at(resolved_at),
            "activity_url": failed_task_activity_url(status, ally_code, date_created, resolved_at, deadline),
        })

    # "Архив" — отдельная вкладка: выполненные через TASK_ARCHIVE_AFTER_DAYS дней или
    # кнопкой, просроченные — только кнопкой (database.is_task_archived). "Список"/"По
    # игрокам" — только текущие задачи, как и в отчётах бота.
    # Задачи выбывших из гильдии — своя вкладка "Выбывшие" (только они, любой статус),
    # ни в текущих, ни в "Архиве" их нет.
    # Личные задачи (игрок поставил себе сам) — только на вкладке "Личные", без архива:
    # в счётчики, списки и отчёты гильдии не попадают.
    personal = [t for t in all_ctx if t["personal"] and not t["departed"]]
    current = [t for t in all_ctx if not t["archived"] and not t["departed"] and not t["personal"]]
    archived = [t for t in all_ctx if t["archived"] and not t["departed"] and not t["personal"]]
    if view == "departed":
        scope = [t for t in all_ctx if t["departed"]]
    elif view == "personal":
        scope = personal
    elif view == "archive":
        scope = archived
    else:
        scope = current

    # Счётчики в шапке — всегда по текущим задачам + размер архива, вне зависимости от вкладки.
    counts = {key: 0 for key in STATE_KEYS}
    for t in current:
        counts[t["state"]] = counts.get(t["state"], 0) + 1
    counts["ARCHIVE"] = len(archived)

    if status_filter == "ACTIVE":
        tasks_ctx = [t for t in scope if t["status"] == "ACTIVE"]
    else:
        tasks_ctx = [t for t in scope if not status_filter or t["state"] == status_filter]

    # Омикрон-варианты для инлайн-редактирования — только для реально показанных задач
    # этого типа, не весь справочник разом.
    omicron_options_by_unit = {
        t["base_id"]: _omicron_options(t["base_id"])
        for t in tasks_ctx if t["target_type"] == "omicron"
    }

    # Групповые задания (массовая постановка / из отчёта плейта) — для скрытой ссылки
    # "отменить группу" внизу страницы. Раньше жило в отдельной плашке над формами
    # (убрана — Nick, 2026-09-08: "неинформативная совсем"), теперь просто данные
    # для незаметного, но доступного блока в самом низу (см. tasks.html).
    batch_groups = {}
    for t in tasks_ctx:
        if t["batch_id"] and t["status"] == "ACTIVE":
            # Группа «Руководства» — разные герои у одного игрока: перечисляем всех, а не
            # первого попавшегося (Ricardo, 2026-10-13: группа выглядела как один «Командир
            # снежных штурмовиков»). Игроки и цели — без повторов.
            g = batch_groups.setdefault(t["batch_id"], {
                "count": 0, "players": [], "units": [], "targets": [], "deadline": t["deadline"],
            })
            g["count"] += 1
            for key, value in (("players", t["player_name"]), ("units", t["unit_name"]), ("targets", t["target_label"])):
                if value not in g[key]:
                    g[key].append(value)
    batch_list = [{"batch_id": bid, **g} for bid, g in batch_groups.items()]

    players_view = []
    if view == "players":
        by_player = {}
        for t in tasks_ctx:
            if t["status"] in ("ACTIVE", "FAILED"):
                by_player.setdefault(t["ally_code"], {"name": t["player_name"], "tasks": []})["tasks"].append(t)
        # Внутри игрока — по дедлайну (ближе срок = выше в списке), см. Ricardo,
        # Discord-тред "Гайд по АС Боту", 2026-09-08.
        for p in by_player.values():
            p["tasks"].sort(key=lambda t: t["deadline"])
        players_view = sorted(by_player.values(), key=lambda p: (-len(p["tasks"]), p["name"].lower()))

    roster = sorted(roster_names.items(), key=lambda kv: kv[1].lower())

    prefill = {
        "ally_code": request.query_params.get("prefill_ally", ""),
        "unit": request.query_params.get("prefill_unit", ""),
        "type": request.query_params.get("prefill_type", ""),
        "value": request.query_params.get("prefill_value", ""),
    }
    prefill["active"] = bool(prefill["ally_code"] or prefill["unit"])
    prefill["unit_name"] = (database.get_game_unit_name(prefill["unit"]) or prefill["unit"]) if prefill["unit"] else ""

    return templates.TemplateResponse(request, "tasks.html", {
        "user": user,
        "tasks": tasks_ctx,
        "status_filter": status_filter,
        "view": view,
        "counts": counts,
        "batch_list": batch_list,
        "players_view": players_view,
        "omicron_options_by_unit": omicron_options_by_unit,
        "target_type_options": TARGET_TYPE_OPTIONS,
        "guide_options": journey_guide.guide_options(),
        "roster": roster,
        "prefill": prefill,
        "error": request.query_params.get("error"),
        "notice": request.query_params.get("notice"),
        "warn": request.query_params.get("warn"),
        "status_labels": {"ASSIGNED": "Назначено", "IN_PROGRESS": "В работе", "FAILED": "Просрочено",
                          "COMPLETED": "Выполнено", "ACTIVE": "Назначено и в работе"},
        "synced": request.query_params.get("synced"),
        "archive_after_days": database.TASK_ARCHIVE_AFTER_DAYS,
        "departed_count": len({t["ally_code"] for t in all_ctx if t["departed"]}),
        "personal_count": len(personal),
        "config_warning": config_warning_html(database.get_guild_config(guild_id), "tasks"),
    })


@router.get("/api/players", response_class=JSONResponse)
async def players_search(q: str = "", user: dict = Depends(feature_flags.require_feature("tasks"))):
    if not q or len(q.strip()) < 2:
        return []
    q_lower = q.strip().lower()
    rows = database.get_all_user_mappings(user["guild_id"])
    matches = [
        {"ally_code": ally_code, "name": name}
        for _, ally_code, name in rows
        if name and q_lower in name.lower()
    ]
    matches.sort(key=lambda m: m["name"].lower())
    return matches[:20]


@router.get("/api/units", response_class=JSONResponse)
async def units_search(q: str = "", user: dict = Depends(feature_flags.require_feature("tasks"))):
    if not q or len(q.strip()) < 2:
        return []
    rows = database.search_game_units(q.strip(), limit=20)
    return [{"base_id": base_id, "name": name} for base_id, name in rows]


@router.get("/api/omicron-skills", response_class=JSONResponse)
async def omicron_skills(base_id: str = "", user: dict = Depends(feature_flags.require_feature("tasks"))):
    if not base_id:
        return []
    return _omicron_options(base_id)


@router.get("/api/guides", response_class=JSONResponse)
async def guides_search(q: str = "", user: dict = Depends(feature_flags.require_feature("tasks"))):
    """Поиск цели «Руководства» по имени (вместо длинного выпадающего списка)."""
    q_lower = q.strip().lower()
    if len(q_lower) < 2:
        return []
    return [
        {"base_id": g["base_id"], "name": f"{g['name']} — {g['kind_label'].lower()}, героев: {g['count']}"}
        for g in journey_guide.guide_options() if q_lower in g["name"].lower()
    ][:20]


@router.post("/api/bulk-preview", response_class=JSONResponse)
async def bulk_preview(
    ally_codes: list = Form(default=[]),
    base_id: str = Form(...),
    target_type: str = Form(...),
    target_value: str = Form(...),
    user: dict = Depends(feature_flags.require_feature("tasks")),
):
    """Предпросмотр массовой постановки — сколько реально будет создано, сколько
    пропустится (дубль / уже выполнено), с именами игроков — для модалки подтверждения
    перед /tasks/add-bulk (см. Discord-тред "Гайд по АС Боту", отложенный пункт
    "сколько человек уже имеют эту задачу"). Не пишет в базу — только считает, по той
    же логике дублей/completion, что и сам task_add_bulk ниже."""
    guild_id = user["guild_id"]

    unit_name = database.get_game_unit_name(base_id)
    if not unit_name:
        return JSONResponse({"error": f"Юнит {base_id} не найден в справочнике."}, status_code=400)

    names_by_code = {code: name for _, code, name in database.get_all_user_mappings(guild_id)}
    valid_codes = [c for c in ally_codes if c in names_by_code]
    if not valid_codes:
        return JSONResponse({"error": "Не выбрано ни одного игрока из состава гильдии."}, status_code=400)

    err = _validate_target(base_id, target_type, target_value)
    if err:
        return JSONResponse({"error": err}, status_code=400)

    target_value = target_value.strip()
    skill_thresholds = database.get_all_skill_tier_thresholds() if target_type == "omicron" else {}
    try:
        comlink = _get_comlink()
    except Exception:
        comlink = None

    create, duplicate, done = [], [], []
    for code in valid_codes:
        name = names_by_code[code]
        if database.get_active_task_for_unit(guild_id, code, base_id):
            duplicate.append(name)
            continue
        if comlink is not None:
            try:
                unit_data = _fetch_unit_data(comlink, code, base_id)
                if unit_data is not None and _is_target_completed(unit_data, target_type, target_value, skill_thresholds):
                    done.append(name)
                    continue
            except Exception:
                pass
        create.append(name)

    return {"create": create, "duplicate": duplicate, "done": done}


@router.post("/add-bulk", response_class=HTMLResponse)
async def task_add_bulk(
    ally_codes: list = Form(default=[]),
    base_id: str = Form(...),
    target_type: str = Form(...),
    target_value: str = Form(...),
    deadline: str = Form(...),
    user: dict = Depends(feature_flags.require_feature("tasks")),
):
    guild_id = user["guild_id"]

    unit_name = database.get_game_unit_name(base_id)
    if not unit_name:
        return RedirectResponse(f"/tasks?{urlencode({'error': f'Юнит {base_id} не найден в справочнике.'})}", status_code=303)

    names_by_code = {code: name for _, code, name in database.get_all_user_mappings(guild_id)}
    valid_codes = [c for c in ally_codes if c in names_by_code]
    if not valid_codes:
        return RedirectResponse(f"/tasks?{urlencode({'error': 'Не выбрано ни одного игрока из состава гильдии.'})}", status_code=303)

    err = _validate_target(base_id, target_type, target_value)
    if err:
        return RedirectResponse(f"/tasks?{urlencode({'error': err})}", status_code=303)

    err = _validate_deadline(deadline)
    if err:
        return RedirectResponse(f"/tasks?{urlencode({'error': err})}", status_code=303)

    batch_id = uuid.uuid4().hex[:12]
    target_value = target_value.strip()
    skill_thresholds = database.get_all_skill_tier_thresholds() if target_type == "omicron" else {}
    try:
        comlink = _get_comlink()
    except Exception:
        comlink = None

    created, skipped_dup, skipped_done = 0, 0, 0
    created_codes = []
    for code in valid_codes:
        # Дубль — уже есть активная задача на этот юнит для этого игрока (пропускаем
        # молча при массовой постановке, см. Ricardo 2026-09-04: "просто не добавлять
        # в таблицу дубли" — в отличие от точечной постановки, где это явная ошибка).
        if database.get_active_task_for_unit(guild_id, code, base_id):
            skipped_dup += 1
            continue

        initial_value = None
        if comlink is not None:
            try:
                unit_data = _fetch_unit_data(comlink, code, base_id)
                if unit_data is not None:
                    if _is_target_completed(unit_data, target_type, target_value, skill_thresholds):
                        skipped_done += 1
                        continue
                    initial_value = _current_progress_value(unit_data, target_type, target_value)
            except Exception:
                pass

        database.add_task(code, base_id, target_type, target_value, deadline, str(user["discord_id"]),
                           guild_id=guild_id, batch_id=batch_id, initial_value=initial_value)
        created += 1
        created_codes.append(code)

    target_text = f"{unit_name}: {_target_label(target_type, target_value)}, срок до {deadline}"
    await _mention_new_tasks(guild_id, {code: target_text for code in created_codes})

    parts = [f"поставлено: {created}"]
    if skipped_dup:
        parts.append(f"пропущено (уже назначено): {skipped_dup}")
    if skipped_done:
        parts.append(f"пропущено (уже выполнено): {skipped_done}")
    return RedirectResponse(f"/tasks?{urlencode({'notice': ', '.join(parts)})}", status_code=303)


# ---------------------------------------------------------------------------
# Постановка по «Руководству»: все герои, нужные для ГЛ / флагмана / путешествия
# (RicardoOneS, тред "Гайд по АС Боту", 2026-10-09: "по одному персу назначать муторно").
# ---------------------------------------------------------------------------

async def _guide_plans(guild_id: int, ally_codes: list, requirements: list) -> dict:
    """{ally_code: plan} — ростер каждого игрока грузится один раз, до 5 параллельно."""
    comlink = _get_comlink()
    sem = asyncio.Semaphore(5)

    async def one(code):
        async with sem:
            try:
                roster = journey_guide.roster_index(await asyncio.to_thread(comlink.get_player, code))
            except Exception:
                roster = None
        return code, journey_guide.plan_player(guild_id, code, requirements, roster)

    return dict(await asyncio.gather(*(one(c) for c in ally_codes)))


@router.get("/api/guide", response_class=JSONResponse)
async def guide_requirements(base_id: str = "", user: dict = Depends(feature_flags.require_feature("tasks"))):
    reqs = database.get_journey_guide_requirements_detail(base_id, user["guild_id"])
    names = database.get_game_unit_names([r[0] for r in reqs])
    # У героя эпохи две цели (звёзды + реликвия) — одна строка «7★ + R4».
    result = {}
    for unit_id, target_type, target_value, source, default_type, default_value, overridden in reqs:
        label = journey_guide.short_label(target_type, target_value)
        default_label = journey_guide.short_label(default_type, default_value)
        if unit_id in result:
            result[unit_id]["label"] += f" + {label}"
            result[unit_id]["default_label"] += f" + {default_label}"
            continue
        result[unit_id] = {
            "base_id": unit_id, "name": names.get(unit_id) or unit_id, "label": label,
            "target_type": target_type, "target_value": target_value,
            "editable": source == "entry", "overridden": bool(overridden), "default_label": default_label,
        }
    return list(result.values())


@router.post("/api/guide-levels", response_class=JSONResponse)
async def guide_levels_save(
    guide: str = Form(...),
    unit: list = Form(default=[]),
    target_type: list = Form(default=[]),
    target_value: list = Form(default=[]),
    user: dict = Depends(feature_flags.require_feature("tasks")),
):
    """Уровни гильдии для старого события (минимум для входа можно поднять). Сохраняются
    только отличия от игрового минимума; пустой список = сброс."""
    detail = {r[0]: r for r in database.get_journey_guide_requirements_detail(guide, user["guild_id"])}
    if not detail or any(r[3] != "entry" for r in detail.values()):
        return JSONResponse({"error": "Уровни можно менять только у старых событий."}, status_code=400)
    limits = {"stars": (1, 7), "relic": (0, 10), "gear": (1, 13)}
    overrides = {}
    for u, tt, tv in zip(unit, target_type, target_value):
        if u not in detail or tt not in limits or not str(tv).strip().isdigit():
            return JSONResponse({"error": "Некорректный уровень."}, status_code=400)
        lo, hi = limits[tt]
        if not lo <= int(tv) <= hi:
            return JSONResponse({"error": f"Значение вне диапазона {lo}–{hi}."}, status_code=400)
        default = (detail[u][4], detail[u][5])
        if (tt, str(int(tv))) != default:
            overrides[u] = (tt, str(int(tv)))
    database.set_journey_guide_overrides(user["guild_id"], guide, overrides)
    return {"ok": True, "overridden": len(overrides)}


@router.post("/api/guide-preview", response_class=JSONResponse)
async def guide_preview(
    ally_codes: list = Form(default=[]),
    guide: str = Form(...),
    user: dict = Depends(feature_flags.require_feature("tasks")),
):
    guild_id = user["guild_id"]
    requirements = database.get_journey_guide_requirements(guide, guild_id)
    if not requirements:
        return JSONResponse({"error": "Выберите цель из «Руководства»."}, status_code=400)
    names_by_code = {code: name for _, code, name in database.get_all_user_mappings(guild_id)}
    valid_codes = [c for c in ally_codes if c in names_by_code]
    if not valid_codes:
        return JSONResponse({"error": "Не выбрано ни одного игрока из состава гильдии."}, status_code=400)

    plans = await _guide_plans(guild_id, valid_codes, requirements)
    players = sorted(
        ({"name": names_by_code[c], "create": len(p["create"]), "done": len(p["done"]), "duplicate": len(p["duplicate"])}
         for c, p in plans.items()),
        key=lambda p: (-p["create"], p["name"].lower()),
    )
    return {"players": players, "total": len(journey_guide.group_by_unit(requirements)), "create": sum(p["create"] for p in players)}


@router.post("/add-guide", response_class=HTMLResponse)
async def task_add_guide(
    ally_codes: list = Form(default=[]),
    guide: str = Form(...),
    deadline: str = Form(...),
    user: dict = Depends(feature_flags.require_feature("tasks")),
):
    guild_id = user["guild_id"]
    requirements = database.get_journey_guide_requirements(guide, guild_id)
    if not requirements:
        return RedirectResponse(f"/tasks?{urlencode({'error': 'Выберите цель из «Руководства».'})}", status_code=303)
    names_by_code = {code: name for _, code, name in database.get_all_user_mappings(guild_id)}
    valid_codes = [c for c in ally_codes if c in names_by_code]
    if not valid_codes:
        return RedirectResponse(f"/tasks?{urlencode({'error': 'Не выбрано ни одного игрока из состава гильдии.'})}", status_code=303)
    err = _validate_deadline(deadline)
    if err:
        return RedirectResponse(f"/tasks?{urlencode({'error': err})}", status_code=303)

    plans = await _guide_plans(guild_id, valid_codes, requirements)
    batch_id = uuid.uuid4().hex[:12]
    guide_name = database.get_game_unit_name(guide) or guide
    mention_lines = {}
    created = done = duplicate = 0
    for code, plan in plans.items():
        for unit_id, target_type, target_value, initial in plan["create"]:
            database.add_task(code, unit_id, target_type, target_value, deadline, str(user["discord_id"]),
                              guild_id=guild_id, batch_id=batch_id, initial_value=initial)
        created += len(plan["create"])
        if plan["create"]:
            mention_lines[code] = (f"«Руководство» {guide_name}: задач {len(plan['create'])}, "
                                   f"срок до {deadline}")
        done += len(plan["done"])
        duplicate += len(plan["duplicate"])
    await _mention_new_tasks(guild_id, mention_lines)

    parts = [f"поставлено: {created}"]
    if duplicate:
        parts.append(f"пропущено (уже назначено): {duplicate}")
    if done:
        parts.append(f"пропущено (уже выполнено): {done}")
    return RedirectResponse(f"/tasks?{urlencode({'notice': ', '.join(parts)})}", status_code=303)


@router.post("/{task_id}/edit", response_class=HTMLResponse)
async def task_edit(
    task_id: int,
    target_value: str = Form(...),
    deadline: str = Form(""),
    user: dict = Depends(feature_flags.require_feature("tasks")),
):
    guild_id = user["guild_id"]
    task = database.get_task(task_id)
    if not task or task[7] != guild_id:
        return RedirectResponse(f"/tasks?{urlencode({'error': 'Задача не найдена.'})}", status_code=303)

    base_id, target_type = task[2], task[3]
    err = _validate_target(base_id, target_type, target_value)
    if err:
        return RedirectResponse(f"/tasks?{urlencode({'error': err})}", status_code=303)

    deadline = deadline.strip()
    is_personal = bool(task[15])
    if deadline or not is_personal:  # у личной задачи срок необязателен
        try:
            datetime.strptime(deadline, "%Y-%m-%d")
        except ValueError:
            return RedirectResponse(f"/tasks?{urlencode({'error': 'Некорректная дата дедлайна (ГГГГ-ММ-ДД).'})}", status_code=303)

    database.update_task(task_id, target_value=target_value.strip(), deadline=deadline)
    return RedirectResponse(f"/tasks?{urlencode({'notice': 'Задача обновлена'})}", status_code=303)


@router.post("/{task_id}/archive", response_class=HTMLResponse)
async def task_archive(task_id: int, user: dict = Depends(feature_flags.require_feature("tasks"))):
    task = database.get_task(task_id)
    if not task or task[7] != user["guild_id"]:
        return RedirectResponse(f"/tasks?{urlencode({'error': 'Задача не найдена.'})}", status_code=303)
    if task[6] not in ("COMPLETED", "FAILED"):
        return RedirectResponse(f"/tasks?{urlencode({'error': 'В архив переносятся только выполненные и просроченные задачи.'})}", status_code=303)
    database.archive_task(task_id)
    return RedirectResponse(f"/tasks?{urlencode({'notice': 'Задача перенесена в архив'})}", status_code=303)


@router.post("/{task_id}/delete", response_class=HTMLResponse)
async def task_delete(task_id: int, user: dict = Depends(feature_flags.require_feature("tasks"))):
    guild_id = user["guild_id"]
    task = database.get_task(task_id)
    if task and task[7] == guild_id:
        database.delete_task(task_id)
    return RedirectResponse(f"/tasks?{urlencode({'notice': 'Задача удалена'})}", status_code=303)


@router.post("/batch/{batch_id}/delete", response_class=HTMLResponse)
async def batch_delete(batch_id: str, user: dict = Depends(feature_flags.require_feature("tasks"))):
    deleted = database.delete_tasks_by_batch(batch_id, user["guild_id"])
    return RedirectResponse(f"/tasks?{urlencode({'notice': f'Отменено заданий: {deleted}'})}", status_code=303)


@router.post("/sync-units", response_class=HTMLResponse)
async def sync_units_now(user: dict = Depends(require_officer_access)):
    comlink = _get_comlink()
    try:
        total = await sync_units(comlink)
        await journey_guide.sync_journey_guide(comlink)
    except Exception as e:
        return RedirectResponse(f"/tasks?{urlencode({'error': f'Ошибка синхронизации: {e}'})}", status_code=303)
    return RedirectResponse(f"/tasks?{urlencode({'synced': str(total)})}", status_code=303)
