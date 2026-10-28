"""Ордер ВИ по этапам в базе (2026-10-28): ордер больше не живёт в треде Discord.

Раньше ордер (database.tb_saved_plans) хранил только thread_id, а тексты этапов каждый раз
перечитывались из треда: автопост (cogs/guild_events.py::tb_order_loop), «Мой ВИ», взводы,
предпросмотр. Теперь тексты этапов — строки tb_order_phases, а тред к ордеру не привязывается
вовсе (решение пользователя: «отказаться от обязательного дубликата в дискорде»): тред или
сообщение — лишь источник разового импорта. Все создатели ордера идут через create_order
(план — "generated", картинка — "image", перенос треда — "thread", ссылки на сообщения —
"message", правка на сайте — "edited"); thread_id у таких ордеров пустой.

Ветка «тред главнее копии» (SOURCES_OVER_THREAD, resolve_blocks, fetch_plan_planets) осталась
для ордеров, у которых thread_id ещё задан: так шёл переход на идущей ВИ — сначала копии
этапов в базу при живом треде, сверка старого и нового кода, потом отвязка тредов.

tg_text — свой текст для Telegram; NULL = тот же текст, что для Discord (в tg-разметку его
переводит tg_bot/markup.py). Отправка с сайта — вручную по этапу (send_discord / send_telegram),
sent_*_at не даёт автопосту в 20:00 продублировать этап, уже отправленный руками в тот день."""

import os
import re
import sqlite3
import time
from datetime import datetime

import httpx

import database

SOURCES_OVER_THREAD = ("generated", "image", "message", "edited")
SOURCE_LABELS = {"thread": "перенесён из треда", "generated": "из плана", "image": "из картинки", "message": "из сообщения",
                 "edited": "правка на сайте"}
DISCORD_API = "https://discord.com/api/v10"
USER_AGENT = "DiscordBot (https://swgoh-sng.ru, 1.0)"
DISCORD_LIMIT = 2000
RECENT_SEND_HOURS = 20  # ручная отправка этапа за это время — автопост его пропускает
_MESSAGE_LINK_RE = re.compile(r"discord(?:app)?\.com/channels/(\d+|@me)/(\d+)/(\d+)")


