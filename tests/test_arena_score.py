import copy
import unittest
from pathlib import Path
from unittest.mock import patch

from services import arena_rules, arena_score, game_personal_modes as modes


class ArenaScoreTests(unittest.TestCase):
    def setUp(self):
        # Trimmed real GetTerritoryMapStatuses response, 2026-11-24. Names,
        # player/unit instance IDs, channels, stats and datacrons were removed.
        self.catalog_patch = patch.object(arena_rules.database, 'get_bot_state', return_value=None)
        self.catalog_patch.start()
        self.addCleanup(self.catalog_patch.stop)
        self.state = modes.arena((Path(__file__).parent / 'fixtures/arena_status.bin').read_bytes())
        self.match = next(match for match in self.state['matches'] if match['current'])

    def test_real_current_match_matches_hotbot_and_keeps_first_attack_award_separate(self):
        self.assertEqual(self.match['score'], {'home': 0, 'away': 0})
        for side in ('home', 'away'):
            self.assertEqual(self.match['maximum'][side], {'full': 2085, 'undersized': 2133, 'first_attack_bonus': 10})
        self.assertEqual(self.match['defense'], (18, 18))
        self.assertEqual(self.match['number'], 3)
        self.assertEqual(self.match['round'], 2)

    def test_raw_points_include_nonzero_older_matches_without_inventing_missing_opponent_score(self):
        self.assertEqual(self.state['matches'][0]['score'], {'home': 2034, 'away': None})
        self.assertEqual(self.state['matches'][1]['score'], {'home': 233, 'away': None})

    def test_retries_lower_the_ceiling_and_do_not_consume_opponents_first_attack_bonus(self):
        for attempts, loss in ((1, 20), (2, 30), (3, 30)):
            with self.subTest(attempts=attempts):
                match = copy.deepcopy(self.match)
                match['away'][0]['teams'][0]['defends'] = attempts
                match['home'][0]['attacked'] = 1
                result = arena_score.project(match)
                self.assertEqual(result['home']['full'], 2085 - loss)
                self.assertEqual(result['home']['undersized'], 2133 - loss)
                self.assertEqual(result['home']['first_attack_bonus'], 0)
                self.assertEqual(result['away']['full'], 2085)
                self.assertEqual(result['away']['first_attack_bonus'], 10)

    def test_preloaded_dead_units_do_not_award_enemy_defeat_points_twice(self):
        match = copy.deepcopy(self.match)
        team = match['away'][0]['teams'][0]
        team.update(defends=1, health=[100, 0, 0])
        match['score']['home'] = 12  # First attack plus two killed enemies.
        match['home'][0]['attacked'] = 1
        self.assertEqual(arena_score.project(match)['home']['full'], 2085 + 12 - 20 - 2)

    def test_cleared_zone_is_not_awarded_again_and_current_score_is_preserved(self):
        match = copy.deepcopy(self.match)
        zone = match['away'][0]
        zone['remaining'] = 0
        for team in zone['teams']:
            team['state'] = 3
        match['score']['home'] = 530  # 5 * 57 + 260 capture - 15 lost banners.
        match['home'][0]['attacked'] = 1
        result = arena_score.project(match)['home']
        self.assertEqual(result['full'], 2085 - 545 + 530)
        self.assertEqual(result['undersized'], 2133 - 555 + 530)

    def test_full_clear_has_no_potential_beyond_earned_score(self):
        match = copy.deepcopy(self.match)
        for zone in match['away']:
            zone['remaining'] = 0
        match['score']['home'] = 1999
        self.assertEqual(arena_score.project(match)['home'], {'full': 1999, 'undersized': 1999, 'first_attack_bonus': 0})

    def test_unknown_counters_and_maps_stay_unknown(self):
        for change in ('counter', 'map'):
            with self.subTest(change=change):
                match = copy.deepcopy(self.match)
                if change == 'counter':
                    match['away'][0]['remaining'] = None
                else:
                    match['map'] = 'new-unknown-map'
                self.assertIsNone(arena_score.project(match)['home']['full'])

    def test_locked_battle_uses_previous_failures_not_the_unfinished_attempt(self):
        match = copy.deepcopy(self.match)
        match['away'][0]['teams'][0].update(state=2, defends=1)
        self.assertEqual(arena_score.project(match)['home']['full'], 2085)


if __name__ == '__main__':
    unittest.main()
