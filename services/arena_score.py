"""Remaining Grand Arena battle/territory ceilings, using game point structures.

Like HotBot, the main projection covers battles and territories. An unearned
first-attack award is returned separately, so the UI can show the complete total
without silently disagreeing with HotBot's two projection rows.
"""
from services import arena_rules

_REINFORCEMENTS = 4  # Four reinforcement cells, in addition to the starting fleet.


def _team_ceiling(team, rule, undersized):
    points = rule["points"]
    slots = rule["size"] + (_REINFORCEMENTS if rule["kind"] == 2 else 0)
    own = rule["minimum"] if undersized else slots
    attempts = max(0, (team.get("defends") or 0) - (team.get("state") == 2))
    bonuses = {int(key): value for key, value in points["attempts"].items()}
    attempt_bonus = bonuses[max(key for key in bonuses if key <= attempts)]
    enemies = slots
    # After a failed attack, bonuses for killed/unset enemies have already been
    # earned. Remaining opponents' health comes from SquadCell.unitState.
    health = team.get("health")
    if attempts and health and None not in health:
        enemies = sum(value > 0 for value in health)
    return (points["offensiveWinReward"] + attempt_bonus
            + own * (points["survivingSquadUnitBonus"] + points["fullHealthUnitBonus"]
                     + points["fullProtectionUnitBonus"])
            + (slots - own) * points["emptySquadSlotBonus"]
            + enemies * points.get("enemyDefeatedBonus", 0))


def _projection(match, side, rules):
    current = (match.get("score") or {}).get(side)
    target = "away" if side == "home" else "home"
    zones = {zone["id"]: zone for zone in match[target]}
    if current is None or not rules or not zones or match.get("round") != 2:
        return {"full": None, "undersized": None, "first_attack_bonus": 0}
    result = {"full": current, "undersized": current, "first_attack_bonus": 0}
    for rule in rules:
        zone = zones.get(rule["id"]) or zones.get(rule["parent"])
        if zone is None:
            return {"full": None, "undersized": None, "first_attack_bonus": 0}
        remaining, capacity = zone.get("remaining"), zone.get("capacity")
        if remaining is None or capacity is None or not 0 <= remaining <= capacity:
            return {"full": None, "undersized": None, "first_attack_bonus": 0}
        if not remaining:
            continue
        teams = [team for team in zone["teams"] if team.get("state") != 3]
        if len(teams) > remaining:
            # Unknown individual states must not be treated as definite kills.
            teams = sorted(teams, key=lambda team: _team_ceiling(team, rule, False), reverse=True)[:remaining]
        teams += [{} for _ in range(remaining - len(teams))]
        capture = rule["points"]["completeZoneReward"] + capacity * rule["points"]["completeZoneRewardPerDefensiveSquad"]
        for key in ("full", "undersized"):
            result[key] += capture + sum(_team_ceiling(team, rule, key == "undersized") for team in teams)
    own = match[side]
    # The per-zone attack flags are present even behind a locked territory.
    flags = [zone.get("attacked") for zone in own]
    if flags and all(value == 0 for value in flags) and any(zone.get("remaining", 0) for zone in zones.values()):
        result["first_attack_bonus"] = max(rule["points"].get("firstAttackBonus", 0) for rule in rules)
    return result


def project(match):
    rules = arena_rules.zones(match["map"])
    return {side: _projection(match, side, rules) for side in ("home", "away")}
