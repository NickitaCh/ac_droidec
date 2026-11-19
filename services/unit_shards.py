"""Personal shard progress, using the game's creation and promotion recipes."""
import asyncio
import json
import time

import database
from services import game_account
from services.units_sync import UNIT_DEFINITIONS_FLAG, _rarity
from services.equipment_sync import RECIPE_DEFINITIONS_FLAG

CATALOG_KEY = "unit_shard_recipes"
_catalog_lock = asyncio.Lock()


def build_catalog(units, recipes):
    recipes = {r["id"]: r for r in recipes if r.get("id")}
    by_id = {u["id"]: u for u in units if u.get("id")}
    result = {}
    for unit in units:
        base_id, stars = unit.get("baseId"), _rarity(unit)
        if (not base_id or not 1 <= stars <= 7 or not unit.get("obtainable")
                or unit.get("obtainableTime") != "0"):
            continue
        entry = result.setdefault(base_id, {"promotion": {}})
        for field, key in (("creationRecipeReference", "creation"),
                           ("promotionRecipeReference", "promotion")):
            recipe = recipes.get(unit.get(field), {})
            parts = [p for p in recipe.get("ingredients", [])
                     if p.get("id") in ("UNIT_SHARD_MATERIAL", f"unitshard_{base_id}")]
            if len(parts) != 1:
                continue
            count = parts[0].get("minQuantity", 0)
            if not isinstance(count, int) or count <= 0 or parts[0].get("maxQuantity", count) != count:
                continue
            if key == "creation":
                target = (recipe.get("result") or {}).get("id")
                target_unit = by_id.get(target)
                if target_unit and 1 <= _rarity(target_unit) <= 7:
                    entry[key] = {"stars": _rarity(target_unit), "count": count}
            elif stars < 7:
                # A unit's promotion reference is the recipe FROM its current rarity.
                entry[key][str(stars)] = {"stars": stars + 1, "count": count}
    return result


async def catalog(comlink):
    async with _catalog_lock:
        raw = await asyncio.to_thread(database.get_bot_state, CATALOG_KEY, guild_id=0)
        cached = json.loads(raw) if raw else None
        if cached and time.time() - cached["updated_at"] < 86400:
            return cached["units"]
        units = await asyncio.to_thread(comlink.get_game_data, items=str(UNIT_DEFINITIONS_FLAG))
        recipes = await asyncio.to_thread(comlink.get_game_data, items=str(RECIPE_DEFINITIONS_FLAG))
        data = await asyncio.to_thread(build_catalog, units.get("units") or [], recipes.get("recipe") or [])
        if not data or not any(e.get("creation") for e in data.values()):
            raise game_account.AccountError("Не удалось получить рецепты звёзд из игры. Попробуйте позже.")
        await asyncio.to_thread(database.set_bot_state, CATALOG_KEY,
                                json.dumps({"updated_at": time.time(), "units": data}), guild_id=0)
        return data


def add_progress(report, snapshot, recipes):
    farm = snapshot.get("farm")
    if not farm or "stock" not in farm or "stars" not in farm:
        raise game_account.AccountError("В ответе игры нет данных об осколках.")
    for unit in report["units"]:
        base_id = unit["base_id"]
        stars = farm["stars"].get(base_id, 0)
        if stars >= 7:
            unit["shards"] = {"max": True}
            continue
        available = farm["stock"].get(f"unitshard_{base_id}", 0)
        entry = recipes.get(base_id, {})
        recipe = entry.get("creation") if stars == 0 else entry.get("promotion", {}).get(str(stars))
        unit["shards"] = {"available": available, "target": recipe["stars"] if recipe else None,
                          "needed": recipe["count"] if recipe else None,
                          "remaining": max(0, recipe["count"] - available) if recipe else None,
                          "unlock": stars == 0}


def private_roster(snapshot):
    farm = snapshot.get("farm")
    if not farm or "stars" not in farm or "units" not in farm or "stock" not in farm:
        raise game_account.AccountError("В ответе игры нет данных о персонажах.")
    result = {}
    for base_id, stars in farm["stars"].items():
        if not 1 <= stars <= 7:
            continue
        _, gear, relic, _ = farm["units"].get(base_id, (base_id, 1, 0, {}))
        result[base_id] = {"currentRarity": stars, "currentTier": gear,
                           "relic": {"currentTier": relic + 2 if gear >= 13 else 0}}
    return result
