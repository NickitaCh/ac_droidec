import asyncio
import os
import re
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

import httpx
from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

import database
import tb_platoon_data
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
# КОНСТРУКТОР ВЗВОДОВ ТБ: для планет, которые реально задействованы в текущем активном
# плане ордера (guilds.tb_active_plan_id → tb_saved_plans.thread_id), показывает 6
# операций и подсказки юнитов на каждую (tb_platoon_data.ROTE_PLATOON_SUGGESTIONS,
# см. память project_tb_platoon_unit_lists_rote_2026-08-27). Пока ЧИСТО НА ЧТЕНИЕ —
# по прямому запросу пользователя 2026-08-27: сначала просто список юнитов, механику
# назначения игрок/юнит откладываем на следующий заход (не путать с уже существующими
# database.*_tb_platoon_assignment* — они написаны заранее под старую, пофазовую
# модель и потребуют переделки под планеты, прежде чем их подключать сюда).
#
# Активный план хранит только discord thread_id (см. cogs/tb_order_image.py) — самого
# разбора "какая планета на каком этапе" нигде в БД не остаётся, только опубликованные
# ботом сообщения-блоки в этом треде ("## Восход Империи — N этап" + по кружку-эмодзи
# на планету). Поэтому здесь эти сообщения перечитываются напрямую через Discord REST
# (bot-token техника, уже проверенная в этом проекте — см. память
# project_tw_counter_order_builder.md), а не через живой бот (веб-процесс его не поднимает).
# =====================================================================

# Дублирует cogs/guild_events.py::TB_PLAN_HEADER_RE/TB_PLAN_CIRCLE_CHARS — тот же принцип
# развязки веб-процесса от cogs/*, что и у TB_THREAD_LINK_RE выше в этом файле.
_TB_PLAN_HEADER_RE = re.compile(r"Восход\s+Импери\w*\s*[—\-]\s*(\d+)\s*этап", re.IGNORECASE)
_TB_PLAN_CIRCLE_CHARS = "🔴🟠🟡🟢🔵🟣⚫⚪"

# Русские названия планет, которые публикует cogs/tb_order_image.py::_translate_planet —
# обратный словарь к его PLANET_RU, ключи которого — названия из tb_platoon_data.py.
# Death Star/Hoth — стандартные локализации, НЕ подтверждены живым постом бота (эта
# гильдия пока не доходила до 6 этапа в реальном прогрессе) — если появятся в реальном
# ордере под другим названием, поправить здесь.
_PLANET_NAME_TO_RU = {
    "Mustafar": "мустафар", "Corellia": "кореллия", "Coruscant": "корусант",
    "Geonosis": "джеонозис", "Felucia": "фелуция", "Bracca": "бракка",
    "Dathomir": "датомир", "Tatooine": "татуин", "Kashyyyk": "кашиик",
    "Haven-class Medical Station": "медстанция", "Kessel": "кессель", "Lothal": "лотал",
    "Malachor": "малакор", "Vandor": "вандор", "Ring of Kafrene": "кольцо кафрены",
    "Death Star": "звезда смерти", "Hoth": "хот", "Scarif": "скариф",
    "Zeffo": "зеффо", "Mandalore": "мандалор",
}
_RU_TO_PLANET_NAME = {ru: en for en, ru in _PLANET_NAME_TO_RU.items()}


async def _fetch_active_plan_planets(guild_id: int) -> tuple[list[dict], str | None]:
    """Возвращает (список {"planet": англ.название|None, "raw": как было в сообщении,
    "round": N}, текст_ошибки|None). Планета=None — распознали блок этапа, но название
    не нашлось в _RU_TO_PLANET_NAME (новая планета/опечатка — raw всё равно показываем)."""
    guild_cfg = database.get_guild_config(guild_id) or {}
    plan_id = guild_cfg.get("tb_active_plan_id")
    if not plan_id:
        return [], "Нет активного плана ордера — выберите его на странице «Планы ордера»."
    plan = database.get_tb_saved_plan(int(plan_id))
    if not plan or plan["guild_id"] != guild_id:
        return [], "Активный план не найден — выберите его заново на странице «Планы ордера»."

    token = os.environ.get("DISCORD_TOKEN")
    if not token:
        return [], "DISCORD_TOKEN не настроен на веб-процессе — не могу прочитать тред плана."

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
    by_round: dict[int, str] = {}
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
            planet = _RU_TO_PLANET_NAME.get(name_ru)
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
            entries.append({
                "planet": planet,
                "raw": stripped[1:].strip(" *#"),
                "round": round_num,
            })
    return entries, None


@router.get("/tb/platoons", response_class=HTMLResponse)
async def tb_platoons(request: Request, user: dict = Depends(require_guild_access)):
    guild_id = user["guild_id"]
    entries, error = await _fetch_active_plan_planets(guild_id)

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

    rounds = []
    for round_num in sorted(by_round):
        planet_blocks = []
        for e in by_round[round_num]:
            operations = []
            for operation in range(1, 7):
                units = tb_platoon_data.ROTE_PLATOON_SUGGESTIONS.get((e["planet"], operation)) if e["planet"] else None
                operations.append({"number": operation, "units": units})
            planet_blocks.append({
                "name": e["planet"] or e["raw"],
                "unresolved": e["planet"] is None,
                "operations": operations,
            })
        rounds.append({"number": round_num, "planets": planet_blocks})

    return templates.TemplateResponse(request, "tb_platoons.html", {
        "user": user,
        "rounds": rounds,
        "error": error,
    })


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
