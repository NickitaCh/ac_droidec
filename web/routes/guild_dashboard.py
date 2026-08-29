import asyncio
import io
import json
import re
import zipfile
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import quote, urlencode
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

import database
import stat_engine
import tb_plan_reader
import tb_platoon_autofill
import tb_platoon_data
import tb_platoon_engine
import tb_platoon_filters
from cogs.violations import WARNS_STRUCTURE
from services import activity_diff, dashboard_data
from web.deps import require_guild_access

MSK = ZoneInfo("Europe/Moscow")


def _get_comlink():
    # См. web/routes/admin.py::_get_comlink / registration.py::_get_comlink — веб-процесс
    # не поднимает бота, строит свой SwgohComlink на тот же comlink-сайдкар.
    from swgoh_comlink import SwgohComlink
    return SwgohComlink(url="http://localhost:3000")


def _format_delta(delta: timedelta) -> str:
    total_minutes = round(delta.total_seconds() / 60)
    hours, minutes = divmod(abs(total_minutes), 60)
    parts = []
    if hours:
        parts.append(f"{hours} ч")
    parts.append(f"{minutes} мин")
    return " ".join(parts)


def _hours_label(hours: int) -> str:
    """Склонение "час"/"часа"/"часов" под PLAYER_STATS_SYNC_HOURS — значение конфигурируемое
    (сейчас 1), поэтому текст на /activity не может просто хардкодить "часов"."""
    n = abs(hours) % 100
    if 11 <= n <= 14:
        word = "часов"
    elif n % 10 == 1:
        word = "час"
    elif 2 <= n % 10 <= 4:
        word = "часа"
    else:
        word = "часов"
    return f"{hours} {word}"


def _sync_status_text(sync_status: dict) -> dict:
    """(last_sync, next_auto, sync_hours) -> человекочитаемые строки для панели на /activity."""
    last_sync_text = None
    if sync_status["last_sync"]:
        last_sync_text = sync_status["last_sync"].strftime("%d.%m.%Y %H:%M (МСК)")

    next_auto_text = None
    next_auto = sync_status["next_auto"]
    if next_auto:
        now = datetime.now(MSK)
        if next_auto <= now:
            next_auto_text = "вот-вот"
        else:
            next_auto_text = f"через {_format_delta(next_auto - now)}"

    return {
        "last_sync_text": last_sync_text,
        "next_auto_text": next_auto_text,
        "sync_hours_text": _hours_label(sync_status["sync_hours"]),
    }

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent.parent / "templates"))

# Подписи веток — намеренно те же самые, что в choices Discord-команды тб_отчет план
# (cogs/guild_events.py::tb_set_plan) для консистентности офицерского UX между ботом и
# вебом. Не путать с TB_CONFLICT_LABELS (cogs/guild_events.py) — тот словарь используется
# только как fallback-подпись в отчётах на чтение и расходится с этими ("01"->Dark там,
# "01"->Light здесь) — это существовавшее расхождение в самом боте, не веб-баг.
TB_PLAN_CONFLICT_OPTIONS = [
    {"value": "01", "label": "Light (красная)"},
    {"value": "02", "label": "Dark (жёлтая)"},
    {"value": "03", "label": "Mixed (синяя)"},
    {"value": "bonus", "label": "Бонус/ОЗ-зона"},
]

# Тот же паттерн, что и cogs/tb_order_image.py::LINK_RE — продублирован, а не
# импортирован, чтобы веб-процесс (без доступа к Discord-клиенту) не тянул за
# собой cogs/guild_events.py целиком ради одной регулярки (см. коммент выше про
# TB_PLAN_CONFLICT_OPTIONS — здесь тот же принцип дублирования ради развязки).
TB_THREAD_LINK_RE = re.compile(r"discord(?:app)?\.com/channels/(\d+)/(\d+)")


def _extract_thread_id(value: str) -> int | None:
    value = (value or "").strip()
    m = TB_THREAD_LINK_RE.search(value)
    if m:
        return int(m.group(2))
    if value.isdigit():
        return int(value)
    return None


@router.get("/tw", response_class=HTMLResponse)
async def tw_report(request: Request, user: dict = Depends(require_guild_access)):
    rows = dashboard_data.get_recent_tw_results(user["guild_id"], limit=50)
    wins = sum(1 for r in rows if r.result == "win")
    return templates.TemplateResponse(request, "tw_report.html", {
        "user": user,
        "rows": rows,
        "wins": wins,
        "tw_stats": dashboard_data.get_tw_stats(user["guild_id"]),
    })


@router.get("/tb", response_class=HTMLResponse)
async def tb_report(request: Request, user: dict = Depends(require_guild_access)):
    report = dashboard_data.get_tb_report(user["guild_id"])
    max_summary = report.latest[0].summary if report and report.latest else 0
    max_trend_total = max((t for _, t in report.event_totals), default=0) if report else 0
    return templates.TemplateResponse(request, "tb_report.html", {
        "user": user,
        "report": report,
        "max_summary": max_summary,
        "max_trend_total": max_trend_total,
    })


@router.get("/tb/player/{name}", response_class=HTMLResponse)
async def tb_player(name: str, request: Request, user: dict = Depends(require_guild_access)):
    report = dashboard_data.get_tb_player_report(user["guild_id"], name)
    if report is None:
        raise HTTPException(status_code=404, detail=f"Нет сохранённых данных ТБ для игрока «{name}»")
    return templates.TemplateResponse(request, "tb_player.html", {"user": user, "report": report})


@router.get("/tb/compare/{name}", response_class=HTMLResponse)
async def tb_compare_player(name: str, request: Request, user: dict = Depends(require_guild_access)):
    compare = dashboard_data.get_tb_player_compare(user["guild_id"], name)
    if compare is None:
        raise HTTPException(status_code=404, detail=f"Нет сохранённых данных ТБ для игрока «{name}»")
    return templates.TemplateResponse(request, "tb_compare_player.html", {"user": user, "compare": compare})


@router.get("/tb/plan", response_class=HTMLResponse)
async def tb_plan(request: Request, user: dict = Depends(require_guild_access)):
    current = database.get_tb_planet_names(user["guild_id"])
    conflict_label_map = {opt["value"]: opt["label"] for opt in TB_PLAN_CONFLICT_OPTIONS}
    rows = [
        {
            "phase": phase,
            "conflict_key": conflict_key,
            "conflict_label": conflict_label_map.get(conflict_key, conflict_key),
            "planet_name": planet_name,
        }
        for (phase, conflict_key), planet_name in sorted(current.items())
    ]
    return templates.TemplateResponse(request, "tb_plan.html", {
        "user": user,
        "rows": rows,
        "conflict_options": TB_PLAN_CONFLICT_OPTIONS,
        "error": request.query_params.get("error"),
        "saved": request.query_params.get("saved"),
    })


@router.post("/tb/plan", response_class=HTMLResponse)
async def tb_plan_save(
    phase: int = Form(...),
    conflict_key: str = Form(...),
    planet_name: str = Form(...),
    user: dict = Depends(require_guild_access),
):
    planet_name = planet_name.strip()
    valid_conflicts = {opt["value"] for opt in TB_PLAN_CONFLICT_OPTIONS}
    if not planet_name:
        return RedirectResponse(f"/tb/plan?{urlencode({'error': 'Название планеты не может быть пустым'})}", status_code=303)
    if not (1 <= phase <= 6):
        return RedirectResponse(f"/tb/plan?{urlencode({'error': 'Некорректный номер этапа (1-6)'})}", status_code=303)
    if conflict_key not in valid_conflicts:
        return RedirectResponse(f"/tb/plan?{urlencode({'error': 'Некорректная ветка'})}", status_code=303)
    database.set_tb_planet_name(str(phase), conflict_key, planet_name, source="manual", guild_id=user["guild_id"])
    return RedirectResponse("/tb/plan?saved=1", status_code=303)


