"""Энка (рейдовые билеты): кто сколько сдал за игровой день гильдии.

Источник — comlink.get_guild(..., include_recent_guild_activity_info=True):
member[].memberContribution с type == 2 — билеты, currentValue = сдано за текущий
день, lifetimeValue = за всё время; guild.nextChallengesRefresh — момент сброса дня
(эпоха, секунды). Проверено вживую 2026-10-19 против поста HotBot того же дня.

После сброса currentValue обнуляется, поэтому итог дня считается по последнему
снимку до сброса и приросту lifetimeValue: итог = снимок.current + (lifetime_после −
снимок.lifetime) − current_после — так учитываются и билеты, сданные уже после снимка.

Логика без Discord: цикл и отправка — cogs/raid_tickets.py, история — веб /tickets.
"""

import random
from datetime import datetime
from zoneinfo import ZoneInfo

MSK = ZoneInfo("Europe/Moscow")
REQUIRED = 600  # дневной максимум билетов в игре
CONTRIBUTION_TICKETS = 2

# (ключ, секунд до сброса) — напоминания с тегами; итог — после сброса.
REMINDERS = (("r120", 2 * 3600), ("r30", 30 * 60))
SNAPSHOT_BEFORE = 5 * 60   # последний тихий снимок перед сбросом
FINAL_AFTER = 2 * 60       # итог — через 2 минуты после сброса
STAGE_WINDOW = 15 * 60     # опоздали дольше (бот лежал) — напоминание не шлём
FINAL_WINDOW = 3600

# Похвала в итоге дня, когда сдали все (только после среза — до него молчим).
PRAISE = (
    "Все молодцы, так держать! 💪",
    "Красавчики, ни одного должника! 👏",
    "Вот это гильдия — спасибо каждому! ❤️",
    "Идеальный день, рейды скажут спасибо! 🎉",
    "Ни одного хвоста — уважение всем! 🙌",
)
STREAK_PRAISE = (
    "держим планку!",
    "так и продолжаем!",
    "это уже традиция!",
    "легенды, не иначе!",
)


def parse_guild(resp: dict) -> tuple[int | None, list[dict]]:
    """(момент сброса, [{player_id, name, current, lifetime}])."""
    guild = resp.get("guild", resp) if isinstance(resp, dict) else {}
    raw_reset = guild.get("nextChallengesRefresh")
    reset = int(raw_reset) if str(raw_reset or "").isdigit() else None
    members = []
    for m in guild.get("member") or []:
        contrib = next((c for c in m.get("memberContribution") or [] if c.get("type") == CONTRIBUTION_TICKETS), None)
        if not m.get("playerId") or contrib is None:
            continue
        members.append({
            "player_id": m["playerId"],
            "name": m.get("playerName") or "",
            "current": int(contrib.get("currentValue") or 0),
            "lifetime": int(contrib.get("lifetimeValue") or 0),
        })
    return reset, members


def snapshot(members: list[dict]) -> dict:
    return {m["player_id"]: [m["current"], m["lifetime"]] for m in members}


def final_totals(snap: dict, after: list[dict]) -> dict:
    """{player_id: билетов за закончившийся день} — только для тех, кто есть и в снимке,
    и в составе после сброса (новички без снимка и ушедшие не оцениваются)."""
    totals = {}
    for m in after:
        prev = snap.get(m["player_id"])
        if not prev:
            continue
        cur, life = prev
        total = cur + (m["lifetime"] - life) - m["current"]
        totals[m["player_id"]] = max(cur, min(total, REQUIRED))
    return totals


def day_label(reset: int) -> str:
    """Дата сброса по МСК — день, который этим сбросом закончился."""
    return datetime.fromtimestamp(reset, MSK).date().isoformat()


def reset_time_text(reset: int) -> str:
    return datetime.fromtimestamp(reset, MSK).strftime("%H:%M")


def _left_text(seconds: int) -> str:
    minutes = max(0, round(seconds / 60))
    hours, minutes = divmod(minutes, 60)
    if hours and minutes:
        return f"{hours} ч {minutes} мин"
    return f"{hours} ч" if hours else f"{minutes} мин"


def _days_word(n: int) -> str:
    if n % 10 == 1 and n % 100 != 11:
        return "день"
    if n % 10 in (2, 3, 4) and n % 100 not in (12, 13, 14):
        return "дня"
    return "дней"


def reminder_lines(guild_name: str, reset: int, now: int, debtors: list) -> list[str]:
    """debtors: [(name, tickets, discord_id | None)] — уже отсортированы, не пусто
    (если сдали все — all_done_lines)."""
    head = (f"🎟️ **{guild_name}**: не забудьте сдать энку! Срез в {reset_time_text(reset)} МСК "
            f"(через {_left_text(reset - now)})")
    lines = [head]
    for name, tickets, discord_id in debtors:
        who = f"<@{discord_id}> {name}" if discord_id else name
        lines.append(f"{who} — **{tickets}**/{REQUIRED}")
    return lines


def dm_text(guild_name: str, reset: int, now: int, name: str, tickets: int) -> str:
    """Личка недосдавшему (опция гильдии raid_tickets_dm)."""
    return (f"🎟️ {name}, в гильдии **{guild_name}** у тебя сдано **{tickets}**/{REQUIRED} энки. "
            f"Срез в {reset_time_text(reset)} МСК (через {_left_text(reset - now)}) — не забудь досдать!")


def all_done_lines(guild_name: str, reset: int, now: int) -> list[str]:
    return [f"✅ **{guild_name}**: проверка энки — все сдали {REQUIRED}/{REQUIRED}, "
            f"до среза в {reset_time_text(reset)} МСК ещё {_left_text(reset - now)}."]


def final_lines(guild_name: str, debtors: list, streak: int) -> list[str]:
    """debtors: [(name, tickets)] без тегов. Пусто → похвала (+ серия «N дней подряд»)."""
    if not debtors:
        lines = [f"🏆 **{guild_name}**: энку сдали все — по {REQUIRED}/{REQUIRED}! {random.choice(PRAISE)}"]
        if streak > 1:
            lines.append(f"🔥 Без единой недосдачи уже {streak} {_days_word(streak)} подряд — {random.choice(STREAK_PRAISE)}")
        return lines
    lines = [f"📋 **{guild_name}**: итог дня — не сдали энку ({len(debtors)}):"]
    lines += [f"{name} — **{tickets}**/{REQUIRED}" for name, tickets in debtors]
    return lines


def chunk(lines: list[str], limit: int = 1900) -> list[str]:
    chunks, cur = [], ""
    for line in lines:
        if cur and len(cur) + 1 + len(line) > limit:
            chunks.append(cur)
            cur = line
        else:
            cur = f"{cur}\n{line}" if cur else line
    if cur:
        chunks.append(cur)
    return chunks
