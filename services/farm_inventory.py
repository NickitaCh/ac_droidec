"""Shared inventory report for the web and Discord farm calculators."""

from collections import Counter

import database
from services import game_account, game_gear_planner, journey_guide


def build_report(snapshot, targets=None, *, guide_id=None, requirements=None, include_reward=False):
    farm = snapshot["farm"]
    if not farm:
        raise game_account.AccountError("В ответе игры нет данных для расчёта прокачки.")
    units = dict(farm["units"])
    blockers = []
    if guide_id:
        grouped = journey_guide.group_by_unit(requirements)
        names = database.get_game_unit_names([guide_id, *grouped])
        targets = []
        for uid, all_targets in grouped.items():
            name = names.get(uid, uid)
            mode, level = all_targets[0]
            stars = farm.get("stars", {})
            for tier, value in all_targets:
                if tier == "stars" and stars.get(uid, 0) < int(value):
                    blockers.append(f"{name} · {stars.get(uid, 0)} из {value}★")
            if mode != "stars":
                if uid in units:
                    targets.append((uid, mode, int(level)))
                else:
                    blockers.append(f"{name} · нет данных о прокачке до {journey_guide.short_label(mode, level)}")
        if include_reward:
            units.setdefault(guide_id, (guide_id, 1, 0, Counter()))
            if guide_id not in grouped:
                targets.append((guide_id, "gear", 13))
    else:
        if any(uid not in units for uid, _, _ in targets):
            raise game_account.AccountError("Персонаж не найден среди доступных данных инвентаря.")
        names = database.get_game_unit_names([uid for uid, _, _ in targets])
    rows, _, relic_ids = game_gear_planner.calculate_report(
        [(units[uid], mode, level) for uid, mode, level in targets], farm["stock"],
    )
    sections = {"gear": [], "relic": []}
    for item, name, needed, stock, missing, craft in rows:
        if missing or craft:
            sections["relic" if item in relic_ids else "gear"].append(
                (name, needed, stock, missing, craft, database.get_equipment_locations(item) if missing else [], item),
            )
    deficit_names = {row[0] for section in sections.values() for row in section}
    return {
        "guide": names.get(guide_id, guide_id) if guide_id else None,
        "goals": [f"{names.get(uid, uid)} · G{units[uid][1]}"
                  f"{f' R{units[uid][2]}' if units[uid][1] == 13 else ''} → {journey_guide.short_label(mode, level)}"
                  for uid, mode, level in targets],
        "sections": sections, "blockers": blockers,
        "show_relic": any(mode == "relic" for _, mode, _ in targets),
        "unit_target": (targets[0][1], targets[0][2]) if not guide_id and len(targets) == 1 else None,
        "energy": snapshot.get("energy", []),
        "offers": [offer for offer in snapshot.get("shipments", [])
                   if offer["item"].get("Название") in deficit_names and not offer["bought"]],
    }