@router.get("/tb/order-plans", response_class=HTMLResponse)
async def tb_order_plans(request: Request, user: dict = Depends(require_guild_access)):
    guild_id = user["guild_id"]
    guild_cfg = database.get_guild_config(guild_id) or {}
    active_id = int(guild_cfg["tb_active_plan_id"]) if guild_cfg.get("tb_active_plan_id") else None
    discord_guild_id = guild_cfg.get("discord_guild_id")
    rows = []
    for p in database.get_tb_saved_plans(guild_id):
        rows.append({
            **p,
            "active": p["id"] == active_id,
            "thread_link": f"https://discord.com/channels/{discord_guild_id}/{p['thread_id']}" if discord_guild_id else None,
        })
    return templates.TemplateResponse(request, "tb_order_plans.html", {
        "user": user,
        "rows": rows,
        "error": request.query_params.get("error"),
        "saved": request.query_params.get("saved"),
    })


@router.post("/tb/order-plans/select", response_class=HTMLResponse)
async def tb_order_plans_select(plan_id: int = Form(...), user: dict = Depends(require_guild_access)):
    plan = database.get_tb_saved_plan(plan_id)
    if plan is None or plan["guild_id"] != user["guild_id"]:
        return RedirectResponse(f"/tb/order-plans?{urlencode({'error': 'План не найден'})}", status_code=303)
    database.update_guild_config(user["guild_id"], tb_active_plan_id=plan_id)
    return RedirectResponse("/tb/order-plans?saved=1", status_code=303)


@router.post("/tb/order-plans/delete", response_class=HTMLResponse)
async def tb_order_plans_delete(name: str = Form(...), user: dict = Depends(require_guild_access)):
    database.delete_tb_saved_plan(user["guild_id"], name)
    return RedirectResponse("/tb/order-plans?saved=1", status_code=303)


@router.post("/tb/order-plans/save", response_class=HTMLResponse)
async def tb_order_plans_save_manual(
    name: str = Form(...),
    thread_link: str = Form(...),
    stars: int = Form(...),
    user: dict = Depends(require_guild_access),
):
    # Веб-процесс не держит Discord-клиент (см. web/app.py — "без промежуточного
    # API у бота"), поэтому, в отличие от бот-команды /тб_план сохранить, здесь
    # НЕТ проверки, что в ветке реально лежит ордер на все 6 этапов — только
    # синтаксическая валидация ссылки. Шаблон явно предупреждает об этом.
    name = name.strip()
    if not name:
        return RedirectResponse(f"/tb/order-plans?{urlencode({'error': 'Название не может быть пустым'})}", status_code=303)
    thread_id = _extract_thread_id(thread_link)
    if thread_id is None:
        return RedirectResponse(
            f"/tb/order-plans?{urlencode({'error': 'Не удалось распознать ссылку на тред'})}", status_code=303
        )
    database.save_tb_plan(user["guild_id"], name, thread_id, stars, created_by=f"web:{user['discord_id']}")
    return RedirectResponse("/tb/order-plans?saved=1", status_code=303)


# =====================================================================
# КОНСТРУКТОР ВЗВОДОВ ТБ: для планет, которые реально задействованы в выбранном
# сохранённом плане ордера (по умолчанию — активный, guilds.tb_active_plan_id, но
# можно переключиться на любой другой через ?plan_id=), показывает 6 операций,
# подсказки юнитов на каждую (tb_platoon_data.ROTE_PLATOON_SUGGESTIONS, см. память
# project_tb_platoon_unit_lists_rote_2026-08-27), минимальную реликвию по этапу
# (tb_platoon_data.ROTE_MIN_RELIC_BY_ROUND) и назначение конкретного игрока гильдии
# на конкретный донат-слот (database.*_tb_platoon_assignment*, аналог "Assign
# Players" в HotUtils) — кандидаты на слот считаются из живого кэша ростера гильдии
# (database.get_player_unit_owners_bulk + stat_engine.get_current_relic_level).
#
# План хранит только discord thread_id (см. cogs/tb_order_image.py) — самого разбора
# "какая планета на каком этапе" нигде в БД не остаётся, только опубликованные ботом
# сообщения-блоки в этом треде. Разбор (чтение через Discord REST + регексы + перевод
# RU->EN названий планет) вынесен в tb_plan_reader.py 2026-08-29 — тот же разбор
# понадобился tb_platoon_autofill.py, который дёргается и с веб-роута, и из
# cogs/tb_order_image.py (см. план "Автозаполнение взводов ТБ + фильтры"), дублировать
# его в третий раз не стали.
# =====================================================================

_PLANET_NAME_TO_RU = tb_plan_reader.PLANET_NAME_TO_RU
_RU_TO_PLANET_NAME = tb_plan_reader.RU_TO_PLANET_NAME

# Ветка (conflict-код в comlink zoneId, "power_zone_tb3_mixed_phaseNN_conflictNN_...") для
# каждой планеты — дублирует cogs/guild_events.py::TB_PLANET_CONFLICT (тот же принцип
# развязки веб-процесса от cogs/*, что и у _TB_PLAN_HEADER_RE выше), но ключами здесь взяты
# английские названия из tb_platoon_data.py (а не русские из офицерских анонсов) — сразу
# нужный вид для сборки zoneId в EchoBase/HotUtils-экспорте взводов (см. tb_platoons_export
# ниже). Код фиксирован per-планета (не меняется от этапа к этапу) — подтверждено сверкой
# сгенерированного zoneId с реальным экспортом HotUtils: 15 обычных планет — по файлу с
# этапа 1 (Mustafar=conflict01, набор юнитов совпал с ROTE_PLATOON_SUGGESTIONS[("Mustafar",1)]
# как мультимножество); Zeffo/Mandalore — по файлу-примеру с бонус-зонами (этап "4/M4/Z4",
# 2026-08-28): их unitBaseId по каждому platoonDefinitionId сверены с ROTE_PLATOON_SUGGESTIONS
# для обеих бонус-планет (Zeffo: conflict01_bonus, 47/72 юнитов совпало против 5/72 у
# Mandalore на той же зоне; Mandalore: conflict03_bonus, 48/71 против 4/71 у Zeffo).
# Death Star/Hoth/Scarif (появляются только на этапе 6, до которого гильдия ещё не доходила,
# поэтому их вообще нет в TB_PLANET_CONFLICT) — сняты 2026-08-28 напрямую со страницы
# https://echobase.app/platoonAssigner/tb/RiseOfTheEmpire/phase/6 для гильдии AbsoluteChaos
# (guildId=718294) через DOM (table[cgid] на каждую зону несёт готовый zoneId), составы
# донат-слотов дополнительно сверены с ROTE_PLATOON_SUGGESTIONS — совпадение сильное для всех
# трёх. ВАЖНО: цифра conflict-кода здесь НЕ соответствует "интуитивной" тёмная/смешанная/
# светлая теме планеты в порядке 01/02/03 — Scarif (светлая по геймплею) оказался на
# conflict01, Death Star (тёмная) — на conflict02, Hoth (смешанная) — на conflict03; не
# пытаться выводить conflict-код новых планет из темы юнитов, только снимать живьём.
_PLANET_CONFLICT_CODE = {
    "Mustafar": "01", "Geonosis": "01", "Dathomir": "01", "Haven-class Medical Station": "01", "Malachor": "01",
    "Scarif": "01",
    "Corellia": "02", "Felucia": "02", "Tatooine": "02", "Kessel": "02", "Vandor": "02",
    "Death Star": "02",
    "Coruscant": "03", "Bracca": "03", "Kashyyyk": "03", "Lothal": "03", "Ring of Kafrene": "03",
    "Hoth": "03",
    "Zeffo": "01", "Mandalore": "03",
}
_BONUS_PLANETS = {"Zeffo", "Mandalore"}

