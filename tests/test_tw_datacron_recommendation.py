import unittest
from unittest.mock import patch

import tw_def_engine as engine
from services import tw_def_notify as notify


class RecommendationTests(unittest.TestCase):
    def test_optional_does_not_read_or_reserve_inventory(self):
        self.assertEqual(engine.pick_datacron(None, "123", {"required": False}), (None, None))

    def test_recommendation_uses_only_configured_bonuses_once(self):
        row = (1, 22, "Рей", "light", "NONE", "NONE", "", "", "", "required", "здоровье, оборона", 3)
        catalog = {"seasons": {22: {"display_name": "Кодекс", "level3": [("light", "Бонусная защита")]}}}
        with patch.object(notify.database, "get_datacron_requirement_stats", return_value=[]):
            text = notify.describe_recommendation({"kind": "base", "req_id": 1}, {("base", 1): row}, catalog, 1)
        self.assertIn("здоровье, оборона", text)
        self.assertIn("3: Бонусная защита", text)
        self.assertNotIn("6:", text)
        self.assertNotIn("9:", text)
        self.assertNotIn("ур. 9", text)
        self.assertNotIn("×3", text)

    def test_required_still_checks_inventory(self):
        roster = engine.Roster.__new__(engine.Roster)
        roster.dc_reqs = {("focused", 1): (1, 22, "", "shin", 9)}
        roster.datacrons = {"123": [{"id": "owned", "setId": 22, "focused": True,
                                     "templateId": "datacron_set_22_focused_shin", "affix": [{}] * 9}]}
        dc = {"kind": "focused", "req_id": 1, "required": True}
        self.assertEqual(engine.pick_datacron(roster, "123", dc), ("ok", "owned"))
        self.assertEqual(engine.pick_datacron(roster, "123", dc, {"owned"}), ("used", None))
        self.assertEqual(engine.pick_datacron(roster, "456", dc), ("unknown", None))
