import unittest

from services.player_profile import player_meta


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


if __name__ == "__main__":
    unittest.main()