# Неподтверждённая деталь, НЕ проблема на практике (см. пояснение пользователя 2026-08-28:
# планету можно зачищать и расставлять на ней взводы практически на любом этапе): в файле-
# примере "4/M4/Z4" бонус-зона Зеффо стояла на phase03, хотя весь файл был снят на этапе 4 (три
# обычные зоны и Мандалор — на phase04). Наша сборка всё равно берёт цифру фазы как round_num
# (номер анонсированного этапа) — точно верно для первого появления планеты на этапе (проверено
# на этапе 1); расхождение выше не мешает реальному использованию, оставлено как техническая
# заметка на случай, если импорт в HotUtils когда-нибудь всё же на этом споткнётся.


def _resolve_viewed_plan(guild_id: int, plan_id_param: str | None) -> tuple[dict | None, str | None]:
    """Какой сохранённый план показывать в конструкторе взводов: явный ?plan_id= (любой
    план гильдии, не обязательно активный — переключение в конструкторе только просмотровое,
    не трогает guilds.tb_active_plan_id), либо, если параметра нет, активный план как раньше."""
    if plan_id_param:
        try:
            plan_id = int(plan_id_param)
        except ValueError:
            return None, "Некорректный ID плана."
        plan = database.get_tb_saved_plan(plan_id)
        if not plan or plan["guild_id"] != guild_id:
            return None, "План не найден."
        return plan, None

    guild_cfg = database.get_guild_config(guild_id) or {}
    active_id = guild_cfg.get("tb_active_plan_id")
    if not active_id:
        return None, "Нет активного плана ордера — выберите его на странице «Планы ордера»."
    plan = database.get_tb_saved_plan(int(active_id))
    if not plan or plan["guild_id"] != guild_id:
        return None, "Активный план не найден — выберите его заново на странице «Планы ордера»."
    return plan, None


# Тонкая обёртка вокруг tb_plan_reader.fetch_plan_planets — сохраняет прежнее локальное
# имя _fetch_plan_planets(plan), чтобы не трогать все вызовы ниже по файлу.
_fetch_plan_planets = tb_plan_reader.fetch_plan_planets


# Потолок на число кандидатов, показываемых под одним слотом — без него страница
# распухала до десятков тысяч <form> разом (частый юнит донатят треть гильдии), из-за
# чего переключение этапа/раскрытие слота ощутимо тормозило (жалоба пользователя
# 2026-08-28). Топ-N по релику — снизу всё равно никто не выбирает донора №25.
PLATOON_CANDIDATES_LIMIT = 20