def _ensure(cursor) -> None:
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS tb_order_phases (
            order_id INTEGER NOT NULL,
            phase INTEGER NOT NULL,
            text TEXT NOT NULL,
            tg_text TEXT,
            source TEXT NOT NULL,
            source_link TEXT,
            updated_by TEXT,
            updated_at REAL NOT NULL,
            sent_discord_at REAL,
            sent_tg_at REAL,
            PRIMARY KEY (order_id, phase)
        )
    """)


def _conn():
    conn = sqlite3.connect(database.DB_NAME)
    _ensure(conn.cursor())
    conn.commit()
    return conn


_COLS = ("order_id", "phase", "text", "tg_text", "source", "source_link", "updated_by", "updated_at",
         "sent_discord_at", "sent_tg_at")


def get_phases(order_id: int) -> dict[int, dict]:
    conn = _conn()
    rows = conn.execute(f"SELECT {', '.join(_COLS)} FROM tb_order_phases WHERE order_id = ?", (int(order_id),)).fetchall()
    conn.close()
    return {r[1]: dict(zip(_COLS, r)) for r in rows}


def overrides(order_id: int) -> dict[int, str]:
    """{этап: текст} — этапы, где база главнее треда."""
    return {ph: r["text"] for ph, r in get_phases(order_id).items() if r["source"] in SOURCES_OVER_THREAD}


def save_phase(order_id: int, phase: int, text: str, source: str, by: str | None = None,
               link: str | None = None, tg_text: str | None = None, keep_tg: bool = True) -> None:
    """Пишет текст этапа. keep_tg — не трогать уже заданный текст для Telegram."""
    conn = _conn()
    old = conn.execute("SELECT tg_text FROM tb_order_phases WHERE order_id = ? AND phase = ?",
                       (int(order_id), int(phase))).fetchone()
    if keep_tg and old and tg_text is None:
        tg_text = old[0]
    conn.execute("""
        INSERT INTO tb_order_phases (order_id, phase, text, tg_text, source, source_link, updated_by, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(order_id, phase) DO UPDATE SET text = excluded.text, tg_text = excluded.tg_text,
            source = excluded.source, source_link = excluded.source_link, updated_by = excluded.updated_by,
            updated_at = excluded.updated_at
    """, (int(order_id), int(phase), text.strip(), (tg_text or "").strip() or None, source, link, by, time.time()))
    conn.commit()
    conn.close()


def create_order(guild_id: int, name: str, blocks: dict[int, str], total_stars: int | None, created_by: str,
                 source: str, links: dict[int, str] | None = None) -> int:
    """Новый ордер (или замена одноимённого) — тексты этапов только в базе, без треда.
    С 2026-10-28 тред к ордеру не привязывается: он лишь источник разового импорта."""
    order_id = database.save_tb_plan(guild_id, name, "", total_stars, created_by=created_by)
    delete_order(order_id)  # одноимённый ордер заменяется целиком, старые этапы не остаются
    for phase, text in blocks.items():
        if text:
            save_phase(order_id, phase, text, source, by=created_by, link=(links or {}).get(phase), keep_tg=False)
    return order_id


def copy_from_thread(order_id: int, blocks: dict[int, str]) -> None:
    """Копии этапов из треда — только туда, где база не главнее (правки сайта не затираем)."""
    current = get_phases(order_id)
    for phase, text in blocks.items():
        row = current.get(phase)
        if text and (row is None or (row["source"] == "thread" and row["text"] != text.strip())):
            save_phase(order_id, phase, text, "thread", by="thread")


def delete_order(order_id: int) -> None:
    conn = _conn()
    conn.execute("DELETE FROM tb_order_phases WHERE order_id = ?", (int(order_id),))
    conn.commit()
    conn.close()


def mark_sent(order_id: int, phase: int, where: str) -> None:
    col = {"discord": "sent_discord_at", "tg": "sent_tg_at"}[where]
    conn = _conn()
    conn.execute(f"UPDATE tb_order_phases SET {col} = ? WHERE order_id = ? AND phase = ?",
                 (time.time(), int(order_id), int(phase)))
    conn.commit()
    conn.close()


def recently_sent(order_id: int, phase: int, where: str = "discord") -> bool:
    row = get_phases(order_id).get(int(phase))
    at = row and row["sent_discord_at" if where == "discord" else "sent_tg_at"]
    return bool(at) and time.time() - at < RECENT_SEND_HOURS * 3600


def phase_mismatch(text: str, phase: int) -> str | None:
    """Ошибка, если в тексте явно написан другой этап («5 этап» в поле 3-го) — частая ошибка
    при переносе по ссылкам. Номер ищем тем же шаблоном, что автопост в треде."""
    from cogs.guild_events import TB_ORDER_STAGE_RE, _tb_order_stage_number

    m = TB_ORDER_STAGE_RE.search(text)
    if m and _tb_order_stage_number(m) != str(phase):
        return f"В сообщении указан {_tb_order_stage_number(m)} этап, а вы переносите его в {phase}-й — проверьте ссылку."
    return None


def tg_text(row: dict) -> str:
    return row.get("tg_text") or row["text"]


# ------------------------------------------------------------------ Discord REST

def _headers() -> dict:
    token = os.environ.get("DISCORD_TOKEN")
    if not token:
        raise RuntimeError("DISCORD_TOKEN не настроен на сервере.")
    return {"Authorization": f"Bot {token}", "User-Agent": USER_AGENT}


async def thread_contents(thread_id: int | str) -> list[str]:
    """Все сообщения треда от старых к новым (как bot.history(oldest_first=True) в автопосте)."""
    out, before = [], None
    async with httpx.AsyncClient(timeout=20, headers=_headers()) as client:
        for _ in range(20):
            params = {"limit": 100, **({"before": before} if before else {})}
            resp = await client.get(f"{DISCORD_API}/channels/{thread_id}/messages", params=params)
            resp.raise_for_status()
            page = resp.json()
            out += page
            if len(page) < 100:
                break
            before = page[-1]["id"]
    return [m.get("content") or "" for m in reversed(out) if m.get("content")]


async def thread_blocks(thread_id: int | str) -> dict[int, str]:
    """{этап: блок} из треда — тот же выбор, что у автопоста (extract_tb_order_block)."""
    from cogs.guild_events import extract_tb_order_block

    contents = await thread_contents(thread_id)
    blocks = {ph: extract_tb_order_block(contents, str(ph)) for ph in range(1, 7)}
    return {ph: b for ph, b in blocks.items() if b}


async def import_thread(guild_id: int, thread_id: int | str, name: str, total_stars: int | None, created_by: str) -> int:
    """Разовый перенос ордера из треда: тексты 6 этапов копируются в базу, тред к ордеру не
    привязывается. RuntimeError, если тред не читается или в нём нет всех 6 этапов."""
    try:
        blocks = await thread_blocks(thread_id)
    except httpx.HTTPStatusError as e:
        if e.response.status_code in (403, 404):
            raise RuntimeError("Тред не найден или у бота нет к нему доступа — проверьте ссылку.")
        raise RuntimeError(f"Discord ответил {e.response.status_code}.")
    missing = [ph for ph in range(1, 7) if ph not in blocks]
    if missing:
        found = ", ".join(map(str, sorted(blocks))) or "ни одного"
        raise RuntimeError(f"В треде нашлись этапы: {found}; не хватает: {', '.join(map(str, missing))}. "
                           "В каждом сообщении должен быть номер этапа («1 этап» или «этап 1»).")
    return create_order(guild_id, name, blocks, total_stars, created_by, "thread")


def parse_message_link(link: str) -> tuple[str, str] | None:
    m = _MESSAGE_LINK_RE.search(link or "")
    return (m.group(2), m.group(3)) if m else None


async def fetch_message(link: str) -> str:
    """Текст сообщения Discord по ссылке. RuntimeError с понятным текстом при ошибке."""
    ids = parse_message_link(link)
    if not ids:
        raise RuntimeError("Это не ссылка на сообщение Discord (нужна вида discord.com/channels/…/…/…: "
                           "ПКМ по сообщению → «Копировать ссылку на сообщение»).")
    channel_id, message_id = ids
    async with httpx.AsyncClient(timeout=15, headers=_headers()) as client:
        resp = await client.get(f"{DISCORD_API}/channels/{channel_id}/messages/{message_id}")
    if resp.status_code in (403, 404):
        raise RuntimeError("Сообщение не найдено или у бота нет доступа к этому каналу.")
    if resp.status_code >= 400:
        raise RuntimeError(f"Discord ответил {resp.status_code}.")
    content = (resp.json().get("content") or "").strip()
    if not content:
        raise RuntimeError("В сообщении нет текста (только картинка/вложение?).")
    return content


async def resolve_blocks(order: dict) -> tuple[dict[int, dict], str | None]:
    """Что бот опубликует на каждый этап: {этап: {text, source, link, row}} + ошибка чтения треда.
    Порядок: правка/импорт/план в базе → живой тред (и обновить его копию) → копия треда."""
    rows = get_phases(order["id"])
    result, error = {}, None
    live = {}
    if order.get("thread_id"):
        try:
            live = await thread_blocks(order["thread_id"])
            copy_from_thread(order["id"], live)
            rows = get_phases(order["id"])
        except (httpx.HTTPError, RuntimeError) as e:
            error = f"Тред ордера не прочитался ({e}) — показаны сохранённые копии."
    for phase in range(1, 7):
        row = rows.get(phase)
        if row and row["source"] in SOURCES_OVER_THREAD:
            result[phase] = {"text": row["text"], "source": row["source"], "link": row["source_link"], "row": row}
        elif phase in live:
            result[phase] = {"text": live[phase], "source": "thread", "link": None, "row": row}
        elif row:
            result[phase] = {"text": row["text"], "source": row["source"], "link": row["source_link"], "row": row}
    return result, error


async def migrate_all() -> dict:
    """Разовый перенос: копии этапов всех ордеров с тредами в базу (source="thread")."""
    conn = sqlite3.connect(database.DB_NAME)
    orders = conn.execute("SELECT id, guild_id, name, thread_id FROM tb_saved_plans").fetchall()
    conn.close()
    stats = {"orders": 0, "phases": 0, "errors": []}
    for order_id, guild_id, name, thread_id in orders:
        if not thread_id:
            continue
        try:
            blocks = await thread_blocks(thread_id)
        except (httpx.HTTPError, RuntimeError) as e:
            stats["errors"].append(f"{guild_id}/{name}: {e}")
            continue
        copy_from_thread(order_id, blocks)
        stats["orders"] += 1
        stats["phases"] += len(blocks)
    return stats


# ------------------------------------------------------------------ отправка

def discord_chunks(text: str, role_id: str | None) -> list[str]:
    """Как автопост: текст этапа, две пустые строки, упоминание роли; режем по 2000."""
    from cogs.guild_events import chunk_tb_order_message

    full = f"{text}\n\n\n<@&{role_id}>" if role_id else text
    return chunk_tb_order_message(full, DISCORD_LIMIT)


async def send_discord(guild_id: int, order_id: int, phase: int) -> str:
    cfg = database.get_guild_config(guild_id) or {}
    channel_id, role_id = cfg.get("tb_plan_channel_id"), cfg.get("tb_order_role_id")
    if not channel_id:
        raise RuntimeError("Не задан канал плана ВИ (Настройки → «ВИ — план и ордер»).")
    row = get_phases(order_id).get(int(phase))
    if not row:
        raise RuntimeError(f"У этапа {phase} нет текста.")
    payload_mentions = {"parse": [], "roles": [str(role_id)] if role_id else []}
    from services import channel_health
    async with httpx.AsyncClient(timeout=20, headers=_headers()) as client:
        for chunk in discord_chunks(row["text"], role_id):
            resp = await client.post(f"{DISCORD_API}/channels/{channel_id}/messages",
                                     json={"content": chunk, "allowed_mentions": payload_mentions})
            if resp.status_code >= 400:
                await channel_health.report_failure(str(channel_id), resp.status_code)
                raise RuntimeError(f"Discord ответил {resp.status_code} — проверьте права бота в канале.")
    channel_health.report_success(str(channel_id))
    mark_sent(order_id, phase, "discord")
    return f"Этап {phase} отправлен в канал Discord."


def send_telegram(guild_id: int, order_id: int, phase: int) -> str:
    from tg_bot import outbox

    row = get_phases(order_id).get(int(phase))
    if not row:
        raise RuntimeError(f"У этапа {phase} нет текста.")
    if not outbox.enqueue_guild(guild_id, "tb_order", tg_text(row)):
        raise RuntimeError("Нет Telegram-чата гильдии с включённым оповещением «Ордер ВИ» "
                           "(в чате: /bind, затем /notify).")
    mark_sent(order_id, phase, "tg")
    return f"Этап {phase} поставлен в очередь Telegram — уйдёт в течение нескольких секунд."


# ------------------------------------------------------------------ текущий этап

def current_phase(now: datetime | None = None) -> int | None:
    """Этап идущей ВИ по общему календарю автопоста (cogs/guild_events.py::tb_order_loop):
    неделя ВИ — по якорю tb_schedule, этап N публикуется в 20:00 МСК N-го дня. None — ВИ не идёт."""
    from zoneinfo import ZoneInfo
    from services import tb_schedule

    now = now or datetime.now(ZoneInfo("Europe/Moscow"))
    if ((now.date() - tb_schedule.get_week_anchor_date()).days // 7) % 2 != 0:
        return None
    hour, minute = tb_schedule.apply_correction(20, 0, tb_schedule.effective_dst_offset_minutes(now.date()))
    days = sorted(tb_schedule.STANDARD_ORDER_DAYS)
    started = [i + 1 for i, wd in enumerate(days)
               if wd < now.weekday() or (wd == now.weekday() and (now.hour, now.minute) >= (hour, minute))]
    return started[-1] if started else None
