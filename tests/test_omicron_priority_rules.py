import json
import unittest
from unittest.mock import patch

import guild_omicron_rules
from services.omicron_priority import _missing_for_units


class OmicronPriorityRulesTest(unittest.TestCase):
    def test_alternative_squads_with_required_named_units_and_faction_slots(self):
        state = {
            "unit_factions": {"unaligned_force_user": {
                "name": "Нейтральный адепт Силы",
                "units": ["LIGHT", "DARK", "STARKILLER"],
            }},
            "unit_alignments": {"LIGHT": 2, "DARK": 3, "STARKILLER": 3},
        }
        with (
            patch("database.get_all_unit_omicron_skills", return_value={"JUHANI": ["skill"]}),
            patch("database.get_skill_display_info", return_value={}),
            patch("database.resolve_unit_display_names", side_effect=lambda names: {n: n for n in names}),
            patch("database.get_bot_state", side_effect=lambda key, **kwargs: json.dumps(state[key])),
        ):
            rules, errors = guild_omicron_rules.parse_rules(
                "omicron [JUHANI] tier 1\n"
                "omicron [JUHANI] require unit [STARKILLER]\n"
                "omicron [JUHANI] require unit [STRANGER] alternative 1\n"
                "omicron [JUHANI] require unit [MAUL] alternative 1\n"
                "omicron [JUHANI] require unit [TRAYA] alternative 2\n"
                "omicron [JUHANI] require faction [unaligned_force_user] side light alternative 2\n"
                "omicron [JUHANI] require faction [unaligned_force_user] side dark alternative 2",
                1,
            )
        self.assertEqual(errors, [])
        self.assertEqual(rules.tiers, {"skill": 1})
        catalog = {
            "priority": [("skill", 0)],
            "skill_owner": {"skill": "JUHANI"},
            "thresholds": {"skill": (None, 8)},
            "display": {},
            "unit_names": {},
        }
        for roster, expected in [
            (["STARKILLER", "STRANGER", "MAUL"], True),
            (["STARKILLER", "TRAYA", "LIGHT", "DARK"], True),
            (["STARKILLER", "TRAYA", "LIGHT"], False),
            (["STARKILLER", "STRANGER", "LIGHT", "DARK"], False),
            (["STRANGER", "MAUL"], False),
        ]:
            with self.subTest(roster=roster):
                units = {bid: {} for bid in ["JUHANI", *roster]}
                self.assertEqual(bool(_missing_for_units(units, catalog, rules)), expected)
