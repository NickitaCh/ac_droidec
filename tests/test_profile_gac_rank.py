import unittest
from unittest.mock import Mock, patch

from services import profile_image
from services.player_profile import player_meta, summarize


class ProfileGacRankTests(unittest.TestCase):
    def test_latest_season_rank_is_independent_of_order_and_promotions(self):
        player = {
            "profileStat": [{"nameKey": "STAT_SEASON_PROMOTIONS_EARNED_NAME", "value": "19"}],
            "seasonStatus": [
                {"endTime": "300", "rank": 379},
                {"endTime": "100", "rank": 5725},
                {"endTime": "200", "rank": 3434},
            ],
        }
        self.assertEqual(player_meta(player)["gac_rank"], 379)

    def test_unranked_latest_season_does_not_reuse_previous_rank(self):
        player = {"seasonStatus": [{"endTime": "100", "rank": 25}, {"endTime": "200", "rank": 0}]}
        self.assertIsNone(player_meta(player)["gac_rank"])

    def test_removed_season_is_ignored(self):
        player = {"seasonStatus": [{"endTime": "100", "rank": 25}, {"endTime": "200", "rank": 1, "remove": True}]}
        self.assertEqual(player_meta(player)["gac_rank"], 25)

    def test_missing_seasons_are_unranked(self):
        self.assertIsNone(player_meta({})["gac_rank"])

    def test_render_uses_rank_and_keeps_undersized_wins(self):
        meta = player_meta({
            "seasonStatus": [{"endTime": "300", "rank": 379}],
            "profileStat": [
                {"nameKey": "STAT_SEASON_PROMOTIONS_EARNED_NAME", "value": "19"},
                {"nameKey": "STAT_SEASON_UNDERSIZED_SQUAD_WINS_NAME", "value": "1754"},
            ],
        })
        calc = Mock()
        calc.calc_roster_stats.return_value = []
        summary = summarize([], [], meta, calc, {"names": {}, "skills": {}, "gls": []})
        with patch.object(profile_image, "_Canvas") as canvas:
            profile_image.render_player(summary)
        cells = canvas.return_value.grid.call_args_list[0].args[0]
        self.assertIn(("Ранг", "379"), cells)
        self.assertIn(("Меньшим составом", "1 754"), cells)
        self.assertFalse(any(value == "н/д" for _, value in cells))


if __name__ == "__main__":
    unittest.main()