@router.get("/tb/platoons", response_class=HTMLResponse)
async def tb_platoons(request: Request, user: dict = Depends(require_guild_access)):
    guild_id = user["guild_id"]
    plan, error = _resolve_viewed_plan(guild_id, request.query_params.get("plan_id"))

    entries: list[dict] = []
    if plan is not None:
        entries, error = await _fetch_plan_planets(plan)

    # Группируем по ЭТАПУ (round), не по планете — по прямому запросу пользователя
    # 2026-08-28 (страница раньше листала одну планету за раз через выпадашку, из-за
    # чего было не видно все планеты конкретного этапа сразу). Один этап реально
    # содержит 2-4 планеты одновременно (тёмная/смешанная/светлая ветка + иногда ещё и
    # бонус-зона Зеффо/Мандалор — см. TB_PLANET_CONFLICT в cogs/guild_events.py), и
    # именно это нужно видеть разом, а не листать планета-за-планетой. Планета,
    # стоящая на нескольких этапах подряд (гильдия не выбрала все звёзды за этап),
    # теперь просто попадает в блок каждого из этих этапов отдельно — дублирование
    # разумно: на разных этапах это всё ещё "то же самое, что нужно фармить сейчас".
    by_round: dict[int, list[dict]] = {}
    seen_in_round = set()
    for e in entries:
        key = (e["round"], e["planet"] or f"raw:{e['raw']}")
        if key in seen_in_round:
            continue
        seen_in_round.add(key)
        by_round.setdefault(e["round"], []).append(e)

    # Выбор этапа теперь СЕРВЕРНЫЙ (?round=), не клиентский тумблер видимости — на всех
    # 6 этапах разом кандидаты на слоты давали десятки тысяч элементов в DOM разом
    # (жалоба "очень долго меняется этап" 2026-08-28); считаем и рендерим только один
    # выбранный этап за раз, остальные — просто пункты в выпадашке ниже.
    round_numbers = sorted(by_round)
    try:
        selected_round_num = int(request.query_params.get("round", ""))
    except ValueError:
        selected_round_num = None
    if selected_round_num not in round_numbers:
        selected_round_num = round_numbers[0] if round_numbers else None

    round_options = []
    for n in round_numbers:
        names = [
            ("⚠ " if e["planet"] is None else "") + (e["planet"] or e["raw"])
            for e in by_round[n]
        ]
        round_options.append({"number": n, "label": f"Этап {n} — " + ", ".join(names)})

    selected_entries = by_round.get(selected_round_num, []) if selected_round_num is not None else []
    min_relic = tb_platoon_data.ROTE_MIN_RELIC_BY_ROUND.get(selected_round_num) if selected_round_num else None
    min_relic_label = f"R{min_relic}+" if min_relic is not None else None

    # Уникальные названия юнитов ТОЛЬКО выбранного этапа -> base_id -> живые владельцы в
    # гильдии (один батч-запрос вместо N на каждый слот, см. коммент над
    # get_player_unit_owners_bulk в database.py).
    unit_names_this_round = {
        name
        for e in selected_entries
        if e["planet"]
        for operation in range(1, 7)
        for name in (tb_platoon_data.ROTE_PLATOON_SUGGESTIONS.get((e["planet"], operation)) or [])
    }
    name_to_base_id = database.resolve_unit_display_names(list(unit_names_this_round)) if unit_names_this_round else {}
    mappings = database.get_all_user_mappings(guild_id)
    ally_codes = [ally_code for _discord_id, ally_code, _name in mappings]
    player_name_by_ally = {ally_code: name for _discord_id, ally_code, name in mappings}
    base_ids = sorted({bid for bid in name_to_base_id.values() if bid})
    # Корабли не имеют реликвии (см. tb_platoon_engine.SHIP_MIN_STARS) — донат-требование
    # для них 7★, не порог реликвии этапа.
    unit_types = database.get_unit_types(base_ids) if base_ids else {}
    owners_raw = database.get_player_unit_owners_bulk(ally_codes, base_ids) if base_ids else []
    owners_by_base_id: dict[str, list[dict]] = {}
    for row in owners_raw:
        owners_by_base_id.setdefault(row["base_id"], []).append({
            "ally_code": row["ally_code"],
            "name": player_name_by_ally.get(row["ally_code"], row["ally_code"]),
            "relic": stat_engine.get_current_relic_level(row["unit"]),
            "stars": row["unit"].get("currentRarity", 0),
        })
    for base_id, owners in owners_by_base_id.items():
        is_ship = unit_types.get(base_id) == "ship"
        owners.sort(key=lambda o: -(o["stars"] if is_ship else o["relic"]))
        del owners[PLATOON_CANDIDATES_LIMIT:]

    assignments = database.get_tb_platoon_assignments(guild_id, plan["id"]) if plan else {}

    # Фильтры автозаполнения (tb_platoon_filters.py) применяются и здесь, к ручной
    # расстановке — единая точка правды, см. tb_platoon_engine.py. Ошибки разбора текста
    # молча игнорируются на этой странице (страница /tb/platoons/filters не даёт сохранить
    # текст с ошибками — значит то, что лежит в БД, уже валидно; пустой текст парсится в
    # пустой ParsedRules без ошибок).
    filter_rules, _filter_errors = tb_platoon_filters.parse_rules(
        database.get_tb_platoon_filter_rules(guild_id), guild_id,
    )

    # В рамках ОДНОГО этапа игрок не может задонатить одного и того же юнита дважды —
    # прямой запрос пользователя 2026-08-28 ("если 0-0-0 стоит на Мустафаре на 1 этапе
    # на 3 операции, второй раз его на 1 этапе поставить нельзя"). Считаем по всем
    # назначениям ПЛАНЕТ, показанных на выбранном этапе, разом: (ally_code, base_id) ->
    # где уже стоит. Планета, растянутая на 2+ этапа (не зачищена целиком), теперь всегда
    # входит в selected_entries того этапа, на котором её сейчас смотрят — её назначения
    # (уже не привязанные к round_num, см. database.py::_ensure_tb_platoon_assignments_table)
    # подхватываются здесь так же, как назначения любой другой планеты этого этапа.
    planets_this_round = {e["planet"] for e in selected_entries if e["planet"]}
    used_pairs_this_round = tb_platoon_engine.compute_used_pairs(assignments, planets_this_round, name_to_base_id, selected_round_num)

    # Лимит "не больше 10 юнитов на планету от игрока" — по этапу ПРОСМОТРА (round_num
    # первого назначения слота, не по планете целиком), см. tb_platoon_engine.py.
    round_counts = tb_platoon_engine.compute_round_counts(assignments, selected_round_num) if selected_round_num else {}

    hold_flags = database.get_tb_platoon_holds(guild_id, plan["id"]) if plan else {}

    # Автоматическое "держим": планета, которая ещё встречается на более позднем этапе
    # этого же плана, ещё не зачищена целиком — по прямому запросу пользователя 2026-08-29
    # ("на 3 этапе есть Датомир и на 4 этапе есть Датомир — значит на 3 этапе взводы не
    # нужно заполнять полностью"), без ручного тумблера. Тот же расчёт использует
    # tb_platoon_autofill.py — здесь он только для бейджа "держим" на странице, сама
    # логика неполного заполнения — в автозаполнении.
    planet_last_round: dict[str, int] = {}
    for rn, es in by_round.items():
        for e2 in es:
            if e2["planet"]:
                planet_last_round[e2["planet"]] = max(planet_last_round.get(e2["planet"], rn), rn)

    # Сводка "сколько юнитов не можем поставить на этапе" — незаполненные слоты, у которых
    # прямо сейчас НЕТ ни одного подходящего кандидата (никто не проходит по релику/★,
    # либо исключён фильтром/лимитом/занят в другом месте этого этапа) — не просто "ещё не
    # назначено вручную". Считается по всем планетам выбранного этапа разом.
    unplaceable_count = 0

    planet_blocks = []
    for planet_idx, e in enumerate(selected_entries):
        operations = []
        for operation in range(1, 7):
            unit_names = tb_platoon_data.ROTE_PLATOON_SUGGESTIONS.get((e["planet"], operation)) if e["planet"] else None
            slots = []
            for slot_index, unit_name in enumerate(unit_names or []):
                base_id = name_to_base_id.get(unit_name)
                owners = owners_by_base_id.get(base_id, []) if base_id else []
                assignment = tb_platoon_engine.visible_assignment(
                    assignments.get((e["planet"], operation, slot_index)), selected_round_num,
                )
                here = (e["planet"], operation, slot_index)
                slot_owners = tb_platoon_engine.slot_candidates(
                    owners=owners, base_id=base_id, here=here, used_pairs=used_pairs_this_round,
                    min_relic=min_relic, round_num=selected_round_num, planet=e["planet"],
                    filter_rules=filter_rules, round_counts=round_counts,
                    is_ship=unit_types.get(base_id) == "ship",
                )
                if base_id and filter_rules.is_unit_excluded(base_id):
                    for o in slot_owners:
                        o["excluded_by_filter"] = True
                if not assignment and not any(tb_platoon_engine.is_eligible(o) for o in slot_owners):
                    unplaceable_count += 1
                slots.append({
                    "index": slot_index,
                    "unit": unit_name,
                    "owners": slot_owners,
                    "unit_excluded": bool(base_id and filter_rules.is_unit_excluded(base_id)),
                    "assigned_name": player_name_by_ally.get(assignment["ally_code"], assignment["ally_code"]) if assignment else None,
                    "assigned_ally_code": assignment["ally_code"] if assignment else None,
                    "anchor": f"slot-{selected_round_num}-{planet_idx}-{operation}-{slot_index}",
                })
            operations.append({
                "number": operation,
                "slots": slots,
                "filled": sum(1 for s in slots if s["assigned_ally_code"]),
                "total": len(slots),
            })
        auto_held = bool(e["planet"] and planet_last_round.get(e["planet"], selected_round_num) > selected_round_num)
        planet_blocks.append({
            "name": e["planet"] or e["raw"],
            "unresolved": e["planet"] is None,
            "operations": operations,
            "held": auto_held or bool(e["planet"] and hold_flags.get((selected_round_num, e["planet"]))),
            "auto_held": auto_held,
        })

    saved_plans = database.get_tb_saved_plans(guild_id)
    guild_cfg = database.get_guild_config(guild_id) or {}
    active_plan_id = int(guild_cfg["tb_active_plan_id"]) if guild_cfg.get("tb_active_plan_id") else None

    return templates.TemplateResponse(request, "tb_platoons.html", {
        "user": user,
        "round_options": round_options,
        "selected_round_num": selected_round_num,
        "planets": planet_blocks,
        "min_relic_label": min_relic_label,
        "error": error,
        "plan": plan,
        "saved_plans": saved_plans,
        "active_plan_id": active_plan_id,
        "autofill_summary": request.query_params.get("autofill_summary"),
        "unplaceable_count": unplaceable_count,
    })


@router.post("/tb/platoons/assign", response_class=HTMLResponse)
async def tb_platoons_assign(
    plan_id: int = Form(...),
    round_num: int = Form(...),
    planet: str = Form(...),
    operation: int = Form(...),
    slot_index: int = Form(...),
    ally_code: str = Form(...),
    anchor: str = Form(""),
    user: dict = Depends(require_guild_access),
):
    guild_id = user["guild_id"]
    plan = database.get_tb_saved_plan(plan_id)
    if not plan or plan["guild_id"] != guild_id:
        return RedirectResponse(f"/tb/platoons?{urlencode({'error': 'План не найден'})}", status_code=303)
    database.set_tb_platoon_assignment(
        guild_id, plan_id, round_num, planet, operation, slot_index, ally_code.strip(),
        assigned_by=f"web:{user['discord_id']}",
    )
    suffix = f"#{anchor}" if anchor else ""
    return RedirectResponse(f"/tb/platoons?plan_id={plan_id}&round={round_num}{suffix}", status_code=303)


