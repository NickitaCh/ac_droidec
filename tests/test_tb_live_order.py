import unittest
from unittest.mock import patch

from services import game_account as ga, tb_live, tb_live_order


def message(number, *rows):
    return number, 2, ga.pack(rows)


def text(number, value):
    return number, 2, value.encode()


class TBOrderComparisonTests(unittest.TestCase):
    def test_parse_unit_definition_and_reverse_game_operation_number(self):
        zone = "tb3_mixed_phase05_conflict01"
        operation = message(7, text(1, "tb3-platoon-6"), message(2, message(
            2, text(1, "VADER:SEVEN_STAR"), text(5, "donor"),
        )))
        battle = message(1, text(1, "tb"), (9, 0, 6), (11, 0, 2000000000000),
                         message(5, message(5, text(1, zone), (2, 0, 3))),
                         message(7, message(6, text(1, zone + "_recon01")), operation))
        ops = tb_live.parse(ga.pack([battle]))["zones"][zone]["operations"]
        self.assertEqual(tb_live_order.operation_number(ops[0]["id"]), 1)
        self.assertEqual(ops[0]["slots"], [{"unit": "VADER", "donor": "donor", "squad_id": ""}])

    def test_duplicate_copies_match_actual_player_before_slot_position(self):
        slots = [{"unit": "VADER", "donor": "b"}, {"unit": "VADER", "donor": "a"}]
        planned = {0: {"ally_code": "1"}, 1: {"ally_code": "2"}}
        roster = {"a": ("1", "One"), "b": ("2", "Two")}
        rows = tb_live_order.compare(["VADER", "VADER"], slots, planned, roster)
        self.assertEqual([row[-1] for row in rows], ["done", "done"])

    def test_competing_assignments_do_not_claim_the_same_free_copy(self):
        slots = [{"unit": "VADER", "donor": ""}, {"unit": "VADER", "donor": "other"}]
        planned = {0: {"ally_code": "1"}, 1: {"ally_code": "2"}}
        rows = tb_live_order.compare(["VADER", "VADER"], slots, planned, {"other": ("3", "Other")})
        self.assertEqual([row[-1] for row in rows], ["shared", "shared"])
        self.assertIsNone(tb_live_order.compare(["LUKE", "VADER"], slots, planned, {}))

    def test_capture_only_assignments_due_on_this_stage_or_earlier(self):
        module = tb_live_order
        state = {"phase": 5, "zones": {"tb3_mixed_phase04_conflict01": {
            "status": tb_live.OPEN_ZONE, "operations": [{"id": "tb3-platoon-6", "slots": [
                {"unit": "VADER", "donor": "placed"}, {"unit": "LUKE", "donor": ""},
            ]}],
        }}}
        with (
            patch.object(module.database, "get_guild_config", return_value={"tb_active_plan_id": 3}),
            patch.object(module.database, "get_tb_saved_plan", return_value={"id": 3, "guild_id": 1, "name": "Ордер"}),
            patch.object(module.database, "get_tb_platoon_assignments", return_value={
                ("Planet", 1, 0): {"ally_code": "1", "round_num": 4},
                ("Planet", 1, 1): {"ally_code": "2", "round_num": 6},
            }),
            patch.object(module.database, "get_roster_by_player_id", return_value={"placed": ("1", "One")}),
            patch.object(module.database, "resolve_unit_display_names", return_value={"Vader": "VADER", "Luke": "LUKE"}),
            patch.object(module.tb_forecast, "planet_of", return_value=(4, "Planet", False)),
            patch.object(module.tb_platoon_data, "ROTE_PLATOON_SUGGESTIONS", {("Planet", 1): ["Vader", "Luke"]}),
        ):
            module.capture(1, state)
        operation = state["zones"]["tb3_mixed_phase04_conflict01"]["operations"][0]
        self.assertEqual(operation["order"], [(0, "VADER", "1", "done")])
        self.assertEqual(state["order_name"], "Ордер")


if __name__ == "__main__":
    unittest.main()
