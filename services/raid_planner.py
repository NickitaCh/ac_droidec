"""Подбор пяти непересекающихся попыток для Droid Destruction."""

import json
import logging
from itertools import combinations
from threading import Lock
from time import monotonic
from urllib.request import Request, urlopen

from services.journey_guide import unit_progress

SOURCE = "https://raid-planner.fyi"
CATALOG_URL = f"{SOURCE}/data/droid-destruction.json"
logger = logging.getLogger(__name__)
_catalog_lock = Lock()
_catalog = None
_next_fetch = 0


def load_catalog():
    global _catalog, _next_fetch
    with _catalog_lock:
        if monotonic() < _next_fetch:
            return _catalog
        try:
            request = Request(CATALOG_URL, headers={"User-Agent": "AC-Droidec/1.0", "Accept": "application/json"})
            with urlopen(request, timeout=5) as response:
                data = json.loads(response.read(2_000_000))
            if not isinstance(data, dict) or not {"raid", "squads", "solos", "names"} <= data.keys():
                raise ValueError("Unexpected raid catalog")
            # Validate planning fields before replacing the working copy.
            build_plan({}, data)
        except (OSError, ValueError, KeyError, TypeError) as error:
            logger.warning("Raid Planner catalog unavailable: %s", error)
            _next_fetch = monotonic() + 60
        else:
            _catalog = data
            _next_fetch = monotonic() + 3600
        return _catalog


def turn_order(run, catalog):
    """A catalogue may provide several turn orders for different tiers/lineups."""
    variants = catalog.get("turn_order", {}).get(run["id"], [])
    if isinstance(variants, dict):
        variants = [variants]
    for variant in variants:
        tiers = variant.get("tiers")
        if tiers and not tiers[0] <= run["tier"] <= tiers[1]:
            continue
        if not set(variant.get("only") or []).issubset(run["units"]):
            continue
        return [unit for unit in variant.get("units", []) if unit in run["units"]]
    return []


def comlink_roster(units):
    return {
        base_id: {
            "gear": int(unit_progress(unit, "gear") or 0),
            "relic": int(unit_progress(unit, "relic")),
            "rarity": int(unit_progress(unit, "stars") or 0),
        }
        for base_id, unit in units.items()
    }


def build_plan(roster, catalog):
    raid = catalog["raid"]
    tiers = raid["tiers"]
    points = {tier["tier"]: tier["points"] for tier in tiers}

    def unit_tier(unit):
        return max((tier["tier"] for tier in tiers if (
            unit["gear"] >= 13 and unit["relic"] >= tier["min_relic"]
            if tier["min_relic"] is not None else unit["gear"] >= tier["min_gear"]
        )), default=0)

    def option(team, name, units, tier, cap=None, blockers=()):
        return {
            "id": team.get("id", f"solo-{units[0]}"),
            "name": name,
            "units": units,
            "tier": tier,
            "hi": cap["hi"] if cap else points[tier],
            "lo": cap["lo"] if cap else points.get(tier - 1, 0),
            "blockers": blockers,
            "team": team,
        }

    options = []
    for team in catalog["squads"]:
        pool = team.get("pool")
        lineups = combinations(pool["units"], pool["pick"]) if pool else team["lineups"]
        shown = max((s["tier"] for s in team.get("sources", []) if type(s.get("tier")) is int), default=-1)
        caps = team.get("cap") or []
        if isinstance(caps, dict):
            caps = [caps]
        for index, lineup in enumerate(lineups):
            units = (team["lead"], *lineup)
            if any(u not in roster or roster[u]["rarity"] < raid.get("min_rarity", 0) for u in units):
                continue
            ready = min(unit_tier(roster[u]) for u in units)
            tier = min(ready, shown) if shown >= 0 else ready
            cap = next((c for c in caps if tier >= c["from_tier"] and (
                not c.get("lineups") or index in c["lineups"]
            )), None)
            next_tier = next((t for t in tiers if t["tier"] == tier + 1), None)
            blockers = tuple(u for u in units if unit_tier(roster[u]) == ready) if next_tier and tier == ready and shown != tier else ()
            options.append(option(team, team["name"], units, tier, cap, blockers))
    for solo in catalog["solos"]:
        base_id = solo["unit"]
        unit = roster.get(base_id)
        if unit and unit["rarity"] >= solo["min_rarity"] and unit["gear"] >= 13 and unit["relic"] >= solo["min_relic"]:
            options.append(option(solo, f"{catalog['names'].get(base_id, base_id)} solo", (base_id,), solo["max_tier"]))

    def weight(run):
        return run["hi"] + (run["lo"] or run["hi"])

    options.sort(key=weight, reverse=True)
    weights = [weight(run) for run in options]
    best_score, best = 0, []

    def search(start, used, names, chosen, score):
        nonlocal best_score, best
        if score > best_score:
            best_score, best = score, chosen.copy()
        remaining = raid["attempts"] - len(chosen)
        if not remaining or score + sum(weights[start:start + remaining]) <= best_score:
            return
        for index in range(start, len(options)):
            run = options[index]
            if run["name"] in names or not used.isdisjoint(run["units"]):
                continue
            chosen.append(run)
            search(index + 1, used.union(run["units"]), names | {run["name"]}, chosen, score + weights[index])
            chosen.pop()

    search(0, set(), set(), [], 0)
    return {
        "runs": sorted(best, key=lambda run: run["hi"], reverse=True),
        "hi": sum(run["hi"] for run in best),
        "lo": sum(run["lo"] for run in best),
    }