@router.post("/tb/platoons/unassign", response_class=HTMLResponse)
async def tb_platoons_unassign(
    plan_id: int = Form(...),
    round_num: int = Form(...),
    planet: str = Form(...),
    operation: int = Form(...),
    slot_index: int = Form(...),
    anchor: str = Form(""),
    user: dict = Depends(require_guild_access),
):
    guild_id = user["guild_id"]
    plan = database.get_tb_saved_plan(plan_id)
    if not plan or plan["guild_id"] != guild_id:
        return RedirectResponse(f"/tb/platoons?{urlencode({'error': 'План не найден'})}", status_code=303)
    database.clear_tb_platoon_assignment(guild_id, plan_id, planet, operation, slot_index)
    suffix = f"#{anchor}" if anchor else ""
    return RedirectResponse(f"/tb/platoons?plan_id={plan_id}&round={round_num}{suffix}", status_code=303)


_AUTOFILL_REASON_LABELS = {
    "no_owner": "нет владельцев в гильдии",
    "no_eligible_owner": "нет подходящих доноров",
    "unit_excluded": "юнит исключён фильтром",
    "unit_not_resolved": "юнит не распознан",
}


@router.post("/tb/platoons/autofill", response_class=HTMLResponse)
async def tb_platoons_autofill_route(
    plan_id: int = Form(...),
    round_num: int = Form(...),
    user: dict = Depends(require_guild_access),
):
    """Заполняет все ещё пустые слоты ВСЕГО плана (все распознанные этапы сразу, не
    только открытый) — tb_platoon_autofill.py. round_num здесь — только чтобы вернуть
    пользователя на тот же этап после редиректа, на сам алгоритм не влияет."""
    guild_id = user["guild_id"]
    plan = database.get_tb_saved_plan(plan_id)
    if not plan or plan["guild_id"] != guild_id:
        return RedirectResponse(f"/tb/platoons?{urlencode({'error': 'План не найден'})}", status_code=303)

    try:
        result = await tb_platoon_autofill.autofill_plan(guild_id, plan_id)
    except (ValueError, RuntimeError) as e:
        return RedirectResponse(
            f"/tb/platoons?plan_id={plan_id}&round={round_num}&{urlencode({'error': str(e)})}", status_code=303,
        )

    reasons: dict[str, int] = {}
    for o in result.unfilled:
        if o.reason == "held_back":
            continue
        reasons[o.reason] = reasons.get(o.reason, 0) + 1

    summary = f"Автозаполнение: {result.filled_slots}/{result.total_slots} слотов занято"
    if result.held_back:
        summary += f", {result.held_back} намеренно не добито («держим»)"
    if reasons:
        parts = [f"{_AUTOFILL_REASON_LABELS.get(reason, reason)}: {count}" for reason, count in reasons.items()]
        summary += " — не хватает (" + ", ".join(parts) + ")"

    suffix = urlencode({"autofill_summary": summary})
    return RedirectResponse(f"/tb/platoons?plan_id={plan_id}&round={round_num}&{suffix}", status_code=303)


@router.post("/tb/platoons/hold", response_class=HTMLResponse)
async def tb_platoons_hold_route(
    plan_id: int = Form(...),
    round_num: int = Form(...),
    planet: str = Form(...),
    held: str = Form(...),
    user: dict = Depends(require_guild_access),
):
    guild_id = user["guild_id"]
    plan = database.get_tb_saved_plan(plan_id)
    if not plan or plan["guild_id"] != guild_id:
        return RedirectResponse(f"/tb/platoons?{urlencode({'error': 'План не найден'})}", status_code=303)
    database.set_tb_platoon_hold(guild_id, plan_id, round_num, planet, held == "1", set_by=f"web:{user['discord_id']}")
    return RedirectResponse(f"/tb/platoons?plan_id={plan_id}&round={round_num}", status_code=303)


@router.post("/tb/platoons/clear", response_class=HTMLResponse)
async def tb_platoons_clear_route(
    plan_id: int = Form(...),
    round_num: int = Form(...),
    scope: str = Form(...),  # "operation" / "planet" / "round" / "plan"
    planet: str = Form(""),
    operation: int = Form(0),
    user: dict = Depends(require_guild_access),
):
    """Кнопки «очистить» на /tb/platoons — по операции/планете/этапу/всему плану. "Этап"
    чистит все планеты, показанные на этом этапе (включая перенесённые с прошлого этапа —
    они целиком общие с ним, см. database.py::_ensure_tb_platoon_assignments_table), не
    только те, что впервые появились именно тут."""
    guild_id = user["guild_id"]
    plan = database.get_tb_saved_plan(plan_id)
    if not plan or plan["guild_id"] != guild_id:
        return RedirectResponse(f"/tb/platoons?{urlencode({'error': 'План не найден'})}", status_code=303)

    if scope == "operation" and planet and operation:
        database.clear_tb_platoon_assignments_for_operation(guild_id, plan_id, planet, operation)
    elif scope == "planet" and planet:
        database.clear_tb_platoon_assignments_for_planet(guild_id, plan_id, planet)
    elif scope == "round":
        entries, fetch_error = await tb_plan_reader.fetch_plan_planets(plan)
        if not fetch_error:
            for p in {e["planet"] for e in entries if e["planet"] and e["round"] == round_num}:
                database.clear_tb_platoon_assignments_for_planet(guild_id, plan_id, p)
    elif scope == "plan":
        database.clear_tb_platoon_assignments_for_plan(guild_id, plan_id)

    return RedirectResponse(f"/tb/platoons?plan_id={plan_id}&round={round_num}", status_code=303)


@router.get("/tb/platoons/filters", response_class=HTMLResponse)
async def tb_platoons_filters_page(request: Request, user: dict = Depends(require_guild_access)):
    guild_id = user["guild_id"]
    rules_text = database.get_tb_platoon_filter_rules(guild_id)
    parsed, errors = tb_platoon_filters.parse_rules(rules_text, guild_id)
    return templates.TemplateResponse(request, "tb_platoon_filters.html", {
        "user": user,
        "rules_text": rules_text,
        "described": tb_platoon_filters.describe_rules(parsed) if not errors else [],
        "errors": errors,
        "saved": False,
    })


@router.post("/tb/platoons/filters", response_class=HTMLResponse)
async def tb_platoons_filters_save(
    request: Request,
    rules_text: str = Form(""),
    user: dict = Depends(require_guild_access),
):
    guild_id = user["guild_id"]
    parsed, errors = tb_platoon_filters.parse_rules(rules_text, guild_id)
    if not errors:
        database.set_tb_platoon_filter_rules(guild_id, rules_text, updated_by=f"web:{user['discord_id']}")
    return templates.TemplateResponse(request, "tb_platoon_filters.html", {
        "user": user,
        "rules_text": rules_text,
        "described": tb_platoon_filters.describe_rules(parsed) if not errors else [],
        "errors": errors,
        "saved": not errors,
    })


