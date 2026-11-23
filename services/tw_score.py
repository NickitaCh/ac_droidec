"""Territory War scores and full-clear potential from the game's map snapshot."""
from collections import Counter

from services.game_event_live import TW_ACTIVE_SQUAD_STATUSES, TW_SQUAD_DEFEATED, tw_remaining
from services.tb_live import OPEN_ZONE
from tw_def_engine import ZONES

_LOCKED = 1
_CAPTURED = 4
_SQUAD_POINTS = {"character": (30, 20), "ship": (34, 22)}  # Defense placement, first-try victory ceiling.


def _sum_known(values):
    numbers = list(values)
    return sum(numbers) if numbers and None not in numbers else None


def _failed_attempts(team):
    if team.get("status") not in (*TW_ACTIVE_SQUAD_STATUSES, TW_SQUAD_DEFEATED):
        return 0
    # A locked battle and a victory both occupy the last attempt in the counter.
    return max(0, team.get("holds", 0) - (team["status"] in (2, TW_SQUAD_DEFEATED)))


def _deployed(zone):
    if "remaining" in zone:
        return zone.get("deployed")
    # Older combat snapshots stored the surviving squads under `deployed`.
    return _sum_known((tw_remaining(zone), zone.get("defeated")))


def _zone_report(layout, target, own):
    defense_points, win_cap = _SQUAD_POINTS[layout["kind"]]
    capacity, defeated = target.get("capacity"), target.get("defeated")
    remaining, status = tw_remaining(target), target.get("status")
    teams = target.get("teams", ())
    state_counts = Counter(team.get("status") for team in teams)
    complete = (defeated is not None and remaining is not None
                and state_counts[TW_SQUAD_DEFEATED] == defeated
                and sum(state_counts[state] for state in TW_ACTIVE_SQUAD_STATUSES) == remaining)
    earned_cap, available, lost = None, None, None
    if remaining is not None and capacity is not None and 0 <= remaining <= capacity:
        # Each of the first two failed attacks costs five banners on the next win.
        retry_loss = sum(5 * min(_failed_attempts(team), 2) for team in teams
                         if team.get("status") in TW_ACTIVE_SQUAD_STATUSES)
        maximum = remaining * win_cap - retry_loss
        capture = (900 if layout["column"] == 4 else 450) + 10 * capacity
        if status == _CAPTURED and remaining == 0:
            available = 0
            earned_cap = capacity * win_cap
        elif status in (_LOCKED, OPEN_ZONE) and maximum >= 0:
            available = maximum + capture
            if defeated is not None:
                earned_cap = defeated * win_cap
        placed, points = _deployed(own), own.get("points")
        if earned_cap is not None and placed is not None and points is not None:
            # A guild's mirrored zone score contains its placement, wins and capture.
            attack_points = points - placed * defense_points - (capture if status == _CAPTURED else 0)
            if 0 <= attack_points <= earned_cap:
                lost = earned_cap - attack_points + retry_loss

    first_wins = sum(team.get("status") == TW_SQUAD_DEFEATED and team.get("holds") == 1 for team in teams)
    return {
        "name": layout["label"], "capacity": capacity, "defeated": defeated, "captured": status == _CAPTURED,
        "first": round(first_wins * 100 / defeated) if complete and defeated else None,
        "holds": sum(map(_failed_attempts, teams)) if complete or status == _LOCKED else None,
        "lost": lost, "available": available,
    }


def _top_defenders(zones):
    counts = Counter()
    names = {}
    for zone in zones:
        for team in zone.get("teams", ()):
            if (name := team.get("player_name")) and (holds := _failed_attempts(team)):
                identifier = team.get("player_id") or name
                counts[identifier] += holds
                names[identifier] = name
    return [{"name": names[identifier], "holds": holds} for identifier, holds in counts.most_common(3)]


def analysis(war):
    """Report the current score and upper limit from clearing all remaining zones."""
    from services.tw_live_image import zone_key

    if not war or not war.get("round"):
        return []
    maps = {side: {zone_key(zone["id"]): zone for zone in war[side]} for side in ("own", "opponent")}
    result = []
    for side, target, profile, fallback in (("own", "opponent", "home_guild", "Своя гильдия"),
                                            ("opponent", "own", "opponent_guild", "Соперник")):
        zones = [_zone_report(layout, maps[target][key], maps[side].get(key, {})) for layout in ZONES
                 if (key := layout["key"]) in maps[target]]
        current = _sum_known(zone.get("points") for zone in war[side])
        potential = _sum_known(zone["available"] for zone in zones) if len(zones) == len(ZONES) else None
        result.append({"name": war.get(profile, {}).get("name") or fallback, "zones": zones,
                       "current": current, "potential": potential, "maximum": _sum_known((current, potential)),
                       "defenders": _top_defenders(war[side])})
    return result
