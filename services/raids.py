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
    "rotj": "Битва при Эндоре",
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


# ---------------------------------------------------------------- готовность к рейду

RAID_UNITS_STATE = "raid_entry_units"   # bot_state guild 0: {node_id: {"units": [...], "min_stars": n}}
CAMPAIGN_FLAG = 274877906944


def parse_raid_entry_units(campaigns: list, units: list, known: set) -> dict:
    """Кого пускают в каждый рейд — из условий входа узла GUILD/RAIDS (категории
    entryCategoryAllowed + минимум звёзд) и категорий персонажей. Проверено 2026-10-21:
    ORDER66 → категория raid_order66_allowed, 5★, 38 персонажей — как у WookieeBot raid ready."""
    by_category: dict[str, set] = {}
    for u in units:
        if u.get("rarity") != 7 or not u.get("obtainable") or u.get("baseId") not in known:
            continue
        for cat in u.get("categoryId") or []:
            by_category.setdefault(cat, set()).add(u["baseId"])
    result = {}
    for camp in campaigns:
        if camp.get("id") != "GUILD":
            continue
        for cmap in camp.get("campaignMap") or []:
            if cmap.get("id") != "RAIDS":
                continue
            for group in cmap.get("campaignNodeDifficultyGroup") or []:
                for node in group.get("campaignNode") or []:
                    allowed_units, min_stars = set(), 0
                    for mission in node.get("campaignNodeMission") or []:
                        allowed = mission.get("entryCategoryAllowed") or {}
                        for cat in allowed.get("categoryId") or []:
                            allowed_units |= by_category.get(cat, set())
                        allowed_units |= {m["id"] for m in allowed.get("mandatoryRosterUnit") or [] if m.get("id") in known}
                        min_stars = max(min_stars, min(allowed.get("minimumUnitRarity") or 0, 7))
                    # Узлы без поимённого списка (старые рейды «любой персонаж») пропускаем.
                    if allowed_units and len(allowed_units) < 200:
                        result[node["id"].lower()] = {"units": sorted(allowed_units), "min_stars": min_stars}
    return result


def _raid_units_data() -> dict:
    import json
    import database
    raw = database.get_bot_state(RAID_UNITS_STATE, guild_id=0)
    return json.loads(raw) if raw else {}


def raid_entry_units(raid_id: str) -> dict | None:
    return _raid_units_data().get((raid_id or "").lower())


def resolve_raid_kind(text: str) -> str | None:
    """Рейд по id или по названию, вписанному руками (подсказка Discord не успела
    подгрузиться): «order66», «приказ 66», «крайт» → id рейда с загруженным составом."""
    data = _raid_units_data()
    needle = (text or "").strip().lower()
    if needle in data:
        return needle
    hits = [k for k in data if needle and (needle in raid_name(k).lower() or needle in k)]
    return hits[0] if len(hits) == 1 else None


# Отряды рейда — как у WookieeBot raid ready (в данных игры деления на отряды нет: только
# общая категория raid_<рейд>_allowed). Порядок — как в их таблице. Персонаж, допущенный
# игрой, но не попавший ни в один отряд (новый в рейде), уходит в «Прочие».
RAID_TEAMS = {
    "order66": [
        ("Империя (Таркин и клоны)", ["GRANDMOFFTARKIN", "APPO", "OPERATIVE", "DISGUISEDCLONETROOPER", "SCORCH"]),
        ("Бракованная партия 2.0", ["OMEGAS3", "HUNTERS3", "WRECKERS3", "CROSSHAIRS3", "BATCHERS3"]),
        ("Авангард джедаев", ["JEDIMASTERMACEWINDU", "JOCASTANU", "VANGUARDTEMPLEGUARD", "DEPABILLABA", "AAYLASECURA",
                              "BARRISSOFFEE", "EETHKOTH", "IMAGUNDI", "JEDIKNIGHTCONSULAR", "JEDIKNIGHTGUARDIAN",
                              "KELLERANBEQ", "KITFISTO", "LUMINARAUNDULI", "PLOKOON", "QUIGONJINN", "SHAAKTI"]),
        ("Пираты", ["GLHONDO", "SM33", "ENFYSNEST", "HONDO", "SANASTARROS", "ITHANO", "QUIGGOLD", "KIX", "BRUTUS",
                    "VANE", "CAPTAINSILVO", "MAZKANATA"]),
    ],
}


def raid_teams(raid_id: str, allowed: list[str], names: dict) -> list[tuple[str, list[str]]]:
    """[(отряд, [base_id])] только из допущенных игрой персонажей; без известных отрядов —
    один «Все персонажи» по алфавиту."""
    allowed_set = set(allowed)
    teams = [(name, [b for b in ids if b in allowed_set]) for name, ids in RAID_TEAMS.get((raid_id or "").lower(), [])]
    teams = [(name, ids) for name, ids in teams if ids]
    placed = {b for _n, ids in teams for b in ids}
    rest = sorted((b for b in allowed if b not in placed), key=lambda b: (names.get(b) or b).lower())
    if rest:
        teams.append(("Прочие" if teams else "Все персонажи", rest))
    return teams
