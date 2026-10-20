"""Рейды гильдии: очки каждого участника за завершённые рейды (как C3PO `raid history`
и HotBot `/raids scorehistory`).

Источник — comlink.get_guild(..., include_recent_guild_activity_info=True):
guild.recentRaidResult — последние завершённые рейды (проверено вживую 2026-10-20: три
последних «Приказа 66»), у каждого raidId, identifier.campaignNodeDifficulty, endTime
(эпоха, с) и raidMember[] = {playerId, memberProgress (очки), memberRank}. Текущий,
ещё идущий рейд без входа в аккаунт игры не виден (raidStatus пуст), поэтому история —
только по завершённым; копится в raid_results/raid_member_scores (cogs/raids.py).

Логика без Discord: цикл и команды — cogs/raids.py, страница — веб /raids."""

from datetime import datetime
from zoneinfo import ZoneInfo

MSK = ZoneInfo("Europe/Moscow")

# raidId из Comlink -> русское название; неизвестный — как есть.
RAID_NAMES = {
    "order66": "Приказ 66",
    "speederbike": "Набу",
    "naboo": "Набу",
    "kraytdragon": "Крайт-дракон",
    "endor": "Эндор",
    "rancor": "Логово ранкора",
    "rancor_challenge": "Логово ранкора (испытание)",
    "aat": "Танковый рейд",
    "sith_raid": "Тройной рейд ситхов",
}


def raid_name(raid_id: str) -> str:
    return RAID_NAMES.get((raid_id or "").lower(), raid_id or "?")


def raid_key(raid_id: str, end_time: int) -> str:
    return f"{raid_id}:{end_time}"


def parse_recent(resp: dict) -> list[dict]:
    """[{key, raid_id, difficulty, end_time, guild_score, members: [{player_id, score, rank}],
    names: {player_id: playerName}}] — по завершённым рейдам из ответа get_guild."""
    guild = resp.get("guild", resp) if isinstance(resp, dict) else {}
    names = {m.get("playerId"): m.get("playerName") or "" for m in guild.get("member") or [] if m.get("playerId")}
    raids = []
    for r in guild.get("recentRaidResult") or []:
        raid_id = r.get("raidId") or ""
        end = str(r.get("endTime") or "")
        if not raid_id or not end.isdigit():
            continue
        members = []
        for m in r.get("raidMember") or []:
            pid = m.get("playerId")
            if not pid:
                continue
            members.append({
                "player_id": pid,
                "score": int(m.get("memberProgress") or 0),
                "rank": int(m.get("memberRank") or 0) or None,
            })
        raids.append({
            "key": raid_key(raid_id, int(end)),
            "raid_id": raid_id,
            "difficulty": (r.get("identifier") or {}).get("campaignNodeDifficulty"),
            "end_time": int(end),
            "guild_score": sum(m["score"] for m in members),
            "members": members,
            "names": names,
        })
    return raids


def end_date(end_time: int) -> str:
    return datetime.fromtimestamp(end_time, MSK).strftime("%d.%m.%Y")


def millions(score: int | float | None) -> str:
    """12 150 000 -> «12,15 млн»; меньше миллиона — тысячами."""
    if score is None:
        return "—"
    if score >= 1_000_000:
        text = f"{score / 1_000_000:.2f}".rstrip("0").rstrip(".")
        return text.replace(".", ",") + " млн"
    return f"{score / 1000:.0f} тыс."


def summarize(raid: dict, scores: list[dict]) -> dict:
    """Итог рейда для отчёта: участники по убыванию очков, среднее, медиана."""
    ranked = sorted(scores, key=lambda s: -s["score"])
    values = [s["score"] for s in ranked]
    n = len(values)
    median = (values[n // 2] if n % 2 else (values[n // 2 - 1] + values[n // 2]) / 2) if n else 0
    return {
        "raid": raid,
        "ranked": ranked,
        "total": sum(values),
        "avg": sum(values) / n if n else 0,
        "median": median,
        "count": n,
    }