# =====================================================================
# Экспорт расставленных взводов в формат HotUtils/EchoBase Platoon Assigner — по прямому
# запросу пользователя 2026-08-28, снявшего два живых примера импорта с рабочего стола:
# echobase-assignments-ROTE-P1_1_1-<ts>.json (этап 1, только 3 обычные зоны) и
# echobase-assignments-ROTE-P4_M4_Z4-<ts>.json (этап 4, + бонус-зоны Зеффо/Мандалора) —
# и попросившего собирать такой же файл из назначений /tb/platoons, чтобы завозить их
# обратно в HotUtils. Схема файла: {"phase": "N/N/N", "timestamp": ISO8601,
# "platoonAssignments": [{"allyCode", "unitBaseId", "zoneId", "platoonDefinitionId"}, ...]}.
# zoneId = "tb3_mixed_phase{round:02d}_conflict{code}[_bonus]_recon01", code — из
# _PLANET_CONFLICT_CODE, "_bonus" — только для Zeffo/Mandalore (см. _BONUS_PLANETS и
# комментарий у _PLANET_CONFLICT_CODE про то, как это подтверждено). platoonDefinitionId =
# "tb3-platoon-{operation}" — подтверждено тем же сравнением (6 операций конструктора = 6
# tb3-platoon-N в обоих примерах). Поле "phase" в самом файле по-прежнему НЕ проверено на
# реальном импорте: во втором примере оно "4/M4/Z4" — не просто "N/N/N", вероятно
# человекочитаемый ярлык самого сохранённого шаблона HotUtils, а не вычисляемое из данных
# значение; жёстко утверждать нельзя. У цифры фазы В zoneId — своя отдельная оговорка про
# перенос планеты с этапа на этап, см. комментарий у _PLANET_CONFLICT_CODE выше.
def _build_round_platoon_assignments(
    round_num: int, round_entries: list[dict], assignments: dict,
) -> tuple[list[dict], list[str]]:
    """round_entries — записи _fetch_plan_planets для одного этапа. Возвращает
    (platoonAssignments-список, имена пропущенных планет) — планета пропускается, только
    если её название вообще не распознано (raw-заметка officer-анонса, не нашлось в
    _PLANET_NAME_TO_RU)."""
    unit_names = {
        name
        for e in round_entries
        if e["planet"]
        for operation in range(1, 7)
        for name in (tb_platoon_data.ROTE_PLATOON_SUGGESTIONS.get((e["planet"], operation)) or [])
    }
    name_to_base_id = database.resolve_unit_display_names(list(unit_names)) if unit_names else {}

    result = []
    skipped = []
    for e in round_entries:
        planet = e["planet"]
        conflict_code = _PLANET_CONFLICT_CODE.get(planet) if planet else None
        if not conflict_code:
            skipped.append(planet or e["raw"])
            continue
        bonus_suffix = "_bonus" if planet in _BONUS_PLANETS else ""
        zone_id = f"tb3_mixed_phase{round_num:02d}_conflict{conflict_code}{bonus_suffix}_recon01"
        for operation in range(1, 7):
            unit_list = tb_platoon_data.ROTE_PLATOON_SUGGESTIONS.get((planet, operation)) or []
            for slot_index, unit_name in enumerate(unit_list):
                # round-aware: донат, отложенный автозаполнением на более поздний этап
                # многоэтапной планеты, не попадает в файл ЭТОГО этапа — он ещё "не сделан
                # по времени" (см. tb_platoon_engine.visible_assignment).
                assignment = tb_platoon_engine.visible_assignment(
                    assignments.get((planet, operation, slot_index)), round_num,
                )
                if not assignment:
                    continue
                base_id = name_to_base_id.get(unit_name)
                if not base_id:
                    continue
                result.append({
                    "allyCode": assignment["ally_code"],
                    "unitBaseId": base_id,
                    "zoneId": zone_id,
                    "platoonDefinitionId": f"tb3-platoon-{operation}",
                })
    return result, skipped


def _echobase_export_bytes(round_num: int, platoon_assignments: list[dict]) -> tuple[bytes, str]:
    """Сериализует один этап в JSON того же вида, что и живой пример HotUtils/EchoBase
    (минифицированный, без пробелов — как в самом примере) + имя файла с тем же паттерном
    (echobase-assignments-ROTE-P{r}_{r}_{r}-{timestamp с ':' -> '_'}.json)."""
    now = datetime.utcnow()
    timestamp = now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"
    payload = {
        "phase": f"{round_num}/{round_num}/{round_num}",
        "timestamp": timestamp,
        "platoonAssignments": platoon_assignments,
    }
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    filename = f"echobase-assignments-ROTE-P{round_num}_{round_num}_{round_num}-{timestamp.replace(':', '_')}.json"
    return body, filename


def _attachment_headers(filename: str) -> dict:
    ascii_fallback = filename.encode("ascii", "ignore").decode("ascii") or "download"
    return {"Content-Disposition": f"attachment; filename=\"{ascii_fallback}\"; filename*=UTF-8''{quote(filename)}"}


@router.get("/tb/platoons/export")
async def tb_platoons_export(request: Request, user: dict = Depends(require_guild_access)):
    guild_id = user["guild_id"]
    plan, error = _resolve_viewed_plan(guild_id, request.query_params.get("plan_id"))
    if plan is None:
        raise HTTPException(404, error or "План не найден.")
    try:
        round_num = int(request.query_params.get("round", ""))
    except ValueError:
        raise HTTPException(400, "Некорректный номер этапа.")

    entries, fetch_error = await _fetch_plan_planets(plan)
    if fetch_error:
        raise HTTPException(400, fetch_error)
    round_entries = [e for e in entries if e["round"] == round_num]
    if not round_entries:
        raise HTTPException(404, f"На этапе {round_num} нет планет.")

    assignments = database.get_tb_platoon_assignments(guild_id, plan["id"])
    platoon_assignments, _skipped = _build_round_platoon_assignments(round_num, round_entries, assignments)
    body, filename = _echobase_export_bytes(round_num, platoon_assignments)
    return Response(content=body, media_type="application/json", headers=_attachment_headers(filename))


@router.get("/tb/platoons/export/all")
async def tb_platoons_export_all(request: Request, user: dict = Depends(require_guild_access)):
    guild_id = user["guild_id"]
    plan, error = _resolve_viewed_plan(guild_id, request.query_params.get("plan_id"))
    if plan is None:
        raise HTTPException(404, error or "План не найден.")

    entries, fetch_error = await _fetch_plan_planets(plan)
    if fetch_error:
        raise HTTPException(400, fetch_error)

    by_round: dict[int, list[dict]] = {}
    seen = set()
    for e in entries:
        key = (e["round"], e["planet"] or f"raw:{e['raw']}")
        if key in seen:
            continue
        seen.add(key)
        by_round.setdefault(e["round"], []).append(e)
    if not by_round:
        raise HTTPException(404, "В плане не нашлось ни одного этапа.")

    assignments = database.get_tb_platoon_assignments(guild_id, plan["id"])

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        for round_num in sorted(by_round):
            platoon_assignments, _skipped = _build_round_platoon_assignments(round_num, by_round[round_num], assignments)
            body, filename = _echobase_export_bytes(round_num, platoon_assignments)
            zf.writestr(filename, body)

    safe_plan_name = re.sub(r'[\\/:*?"<>|]+', "_", plan["name"]).strip() or "plan"
    zip_name = f"echobase-assignments-ROTE-{safe_plan_name}.zip"
    return Response(content=buffer.getvalue(), media_type="application/zip", headers=_attachment_headers(zip_name))


# Пагинация "1 страница = 1 календарный день" (а не фиксированное число строк) — так
# страница 1 всегда за сегодня, страница 2 — за вчера и т.д., как просил пользователь.
# ACTIVITY_DAY_ROW_LIMIT — просто защитный потолок на случай аномально активного дня
# (массовый первый синк и т.п.), а не обычный постраничный лимит.
ACTIVITY_DAY_ROW_LIMIT = 2000
ACTIVITY_PERIOD_DAYS = {"7": 7, "30": 30, "90": 90}  # пресеты вместо ручного выбора дат — см. обсуждение в гайд-канале 2026-08-25


