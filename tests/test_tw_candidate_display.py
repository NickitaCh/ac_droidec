import unittest
from unittest.mock import patch

from tw_def_engine import Roster
from web.routes.tw_defense import _candidate_json


class CandidateDisplayTests(unittest.TestCase):
    def test_actual_gp_orders_candidates_before_suggestions_and_relic_score(self):
        roster = Roster(['a', 'b'], {'a': 'Alpha', 'b': 'Beta'}, {
            'a': {'U': {'gp': 100, 'skill': [{'id': 'z', 'tier': 7}, {'id': 'o', 'tier': 8}]}},
            'b': {'U': {'gp': 200, 'skill': [{'id': 'z', 'tier': 6}]}}
        }, {'U': 'Unit'}, {}, {'z': (7, None, None), 'o': (7, 8, 'ВГ')})
        candidates = [dict(ally_code=c, name=roster.names[c], status='ok', reason=None,
                           assigned=0, conflict=0, power=p, units=['U']) for c, p in [('a', 9), ('b', 1)]]
        with patch('web.routes.tw_defense.stat_forecast.cached_stat_calc', return_value=None):
            result = _candidate_json(roster, {'combat_type': 'character'}, candidates, {'a'})
        self.assertEqual([r['ally_code'] for r in result], ['b', 'a'])
        self.assertEqual(result[1]['units'][0]['zetas'], 2)
        self.assertEqual(result[1]['units'][0]['omicrons_tw'], 1)
        self.assertEqual(result[0]['units'][0]['zetas'], 0)

    def test_missing_gp_is_not_presented_as_zero(self):
        roster = Roster(['a'], {}, {'a': {'U': {}}}, {}, {}, {})
        candidate = dict(ally_code='a', name='A', status='ok', reason=None,
                         assigned=0, conflict=0, power=7, units=['U'])
        with patch('web.routes.tw_defense.stat_forecast.cached_stat_calc', return_value=None):
            result = _candidate_json(roster, {'combat_type': 'character'}, [candidate], set())
        self.assertIsNone(result[0]['gp'])
