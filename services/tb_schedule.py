"""Бот-вайд (не per-guild) параметры расписания ТБ: якорь чётности "тегаемой"
недели + DST-коррекция времени триггеров.

Оба параметра раньше были в guilds.ping_start_date (только у AbsoluteChaos,
задавалось хардкодом в main.py::seed_default_guild) — стали общими, т.к.
чётность недели ТБ это факт игрового календаря, единый для всех обслуживаемых
гильдий, а не настройка одной гильдии. Хранилище — тот же generic bot_state
(database.get_bot_state/set_bot_state), бакет guild_id=1, тот же приём, что уже
используется для fun_toggle/mistral_usage (см. database.py) — новых таблиц не
требуется.

DST-коррекция нужна потому, что этап ТБ в игре стартует по европейскому
времени, которое дважды в год само сдвигается на летнее/зимнее (Россия — нет),
и без поправки со временем "уезжают" все MSK-триггеры сразу у всех гильдий.
Супер-админ (веб /admin/tb-schedule) один раз в полгода планирует переход
(смещение в минутах + дата вступления в силу) — эффективное смещение
вычисляется на лету по текущей дате, без скрытых промоушенов состояния."""

import json
from datetime import date, datetime

import database

# Дни (0=Пн..6=Вс) записи "ордер" в ping_schedule_json, которыми
# _tb_order_phase_for_weekday (cogs/guild_events.py) сопоставляет день недели с
# номером этапа ТБ (1-й день в списке = 1-й этап и т.д.). Это тоже факт общего
# игрового календаря ТБ (тот же реальный ВБ-цикл у всех гильдий бота), а не
# per-guild настройка — поэтому в веб-редакторе /settings поле "дни" officer'у
# не показывается и не редактируется, каждая строка расписания при сохранении
# получает именно этот список (см. web/routes/guild_dashboard.py). Значение
# совпадает с тем, что уже годами используется у AbsoluteChaos (main.py::
# PING_SCHEDULE), Вс исключён.
STANDARD_ORDER_DAYS = [0, 1, 2, 3, 4, 5]

_ANCHOR_KEY = "tb_week_anchor_date"
_OFFSET_KEY = "dst_offset_minutes"
_PENDING_KEY = "dst_pending_change"

# Дефолт — текущее значение PING_START_DATE из main.py (первый понедельник
# первой тегаемой недели), чтобы поведение AbsoluteChaos не изменилось в
# момент выкатки этой фичи, даже если якорь ни разу не будет переустановлен.
_DEFAULT_ANCHOR_DATE = "2026-07-06"


def get_week_anchor_date() -> date:
    raw = database.get_bot_state(_ANCHOR_KEY, guild_id=1) or _DEFAULT_ANCHOR_DATE
    return datetime.strptime(raw, "%Y-%m-%d").date()


def set_week_anchor_date(date_str: str, updated_by: str):
    # Валидирует формат тем же способом, что и чтение — некорректная дата
    # здесь должна упасть сразу при сохранении, а не тихо сломать чётность
    # недели у всех гильдий разом на следующем тике.
    datetime.strptime(date_str, "%Y-%m-%d")
    database.set_bot_state(_ANCHOR_KEY, date_str, guild_id=1)
    database.set_bot_state(f"{_ANCHOR_KEY}:updated_by", updated_by, guild_id=1)


def get_dst_offset_minutes() -> int:
    raw = database.get_bot_state(_OFFSET_KEY, guild_id=1)
    return int(raw) if raw else 0


def get_pending_dst_change() -> dict | None:
    raw = database.get_bot_state(_PENDING_KEY, guild_id=1)
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return None


def effective_dst_offset_minutes(as_of: date | None = None) -> int:
    """Действующая поправка (минуты, знак любой) на указанную дату (по
    умолчанию — сегодня по МСК, передаётся вызывающим кодом). Если запланирован
    переход и as_of уже на/после его даты — возвращает поправку из плана,
    иначе текущую. Ничего не мутирует — идемпотентно на каждый вызов, отдельного
    шага "применить план" не требуется."""
    pending = get_pending_dst_change()
    if pending:
        effective_date = datetime.strptime(pending["effective_date"], "%Y-%m-%d").date()
        if (as_of or date.today()) >= effective_date:
            return int(pending["offset_minutes"])
    return get_dst_offset_minutes()


def schedule_dst_change(effective_date: str, offset_minutes: int, updated_by: str, note: str = ""):
    datetime.strptime(effective_date, "%Y-%m-%d")
    payload = {"effective_date": effective_date, "offset_minutes": int(offset_minutes), "note": note}
    database.set_bot_state(_PENDING_KEY, json.dumps(payload), guild_id=1)
    database.set_bot_state(f"{_PENDING_KEY}:updated_by", updated_by, guild_id=1)


def cancel_pending_dst_change():
    database.set_bot_state(_PENDING_KEY, "", guild_id=1)


def apply_correction(hour: int, minute: int, offset_minutes: int) -> tuple[int, int]:
    total = (hour * 60 + minute + offset_minutes) % 1440
    return divmod(total, 60)