def _period_to_date_from(period: str | None) -> str | None:
    days = ACTIVITY_PERIOD_DAYS.get(period or "")
    if not days:
        return None
    return (datetime.now(MSK).date() - timedelta(days=days)).isoformat()


@router.get("/activity", response_class=HTMLResponse)
async def activity(request: Request, user: dict = Depends(require_guild_access)):
    guild_id = user["guild_id"]
    player_filter = request.query_params.get("player") or None
    action_type_filter = request.query_params.get("action_type") or None
    period = request.query_params.get("period") or ""
    if period not in ACTIVITY_PERIOD_DAYS:
        period = ""
    date_from = _period_to_date_from(period)

    # "Событий по фильтру/всего" на плашке наверху — суммарно по всему фильтру, не по одному
    # дню, иначе цифра скакала бы при перелистывании страниц.
    total_count = dashboard_data.get_guild_activity_count(
        guild_id, ally_code=player_filter, action_type=action_type_filter, date_from=date_from,
    )

    # Список дат, за которые вообще есть события по фильтру (без учёта страницы) — сама
    # пагинация теперь идёт по этому списку, а не по offset/limit строк: страница N
    # показывает все события за N-й по свежести день, а не N-ю полусотню строк.
    activity_dates = dashboard_data.get_guild_activity_dates(
        guild_id, ally_code=player_filter, action_type=action_type_filter, date_from=date_from,
    )
    total_pages = max(1, len(activity_dates))
    try:
        page = int(request.query_params.get("page", "1"))
    except ValueError:
        page = 1
    page = min(max(page, 1), total_pages)
    selected_date = activity_dates[page - 1] if activity_dates else None

    rows = dashboard_data.get_guild_activity(
        guild_id, ally_code=player_filter, action_type=action_type_filter,
        limit=ACTIVITY_DAY_ROW_LIMIT, date_from=selected_date, date_to=selected_date,
    ) if selected_date else []
    players = dashboard_data.get_guild_activity_players(guild_id)
    grouped = dashboard_data.group_activity(rows)
    sync_status = dashboard_data.get_activity_sync_status(guild_id)
    sync_status_text = _sync_status_text(sync_status)

    # Панель "по типу изменения" — по всему фильтру (игрок/период), не по одной странице,
    # иначе бары скакали бы при перелистывании и не отражали реальную картину.
    breakdown_rows = dashboard_data.get_guild_activity_breakdown(guild_id, ally_code=player_filter, date_from=date_from)
    max_breakdown = breakdown_rows[0][1] if breakdown_rows else 0

    base_params = {k: v for k, v in {"player": player_filter, "action_type": action_type_filter, "period": period}.items() if v}

    return templates.TemplateResponse(request, "activity.html", {
        "user": user,
        "rows": rows,
        "grouped": grouped,
        "players": players,
        "player_filter": player_filter,
        "action_type_filter": action_type_filter,
        "action_types": dashboard_data.ACTIVITY_ACTION_LABELS.items(),
        "period": period,
        "breakdown_rows": breakdown_rows,
        "max_breakdown": max_breakdown,
        "sync_status": sync_status_text,
        "synced_now": request.query_params.get("synced"),
        "total_count": total_count,
        "page": page,
        "total_pages": total_pages,
        "page_date_label": dashboard_data.friendly_activity_date_label(selected_date),
        "prev_page_url": f"/activity?{urlencode({**base_params, 'page': page - 1})}" if page > 1 else None,
        "next_page_url": f"/activity?{urlencode({**base_params, 'page': page + 1})}" if page < total_pages else None,
        "reset_url": "/activity",
    })


@router.post("/activity/sync", response_class=HTMLResponse)
async def activity_sync(
    request: Request,
    player: str = Form(""),
    action_type: str = Form(""),
    period: str = Form(""),
    user: dict = Depends(require_guild_access),
):
    # Ограниченная параллельность (semaphore) + таймаут внутри sync_player — тот же паттерн,
    # что и в web/routes/datacrons.py::_build_guild_report, чтобы HTTP-запрос от кнопки
    # "Обновить сейчас" не висел последовательно по 15с на каждого из ~50 игроков гильдии.
    guild_id = user["guild_id"]
    ally_codes = [code for _, code, _ in database.get_all_user_mappings(guild_id)]
    comlink = _get_comlink()
    today = datetime.now(MSK).date().isoformat()
    semaphore = asyncio.Semaphore(6)
    skill_tier_map = database.get_all_skill_tier_thresholds()

    async def sync_one(ally_code):
        async with semaphore:
            try:
                # guild_ids не {guild_id} — игрок теоретически состоит сразу в нескольких
                # зарегистрированных гильдиях; player_unit_cache общий на всех, и если
                # обновить его тут только под текущую гильдию, автоцикл бота позже не
                # найдёт разницы и "чужая" гильдия потеряет эти события безвозвратно.
                guild_ids = database.get_guild_ids_for_ally_code(ally_code) or {guild_id}
                # Объявления в Discord из omicron_hits тут не постим — у веб-процесса нет
                # Discord-клиента, см. services/activity_diff.py::sync_player.
                _, added, _ = await activity_diff.sync_player(comlink, ally_code, guild_ids, today, skill_tier_map)
                return added
            except Exception as e:
                print(f"⚠️ [/activity/sync] Не удалось обновить ростер {ally_code}: {e}")
                return 0

    results = await asyncio.gather(*(sync_one(ac) for ac in ally_codes))
    params = {"synced": sum(results)}
    if period in ACTIVITY_PERIOD_DAYS:
        params["period"] = period
    if action_type:
        params["action_type"] = action_type
    if player:
        params["player"] = player
    return RedirectResponse(f"/activity?{urlencode(params)}", status_code=303)


@router.get("/violations", response_class=HTMLResponse)
async def violations(request: Request, user: dict = Depends(require_guild_access)):
    show_all = request.query_params.get("all") == "1"
    rows = dashboard_data.get_violations_overview(user["guild_id"], include_zero=show_all)
    top_offenders = [r for r in rows if r.recent_total > 0][:8]
    max_recent = top_offenders[0].recent_total if top_offenders else 0
    return templates.TemplateResponse(request, "violations.html", {
        "user": user,
        "rows": rows,
        "show_all": show_all,
        "n_limit": dashboard_data.N_LIMIT,
        "top_offenders": top_offenders,
        "max_recent": max_recent,
        "warns_structure": WARNS_STRUCTURE,
        "error": request.query_params.get("error"),
    })


@router.get("/violations/api/players", response_class=JSONResponse)
async def violations_players_search(q: str = "", user: dict = Depends(require_guild_access)):
    if not q or len(q.strip()) < 2:
        return []
    q_lower = q.strip().lower()
    mappings = database.get_all_user_mappings(user["guild_id"])
    matches = [
        {"ally_code": ally_code, "name": name}
        for _discord_id, ally_code, name in mappings
        if name and q_lower in name.lower()
    ]
    matches.sort(key=lambda m: m["name"].lower())
    return matches[:20]


