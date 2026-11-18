"""Adapt private inventory modules to the existing mod search representation."""
from collections import defaultdict
import sqlite3

import database
from services import game_account as ga, mod_optimizer, mod_search
from stat_engine import MOD_SET_IDS, MOD_SET_NAMES_RU, UNIT_STAT_RU

CSV_COLUMNS = ("id", "level", "set", "slot", "locked", "equipped_to", "rarity_dots", "tier_pips",
               "primary_stat", "primary_value", *(column for n in range(1, 5)
               for column in (f"secondary{n}", f"secondary{n}_value", f"secondary{n}_rolls")))
CSV_STATS = {stat_id: name for name, ids in mod_optimizer.STAT_NAME_TO_IDS.items()
             for stat_id in ids if stat_id is not None}
CSV_SLOTS = dict(zip(mod_search.SLOT_ID_TO_KEY.values(), mod_optimizer.SHAPES))


def _stat(raw):
    try:
        rows = ga.fields(ga._one(ga.fields(raw), 2, 2))
    except ga.AccountError:
        return {}
    values = {tag: value for tag, wire, value in rows if wire == 0}
    if 3 not in values or (5 not in values and 6 not in values):
        return {}
    value = values.get(6, values.get(5, 0))
    if value >= 1 << 63:
        value -= 1 << 64
    if 6 not in values:
        value *= 10_000
    return {"stat": {"unitStatId": values[3], "unscaledDecimalValue": value}}


def _module(raw):
    rows = ga.fields(raw)
    values = {tag: value for tag, wire, value in rows if wire == 0}
    primary = []
    secondary = []
    secondary_rolls = []
    for tag, wire, value in rows:
        if wire == 2 and tag in (12, 13):
            stat = _stat(value)
            if tag == 12:
                primary.append(stat)
            elif stat:
                secondary.append(stat)
                secondary_rolls.append(next((v for n, w, v in ga.fields(value) if (n, w) == (4, 0)), 0))
    mod = mod_search.decode_mod({
        "definitionId": ga._one(rows, 2, 2).decode(),
        "level": values.get(4), "tier": values.get(5),
        "primaryStat": primary[0] if primary else {},
        "secondaryStat": secondary,
    })
    if mod:
        mod["id"] = next((value.decode() for tag, wire, value in rows if (tag, wire) == (1, 2)), "")
        mod["locked"] = bool(values.get(11))
        mod["secondary_rolls"] = secondary_rolls
    return mod


def _csv_row(mod, equipped_to):
    stats = [mod["primary"], *mod["secondaries"]]
    if (not mod["id"] or not mod["slot_key"] or str(mod["set_id"]) not in MOD_SET_IDS
            or len(stats) > 5 or any(not stat or stat["stat_id"] not in CSV_STATS for stat in stats)):
        return None
    slot = CSV_SLOTS[mod["slot_key"]]
    row = [mod["id"], mod["level"], MOD_SET_IDS[str(mod["set_id"])], slot, str(mod["locked"]).lower(),
           equipped_to, mod["rarity"], mod["tier"]]
    for index, stat in enumerate(stats):
        row.extend([CSV_STATS[stat["stat_id"]], f"{stat['value']:.8f}".rstrip("0").rstrip(".")
                    + ("%" if stat["is_percent"] else "")])
        if index:
            row.append(mod["secondary_rolls"][index - 1])
    return row + [""] * (len(CSV_COLUMNS) - len(row))


def read_inventory(fields):
    equipped = defaultdict(list)
    free = []
    for tag, wire, raw in fields:
        if wire != 2:
            continue
        if tag == 13:
            free.append(raw)
        elif tag == 1:
            unit = ga.fields(raw)
            base_id = next((value.decode("utf-8", "replace").split(":")[0]
                            for number, kind, value in unit if (number, kind) == (2, 2)), "")
            equipped[base_id].extend(value for number, kind, value in unit if (number, kind) == (21, 2))

    try:
        names = database.get_game_unit_names([key for key, rows in equipped.items() if key and rows])
    except sqlite3.OperationalError as error:
        if "no such table: game_units" not in str(error):
            raise
        names = {}
    modules = []
    unreadable = 0
    sources = [("Свободен", False, "", free)]
    sources.extend((names.get(key) or key or "Персонаж", True, key, rows) for key, rows in equipped.items())
    for owner, assigned, base_id, rows in sources:
        for raw in rows:
            try:
                mod = _module(raw)
            except ValueError:
                mod = None
            if not mod:
                unreadable += 1
                continue
            mod.update(owner=owner, assigned=assigned,
                       set_label=MOD_SET_NAMES_RU.get(mod["set_id"], str(mod["set_id"])),
                       csv_row=_csv_row(mod, base_id))
            modules.append(mod)
    return {
        "modules": modules, "unreadable": unreadable, "csv_columns": CSV_COLUMNS,
        "sets": sorted({(mod["set_id"], mod["set_label"]) for mod in modules}),
        "slots": mod_search.SLOT_CHOICES,
        "stats": [(key, UNIT_STAT_RU.get(key, name) + (" (%)" if percent and key in UNIT_STAT_RU else ""))
                  for key, name, percent in mod_search.STAT_CHOICES],
    }
