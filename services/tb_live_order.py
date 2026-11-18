"""Compare TB donations to the active order without relying on game row order."""
from collections import Counter
import re

import database
from services import tb_forecast, tb_live
import tb_platoon_data

_OPERATION = re.compile(r"tb3-platoon-([1-6])")
_LABELS = {
    "done": "Поставил по ордеру", "empty": "Ждёт размещения",
    "other": "Оставшиеся копии заняты другими игроками", "unknown": "Не удалось определить игрока",
    "shared": "Свободных копий меньше, чем оставшихся назначений",
}


def operation_number(operation_id):
    match = _OPERATION.fullmatch(operation_id)
    # The game numbers these definitions in reverse of the order planner.
    return 7 - int(match[1]) if match else None


def compare(expected, slots, assignments, roster):
    """Match donors to all planned copies of the same unit before allocating empty slots."""
    if not expected or not all(expected) or Counter(expected) != Counter(slot["unit"] for slot in slots):
        return None
    donors = Counter()
    empty = Counter()
    unknown = Counter()
    for slot in slots:
        unit, donor = slot["unit"], slot["donor"]
        if not donor:
            empty[unit] += 1
        elif donor not in roster:
            unknown[unit] += 1
        else:
            donors[(unit, str(roster[donor][0]))] += 1

    report = []
    pending = []
    for index, unit in enumerate(expected):
        assignment = assignments.get(index)
        if not assignment:
            continue
        code = str(assignment["ally_code"])
        if donors[(unit, code)]:
            donors[(unit, code)] -= 1
            report.append((index, unit, code, "done"))
        else:
            pending.append((index, unit, code))

    # When both an unplanned donation and a free copy exist, the assignment
    # can still be fulfilled. Only report a replacement once no copy is free.
    pending_counts = Counter(unit for _, unit, _ in pending)
    for index, unit, code in pending:
        if unknown[unit]:
            status = "unknown"
        elif empty[unit] >= pending_counts[unit]:
            status = "empty"
        elif empty[unit]:
            status = "shared"
        else:
            status = "other"
        report.append((index, unit, code, status))
    return sorted(report)


def capture(guild_id, state):
    settings = database.get_guild_config(guild_id) or {}
    plan_id = settings.get("tb_active_plan_id")
    if not plan_id:
        return
    plan = database.get_tb_saved_plan(int(plan_id))
    if not plan or plan["guild_id"] != guild_id:
        return

    assignments = database.get_tb_platoon_assignments(guild_id, plan["id"])
    roster = database.get_roster_by_player_id(guild_id)
    operations = []
    for key, zone in state["zones"].items():
        if zone["status"] != tb_live.OPEN_ZONE or not key.startswith("tb3_mixed_"):
            continue
        where = tb_forecast.planet_of(key)
        if not where:
            continue
        _, planet, _ = where
        for operation in zone["operations"]:
            number = operation_number(operation["id"])
            if number is not None:
                unit_names = tb_platoon_data.ROTE_PLATOON_SUGGESTIONS.get((planet, number), ())
                operations.append((planet, number, operation, unit_names))

    aliases = database.resolve_unit_display_names(sorted({name for *_, names in operations for name in names}))
    for planet, number, operation, unit_names in operations:
        planned = {}
        for index in range(len(unit_names)):
            assignment = assignments.get((planet, number, index))
            if assignment and assignment.get("round_num") and assignment["round_num"] <= state["phase"]:
                planned[index] = assignment
        expected = [aliases.get(name) for name in unit_names]
        operation["order"] = compare(expected, operation["slots"], planned, roster)
    state["order_name"] = plan["name"]


def display(rows, unit_names, player_names):
    return [(unit_names.get(unit, unit), player_names.get(code, "Игрок вне ростера"), _LABELS[status])
            for _, unit, code, status in rows]

def personal_preview(state, ally_code, owned):
    """Present outstanding assignments from the verified snapshot only."""
    output = []
    code = str(ally_code)
    for key, zone in state["zones"].items():
        if zone["status"] != tb_live.OPEN_ZONE:
            continue
        planet = tb_forecast.planet_of(key) if key.startswith("tb3_mixed_") else None
        for operation in zone["operations"]:
            for _, unit, owner, status in operation.get("order") or ():
                if owner != code or status == "done":
                    continue
                in_roster = unit in owned if owned is not None else None
                output.append({
                    "planet": planet[1] if planet else key, "operation": operation_number(operation["id"]),
                    "unit": unit, "status": _LABELS[status], "in_roster": in_roster,
                })
    return sorted(output, key=lambda row: (row["planet"], row["operation"] or 99, row["unit"]))
