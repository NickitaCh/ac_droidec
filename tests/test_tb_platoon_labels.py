import unittest
from unittest.mock import AsyncMock, patch

import tb_platoon_notify as notify


class PlatoonLabelsTests(unittest.IsolatedAsyncioTestCase):
    async def test_localized_assignment_keeps_exact_round_and_unknown_name(self):
        entries = [{"round": 6, "planet": "Lothal"}]
        assignments = {("Lothal", 1, i): {"round_num": r, "ally_code": "123"}
                       for i, r in enumerate([6, 5, 6])}
        with patch.object(notify.tb_plan_reader, "fetch_plan_planets", AsyncMock(return_value=(entries, None))), \
             patch.object(notify.database, "get_tb_platoon_assignments", return_value=assignments), \
             patch.object(notify.database, "get_all_user_mappings", return_value=[("owner", "123", "Игрок")]), \
             patch.object(notify.database, "get_all_registrations", return_value=[("owner", "123", "Игрок", True)]), \
             patch.object(notify, "channels", return_value={}), \
             patch.dict(notify.tb_platoon_data.ROTE_PLATOON_SUGGESTIONS, {("Lothal", 1): ["Rey", "Old", "Unknown"]}, clear=True), \
             patch.object(notify.database, "resolve_unit_display_names", return_value={"Rey": "REY"}), \
             patch.object(notify.database, "get_game_unit_names", return_value={"REY": "Рей"}):
            rows = await notify.build_player_rows(1, {"id": 1}, 6)
        self.assertEqual([e["unit"] for e in rows[0]["entries"]], ["Рей", "Unknown"])
        text = notify.format_dm_text("Гильдия", "План", 6, rows)
        self.assertEqual(text.count("Лотал · взвод 1"), 1)
        self.assertNotIn("Old", text)

    def test_alts_have_separate_blocks_and_unknown_planet_is_preserved(self):
        rows = [{"name": n, "entries": [{"planet": "New Planet", "operation": 2, "unit": "Рей"}]}
                for n in ("Основной", "Альт")]
        text = notify.format_dm_text("Гильдия", "План", 6, rows)
        for name in ("Основной", "Альт", "New Planet"):
            self.assertIn(name, text)