@router.post("/violations/add", response_class=HTMLResponse)
async def violation_add(
    ally_code: str = Form(...),
    category: str = Form(...),
    violation_1: str = Form(""),
    comment_1: str = Form(""),
    violation_2: str = Form(""),
    comment_2: str = Form(""),
    violation_3: str = Form(""),
    comment_3: str = Form(""),
    date: str = Form(""),
    user: dict = Depends(require_guild_access),
):
    guild_id = user["guild_id"]
    ally_code = ally_code.strip()

    if category not in WARNS_STRUCTURE:
        return RedirectResponse(f"/violations?{urlencode({'error': 'Некорректный режим (ТБ/ВГ/Рейд).'})}", status_code=303)
    if not ally_code:
        return RedirectResponse(f"/violations?{urlencode({'error': 'Выберите игрока из подсказок.'})}", status_code=303)

    if date:
        try:
            date_str = datetime.strptime(date, "%Y-%m-%d").strftime("%d.%m.%Y")
        except ValueError:
            return RedirectResponse(f"/violations?{urlencode({'error': 'Некорректная дата.'})}", status_code=303)
    else:
        date_str = datetime.now().strftime("%d.%m.%Y")

    # Дедуп одинаковых пунктов в одном сабмите — та же логика, что /нарушение (cogs/violations.py).
    pairs = [
        (violation_1, comment_1.strip() or None),
        (violation_2, comment_2.strip() or None),
        (violation_3, comment_3.strip() or None),
    ]
    seen = set()
    unique_pairs = []
    for v, c in pairs:
        if v and v in WARNS_STRUCTURE[category] and v not in seen:
            seen.add(v)
            unique_pairs.append((v, c))

    if not unique_pairs:
        return RedirectResponse(f"/violations?{urlencode({'error': 'Выберите хотя бы одно корректное нарушение.'})}", status_code=303)

    for v, c in unique_pairs:
        database.add_warn(ally_code, category, v, date_str, c, guild_id=guild_id)

    return RedirectResponse(f"/violations/{ally_code}", status_code=303)


@router.get("/violations/{ally_code}", response_class=HTMLResponse)
async def violation_dossier(request: Request, ally_code: str, user: dict = Depends(require_guild_access)):
    guild_id = user["guild_id"]
    names_by_code = {code: name for _discord_id, code, name in database.get_all_user_mappings(guild_id)}
    player_name = names_by_code.get(ally_code, ally_code)

    rows = database.get_player_warns(ally_code, guild_id=guild_id)
    three_months_ago = datetime.now() - timedelta(days=90)
    entries = []
    recent_count = 0
    for warn_id, category, subcategory, date_str, comment in rows:
        is_recent = False
        try:
            is_recent = datetime.strptime(date_str, "%d.%m.%Y") >= three_months_ago
        except ValueError:
            pass
        if is_recent:
            recent_count += 1
        entries.append({
            "id": warn_id, "category": category, "subcategory": subcategory,
            "date_str": date_str, "comment": comment, "recent": is_recent,
        })

    return templates.TemplateResponse(request, "violation_dossier.html", {
        "user": user,
        "ally_code": ally_code,
        "player_name": player_name,
        "entries": entries,
        "recent_count": recent_count,
        "lifetime_count": len(entries),
        "n_limit": dashboard_data.N_LIMIT,
        "error": request.query_params.get("error"),
    })


@router.post("/violations/{warn_id}/delete", response_class=HTMLResponse)
async def violation_delete(
    warn_id: int,
    ally_code: str = Form(...),
    user: dict = Depends(require_guild_access),
):
    database.remove_warn_by_id(warn_id, guild_id=user["guild_id"])
    return RedirectResponse(f"/violations/{ally_code}", status_code=303)


# Поля сгруппированы по режиму бота, который их использует — те же группы и подписи,
# что и в cogs/guild_settings.py::SETTINGS_GROUPS (веб-эквивалент /настройки список).
# ТБ раньше была одним блоком на 6 разнородных полей (широкий, плохо читался и на
# узких колонках переносился) — разбита на тег/план+ордер/отчёт вслед за тем, как уже
# называются сабкоманды (тб_ротация_*/тб_план_*/тб_ордер_*/тб_отчет_*), с отдельной
# короткой подсказкой под каждый смысловой блок вместо одного длинного абзаца.
# (поле в guilds, человекочитаемое название, "channel"|"role")
GUILD_SETTINGS_GROUPS = [
    {
        "name": "ТБ — тег на подготовку",
        "hint": "Раз в две недели, в начале «тегаемой» недели цикла ТБ, бот пишет в этот канал и тегает эту "
                "роль — напоминание проверить взвод и заказ склада перед стартом Территориальной битвы.",
        "fields": [
            ("ping_channel_id", "Канал для тега на ротацию/взводы", "channel"),
            ("ping_role_id", "Тегаемая роль", "role"),
        ],
    },
    {
        "name": "ТБ — план и ордер",
        "hint": "Канал, куда бот публикует план ТБ (планеты по этапам) и автоматические ордера на каждый этап. "
                "Ордера бот собирает из канала/ветки со стратегией и тегает в них указанную роль.",
        "fields": [
            ("tb_plan_channel_id", "Канал анонсов плана ТБ и автоордеров", "channel"),
            ("tb_order_source_channel_id", "Канал/ветка-источник стратегии по этапам", "channel"),
            ("tb_order_role_id", "Роль, тегаемая в автоордере", "role"),
        ],
    },
    {
        "name": "ТБ — итоговый отчёт",
        "hint": "Канал, куда бот публикует автоотчёт по итогам завершённой ТБ и уведомления офицерам.",
        "fields": [
            ("officer_channel_id", "Канал для автоотчёта и уведомлений", "channel"),
        ],
    },
    {
        "name": "День рождения",
        "hint": "Канал, куда бот пишет поздравление, и роль, которую выдаёт имениннику в день рождения.",
        "fields": [
            ("birthday_channel_id", "Канал для поздравлений с ДР", "channel"),
            ("birthday_role_id", "Роль, выдаваемая в ДР", "role"),
        ],
    },
]


@router.get("/settings", response_class=HTMLResponse)
async def guild_settings(request: Request, user: dict = Depends(require_guild_access)):
    guild_cfg = database.get_guild_config(user["guild_id"])
    groups = [
        {
            "name": group["name"],
            "hint": group["hint"],
            "rows": [
                {"field": field, "label": label, "kind": kind, "value": guild_cfg.get(field) or ""}
                for field, label, kind in group["fields"]
            ],
        }
        for group in GUILD_SETTINGS_GROUPS
    ]
    return templates.TemplateResponse(request, "guild_settings.html", {
        "user": user,
        "groups": groups,
        "error": request.query_params.get("error"),
        "saved": request.query_params.get("saved"),
    })


@router.post("/settings", response_class=HTMLResponse)
async def guild_settings_save(
    ping_channel_id: str = Form(""),
    ping_role_id: str = Form(""),
    birthday_channel_id: str = Form(""),
    birthday_role_id: str = Form(""),
    officer_channel_id: str = Form(""),
    tb_plan_channel_id: str = Form(""),
    tb_order_source_channel_id: str = Form(""),
    tb_order_role_id: str = Form(""),
    user: dict = Depends(require_guild_access),
):
    values = {
        "ping_channel_id": ping_channel_id,
        "ping_role_id": ping_role_id,
        "birthday_channel_id": birthday_channel_id,
        "birthday_role_id": birthday_role_id,
        "officer_channel_id": officer_channel_id,
        "tb_plan_channel_id": tb_plan_channel_id,
        "tb_order_source_channel_id": tb_order_source_channel_id,
        "tb_order_role_id": tb_order_role_id,
    }
    cleaned = {}
    for field, raw in values.items():
        raw = raw.strip()
        if raw and not raw.isdigit():
            return RedirectResponse(
                f"/settings?{urlencode({'error': f'ID должен состоять только из цифр ({field})'})}", status_code=303
            )
        cleaned[field] = raw or None
    database.update_guild_config(user["guild_id"], **cleaned)
    return RedirectResponse("/settings?saved=1", status_code=303)
