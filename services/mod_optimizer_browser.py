"""Shared Python calculation in a browser worker; server validates allocations only."""
from collections import Counter, defaultdict
import inspect
import json
from pathlib import Path

import stat_engine
from services import mod_optimizer as legacy, mod_optimizer_v2 as engine


def python_source():
    """Export only pure calculation code, with the installed StatCalc implementation.

    No database, EA client, keys, cookies or game transport is included.
    """
    from swgoh_comlink.StatCalc.calculator import StatCalc
    lines = ["from __future__ import annotations", "import copy, itertools, math, random, time, json, logging, urllib.request",
             "from typing import Any", "from collections import Counter, defaultdict", "from types import SimpleNamespace",
             "import pulp", "_CANONICAL_STATS = " + repr(StatCalc.STATS_NAME_MAP), inspect.getsource(StatCalc)]
    for name in ("PERCENT_STATS", "MOD_SET_PIECE_COUNT", "MOD_SET_NAMES_RU", "UNIT_STAT_RU"):
        lines.append(name + " = " + repr(getattr(stat_engine, name)))
    for name in ("project_unit_relic", "get_current_relic_level", "calc_final_stats"):
        lines.append(inspect.getsource(getattr(stat_engine, name)))
    lines.append("stat_engine = SimpleNamespace(**{k: v for k, v in globals().copy().items() if k in "
                 "('project_unit_relic', 'get_current_relic_level', 'calc_final_stats', 'MOD_SET_PIECE_COUNT', 'MOD_SET_NAMES_RU', 'UNIT_STAT_RU')})")
    for name in ("TWO_PIECE_SETS", "FOUR_PIECE_SETS"):
        lines.append(name + " = " + repr(getattr(legacy, name)))
    for name in ("_to_equipped_stat_mod", "_target_multisets", "_compare"):
        lines.append(inspect.getsource(getattr(legacy, name)))
    lines.append("legacy = SimpleNamespace(_to_equipped_stat_mod=_to_equipped_stat_mod, _target_multisets=_target_multisets, _compare=_compare)")
    for name in ("SLOTS", "SLOT_LABELS", "BUILD_LIMIT", "SEARCH_SECONDS"):
        lines.append(name + " = " + repr(getattr(engine, name)))
    for name in ("final_stats", "validate_inventory", "_ids", "_scheme", "evaluation_unit", "_mod_value",
                 "evaluate", "_rank", "search", "solve", "calculate", "render_allocation"):
        lines.append(inspect.getsource(getattr(engine, name)))
    return "\n\n".join(lines)


def calculator_data(calc):
    return {key: getattr(calc, attr) for key, attr in (
        ("unitData", "_unit_data"), ("modSetData", "_mod_set_data"), ("gearData", "_gear_data"),
        ("crTables", "_cr_tables"), ("gpTables", "_gp_tables"), ("relicData", "_relic_data"))}


def assess(calc, prepared, allocation):
    """Accept IDs only, reconstruct and recompute every value from trusted inputs."""
    members, rows, units, tiers = (prepared[k] for k in ("members", "rows", "units", "tiers"))
    mods, locked = prepared["mods"], set(prepared["locked"])
    if not isinstance(allocation, dict) or set(allocation) != set(rows) & set(units):
        raise ValueError("Состав результата не совпадает с выбранными плейтами.")
    indexed = {m["id"]: m for m in mods}
    worn, chosen, used = defaultdict(list), {}, set()
    for mod in mods:
        if mod.get("equipped_to"):
            worn[mod["equipped_to"]].append(mod)
    for base, ids in allocation.items():
        if not isinstance(ids, list) or len(ids) > 6 or any(not isinstance(mid, str) for mid in ids):
            raise ValueError("Некорректный набор модулей.")
        if len(ids) != len(set(ids)) or used.intersection(ids) or any(mid not in indexed for mid in ids):
            raise ValueError("Модуль отсутствует в инвентаре или использован повторно.")
        selected = [indexed[mid] for mid in ids]
        if len({m["slot_id"] for m in selected}) != len(selected):
            raise ValueError("В одном слоте выбраны два модуля.")
        old_ids = {m["id"] for m in worn[base]}
        if base in locked and set(ids) != old_ids:
            raise ValueError("Изменены модули заблокированного персонажа.")
        if any((m.get("equipped_to") in locked or m.get("locked")) and m.get("equipped_to") != base for m in selected):
            raise ValueError("Перенос заблокированного модуля запрещён.")
        if any(m.get("locked") and m["id"] not in ids for m in worn[base]):
            raise ValueError("Снят заблокированный модуль.")
        if units[base].get("currentLevel", 85) < 50 and set(ids) != old_ids:
            raise ValueError("Персонаж ещё не может менять модули.")
        if any(m["rarity"] == 6 for m in selected) and units[base].get("currentTier", 0) < 12:
            raise ValueError("Для модулей 6★ требуется G12.")
        used.update(ids)
        modeled = engine.evaluation_unit(units[base], rows[base], selected, prepared["relic_mode"])
        chosen[base] = {"mods": selected, "mod_ids": frozenset(ids), "evaluation_unit": modeled,
                        "final_stats": engine.final_stats(calc, modeled, selected)}
    return engine.render_allocation(calc, members, rows, units, tiers, mods, prepared["names"], chosen, locked=locked)


def runtime_ready():
    root = Path(__file__).resolve().parents[1] / "web/static/vendor/optimizer"
    return all((root / name).is_file() for name in ("pyodide.js", "pyodide.asm.wasm", "highs.js", "highs.wasm", "pulp.zip"))
