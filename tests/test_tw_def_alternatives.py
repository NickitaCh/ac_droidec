import unittest

import tw_def_engine as engine
from web.routes.tw_defense import _parse_dc


class DefenseAlternativesTests(unittest.TestCase):
    def setUp(self):
        self.roster = engine.Roster(['a'], {}, {'a': {'U': {
            'currentRarity': 7, 'currentTier': 13,
            'skill': [{'id': 'z', 'tier': 6}],
        }}}, {'U': 'Unit'}, {}, {'z': (7, None, None)})
        self.roster.dc_reqs = {('focused', 1): (1, 21, '', 'shin', 9),
                               ('focused', 2): (2, 22, '', 'shin', 9)}
        self.roster.datacrons = {'a': [{'id': 'new', 'setId': 22, 'focused': True,
            'templateId': 'datacron_set_22_focused_shin', 'affix': [{}] * 9}]}
        self.dc = {'required': True, 'alternatives': [
            {'kind': 'focused', 'req_id': 1}, {'kind': 'focused', 'req_id': 2}]}

    def test_any_season_and_used_inventory(self):
        self.assertEqual(engine.pick_datacron(self.roster, 'a', self.dc), ('ok', 'new'))
        self.assertEqual(engine.pick_datacron(self.roster, 'a', self.dc, {'new'}), ('used', None))
        del self.roster.dc_reqs[('focused', 1)]
        self.assertEqual(engine.pick_datacron(self.roster, 'a', self.dc), ('ok', 'new'))
        self.assertEqual(engine.pick_datacron(self.roster, 'missing', self.dc), ('unknown', None))

    def test_optional_alternatives_do_not_reserve(self):
        self.assertEqual(engine.pick_datacron(None, 'a', {**self.dc, 'required': False}), (None, None))

    def test_recommendation_names_all_alternatives(self):
        from services.tw_def_notify import describe_recommendation
        from unittest.mock import patch
        with patch('services.tw_def_notify.database.get_datacron_requirement_stats', return_value=[]):
            text = describe_recommendation(self.dc, self.roster.dc_reqs, None, 1)
        self.assertIn(' ИЛИ ', text)
        self.assertIn('21', text)
        self.assertIn('22', text)

    def test_soft_omicron_does_not_ignore_ultimate(self):
        self.roster.skills = {'z': (None, 8, 'ВГ')}
        opt = {'base_id': 'U', 'omicron': True, 'ignore_skills_placement': True}
        squad = {'combat_type': 'character', 'slots': [{'options': [opt]}]}
        self.assertEqual(engine.match_squad(self.roster, 'a', squad).units, ['U'])
        self.assertIsNotNone(engine.check_option(self.roster, 'a', opt, 'character'))
        opt['ultimate'] = True
        self.assertIsNone(engine.match_squad(self.roster, 'a', squad).units)

    def test_form_filters_and_deduplicates(self):
        opts = [{'value': 'focused:1'}, {'value': 'focused:2'}]
        self.assertEqual(_parse_dc(['focused:1', 'focused:1', 'foreign:3', 'focused:2'], True, opts), self.dc)
        self.assertEqual(_parse_dc('focused:1', False, opts),
                         {'kind': 'focused', 'req_id': 1, 'required': False})
        self.assertIsNone(_parse_dc([], True, opts))

    def test_soft_skills_allow_assignment_keep_diagnostics_and_other_limits(self):
        opt = {'base_id': 'U', 'zeta': True}
        squad = {'id': 1, 'combat_type': 'character', 'slots': [{'options': [opt]}]}
        self.assertIsNone(engine.match_squad(self.roster, 'a', squad).units)
        opt['ignore_skills_placement'] = True
        self.assertEqual(engine.match_squad(self.roster, 'a', squad).units, ['U'])
        self.assertIsNotNone(engine.check_option(self.roster, 'a', opt, 'character'))
        candidates = engine.evaluate_candidates(self.roster, squad, [])
        self.assertEqual(candidates[0]['status'], 'ok')
        self.assertTrue(candidates[0]['reason'])
        self.assertIsNone(engine.match_squad(self.roster, 'a', squad, {'U'}).units)
        opt['min_relic'] = 9
        self.assertIsNone(engine.match_squad(self.roster, 'a', squad).units)

    def test_soft_flag_survives_normalization(self):
        slots, errors = engine.normalize_slots([{'options': [
            {'base_id': 'U', 'zeta': True, 'ignore_skills_placement': True}]}], 'character', {'U': 'character'})
        self.assertFalse(errors)
        self.assertTrue(slots[0]['options'][0]['ignore_skills_placement'])
