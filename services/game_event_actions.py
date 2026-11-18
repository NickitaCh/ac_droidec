"""Prepare one confirmed game action from a fresh event snapshot."""

from dataclasses import dataclass
import time

from services import game_account, game_event_live, tb_live


@dataclass(frozen=True)
class EventAction:
    kind: str
    event_id: str
    phase: int
    zone_id: str
    unit_ids: tuple[str, ...]
    datacron_id: str = ""
    platoon_id: str = ""
    squad_id: str = ""


def _current(event_id, phase, ends_at, expected_event, expected_phase):
    if (not event_id or not ends_at or event_id != expected_event
            or phase != expected_phase or game_event_live.epoch(ends_at) <= time.time()):
        raise game_account.AccountError("Событие или этап изменились. Обновите предпросмотр.")


def _units(base_ids, available):
    unit_ids = tuple(available.get(base_id) for base_id in base_ids)
    if not unit_ids or not all(unit_ids) or len(unit_ids) != len(set(unit_ids)):
        raise game_account.AccountError("Состав содержит недоступных или повторяющихся юнитов.")
    return unit_ids


def tw_defense(state, expected_event, expected_round, assignment, ally_code, zone_ids,
               available_units, available_datacrons, selected_datacron=""):
    """Resolve the delivered assignment against current game IDs and inventory."""
    event, phase = state["event_id"], state["round"]
    _current(event, phase, state["end"], expected_event, expected_round)
    if str(assignment["ally_code"]) != str(ally_code):
        raise game_account.AccountError("Отряд назначен другому аккаунту.")
    zone_id = zone_ids.get(assignment["zone"])
    zone = next((row for row in state["own"] if row["id"] == zone_id), None)
    if (not zone or zone["status"] != tb_live.OPEN_ZONE or zone["deployed"] is None
            or not zone["capacity"] or zone["deployed"] >= zone["capacity"]):
        raise game_account.AccountError("В зоне нельзя разместить отряд.")
    assigned = (assignment.get("datacron_id") or "").split(",")
    assigned = [identifier for identifier in assigned if identifier]
    if len(assigned) == 1 and not selected_datacron:
        selected_datacron = assigned[0]
    if ((assigned and selected_datacron not in assigned)
            or (selected_datacron and selected_datacron not in available_datacrons)):
        raise game_account.AccountError("Выберите доступный датакрон для назначенного отряда.")
    return EventAction("tw_defense", event, phase, zone_id,
                       _units(assignment["units"], available_units), selected_datacron)


def _tb_zone(state, expected_event, expected_phase, zone_key):
    _current(state["event"], state["phase"], state["end_ms"], expected_event, expected_phase)
    zone = state["zones"].get(zone_key)
    if not zone or zone["status"] != tb_live.OPEN_ZONE or not zone.get("id"):
        raise game_account.AccountError("Планета закрыта или отсутствует в текущем этапе.")
    return zone


def tb_platoon(state, expected_event, expected_phase, zone_key, platoon_id, unit,
               ally_code, available_units):
    zone = _tb_zone(state, expected_event, expected_phase, zone_key)
    for operation in zone["operations"]:
        if operation["id"] != platoon_id:
            continue
        if not any(base_id == unit and owner == str(ally_code) and status == "empty"
                   for _, base_id, owner, status in operation.get("order") or ()):
            break
        candidates = {slot["squad_id"] for slot in operation["slots"]
                      if slot["unit"] == unit and not slot["donor"] and slot.get("squad_id")}
        if len(candidates) == 1:
            return EventAction(
                "tb_platoon", state["event"], state["phase"], zone.get("recon_id") or zone["id"],
                _units([unit], available_units), platoon_id=platoon_id, squad_id=candidates.pop(),
            )
        break
    raise game_account.AccountError("Свободный слот для этого назначения не определён однозначно.")


def tb_deploy(state, expected_event, expected_phase, zone_key, unit_ids, available_unit_ids):
    zone = _tb_zone(state, expected_event, expected_phase, zone_key)
    unit_ids = tuple(unit_ids)
    if not unit_ids or len(set(unit_ids)) != len(unit_ids) or set(unit_ids) - available_unit_ids:
        raise game_account.AccountError("Выберите доступных для заливки юнитов.")
    return EventAction("tb_deploy", state["event"], state["phase"], zone["id"], unit_ids)
