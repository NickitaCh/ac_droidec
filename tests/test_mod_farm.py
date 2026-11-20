from copy import deepcopy
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from services import mod_farm as farm, mod_optimizer_v2 as engine
from services import mod_optimizer_browser as browser
from tests.test_mod_optimizer_v2 import mod, row


def stats(_calc, unit, mods):
    speed = 100 + sum(m['primary']['value'] if m['primary']['unit_stat_id'] == 5 else 0 for m in mods)
    speed += sum(s['value'] for m in mods for s in m['secondaries'] if s['unit_stat_id'] == 5)
    if sum(m['set_id'] == 4 for m in mods) >= 4:
        speed += 20
    return {'Speed': speed, 'Health': 1000}


class RecommendationTests(unittest.TestCase):
    def setUp(self):
        self.patch = patch.object(engine, 'final_stats', stats)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.mods = [mod(f'a{s}', s, 'A', set_id=4 if s > 4 else 1) for s in range(1, 7)]
        self.units = {'A': {'currentTier': 13, 'currentLevel': 85}}
        self.rows = {'A': {'P': [row(value=180)]}}
        self.chosen = {'A': dict(mods=self.mods[:], final_stats=stats(None, self.units['A'], self.mods),
                                 evaluation_unit=self.units['A'])}
        self.pair = [dict(source='a1', slot=1, set=4, primary=5), dict(source='a2', slot=2, set=4, primary=5)]

    def context(self, locked=(), mode='current'):
        return farm.farm_context(None, self.rows, self.units, {}, self.mods, self.chosen, locked, mode)

    def test_two_missing_mods_close_requirement_when_neither_single_helps(self):
        ctx = self.context()
        self.assertIsNone(farm.farm_trial(ctx, 'A', self.pair[:1]))
        found = farm.farm_trial(ctx, 'A', self.pair)
        self.assertEqual(found['kind'], 'shortage')
        self.assertTrue(found['completes'])
        self.assertEqual(found['gains'][0]['after'], 180)
        self.assertEqual([n['status'] for n in found['needs']], ['missing', 'missing'])
        suggestions = farm.recommend(ctx, 'A')
        self.assertTrue(suggestions)
        self.assertTrue(suggestions[0]['completes'])
        self.assertEqual(len(suggestions[0]['needs']), 2)

    def test_finished_build_still_gets_useful_upgrade(self):
        self.rows['A']['P'] = [row(value=150)]
        found = farm.recommend(self.context(), 'A')
        self.assertTrue(found)
        self.assertEqual(found[0]['kind'], 'upgrade')
        self.assertGreater(found[0]['gains'][0]['after'], 160)

    def test_optional_goal_can_improve_finished_build(self):
        self.rows['A']['P'] = [row(value=150), row(value=180, priority='optional')]
        found = farm.farm_trial(self.context(), 'A', self.pair)
        self.assertEqual(found['kind'], 'upgrade')
        self.assertEqual(len(found['closed']), 1)
        self.assertEqual(found['closed'][0]['priority'], 'optional')

    def test_preserves_upper_bound_and_incoming_turn_order(self):
        self.rows['A']['P'] = [row(value=150), row(value=170, op='<=')]
        self.assertIsNone(farm.farm_trial(self.context(), 'A', self.pair))
        self.rows['A']['P'] = [row(value=150)]
        self.units['B'] = dict(currentTier=13, currentLevel=85)
        self.rows['B'] = {'Q': [row(value=1, other='A')]}
        self.chosen['B'] = dict(mods=[], final_stats={'Speed': 175, 'Health': 1000}, evaluation_unit=self.units['B'])
        self.assertTrue(self.context()['checks']['B']['full_pass'])
        self.assertIsNone(farm.farm_trial(self.context(), 'A', self.pair))

    def test_spares_and_occupied_copies_are_distinguished(self):
        self.mods.append(mod('spare1', 1, set_id=4))
        self.mods.append(mod('busy2', 2, 'B', set_id=4))
        self.units['B'] = dict(currentTier=13, currentLevel=85)
        self.rows['B'] = {'Q': []}
        self.chosen['B'] = dict(mods=[self.mods[-1]], final_stats={'Speed': 110, 'Health': 1000}, evaluation_unit=self.units['B'])
        found = farm.farm_trial(self.context(), 'A', self.pair)
        self.assertEqual([n['status'] for n in found['needs']], ['available', 'occupied'])
        self.mods[-2]['locked'] = True
        found = farm.farm_trial(self.context(), 'A', self.pair)
        self.assertEqual(found['needs'][0]['status'], 'occupied')

    def test_character_and_mod_locks_and_real_gear_are_respected(self):
        self.assertEqual(farm.recommend(self.context(locked=['A']), 'A'), [])
        self.mods[0]['locked'] = True
        self.assertIsNone(farm.farm_trial(self.context(), 'A', self.pair))
        self.mods[0]['locked'] = False
        self.units['A']['currentTier'] = 11
        self.assertIsNone(farm.farm_trial(self.context(), 'A', [dict(source='', slot=2, set=4, primary=5)]))
        self.units['A']['currentLevel'] = 49
        self.assertEqual(farm.recommend(self.context(), 'A'), [])

    def test_recipes_rebuild_values_and_never_mutate_real_inventory(self):
        before = deepcopy(self.mods)
        found = farm.farm_trial(self.context(), 'A', self.pair)
        self.assertEqual(self.mods, before)
        self.assertNotIn('farm-example', [m['id'] for m in self.chosen['A']['mods']])
        checked = farm.verify(self.context(), {'A': [found['recipes']]})
        self.assertEqual(checked['A'][0]['gains'], found['gains'])
        for bad in (dict(self.pair[0], speed=9999), dict(self.pair[0], source='unknown'),
                    dict(self.pair[0], slot=7), dict(self.pair[0], set=99)):
            with self.assertRaises(ValueError):
                farm.verify(self.context(), {'A': [[bad]]})
        with self.assertRaises(ValueError):
            farm.verify(self.context(), {'A': [self.pair] * 4})
        with self.assertRaises(ValueError):
            farm.verify(self.context(), {'foreign': []})

    def test_canonical_primary_scaling_and_conflict(self):
        self.mods.append(mod('six', 3, rarity=6))
        self.mods[-1]['secondaries'] = [dict(unit_stat_id=55, unscaled=1000000, value=1, is_percent=True, name='Health')]
        rebuilt = farm.farm_mod(dict(source='six', slot=6, set=1, primary=56), self.context()['indexed'])
        self.assertEqual(rebuilt['primary']['value'], 24)
        self.assertEqual(rebuilt['primary']['unscaled'], 24000000)
        self.assertEqual(len(rebuilt['secondaries']), 1)
        with self.assertRaises(ValueError):
            farm.farm_mod(dict(source='six', slot=6, set=1, primary=55), self.context()['indexed'])
        with self.assertRaises(ValueError):
            farm.farm_mod(dict(source='six', slot=1, set=1, primary=5), self.context()['indexed'])

    def test_non_selected_donor_is_occupied_not_a_free_spare(self):
        self.mods.append(mod('donor1', 1, 'C', set_id=4))
        found = farm.farm_trial(self.context(), 'A', self.pair)
        self.assertEqual(found['needs'][0]['status'], 'occupied')
        self.assertEqual(found['needs'][0]['candidates'][0]['owner'], 'C')

    def test_no_useful_change_returns_no_recommendations(self):
        for m in self.mods:
            m['set_id'] = 4
        self.rows['A']['P'] = [row(value=150)]
        self.chosen['A']['final_stats'] = stats(None, self.units['A'], self.mods)
        self.assertEqual(farm.recommend(self.context(), 'A'), [])

    def test_alternative_scheme_cannot_buy_a_free_relic_upgrade(self):
        self.units['A']['relic'] = {'currentTier': 3}
        self.rows['A']['P'] = [row('ModSet', 1, skill='1', scheme=1), row('Relic', 7, scheme=2),
                              row('ModSet', 1, skill='4', scheme=2), row(value=180, scheme=2)]
        self.assertIsNone(farm.farm_trial(self.context(mode='plate'), 'A', self.pair))

    def test_browser_exports_identical_recommendation_logic(self):
        namespace = {}
        self.patch.stop()
        exec(browser.python_source(), namespace)
        self.patch.start()
        namespace['optimizer'].final_stats = stats
        ctx = namespace['farm_context'](None, self.rows, self.units, {}, self.mods, self.chosen, [], 'current')
        self.assertEqual(namespace['recommend'](ctx, 'A'), farm.recommend(self.context(), 'A'))


class RealAssessmentTests(unittest.TestCase):
    def test_browser_proof_is_rebuilt_with_real_statcalc_and_does_not_enter_allocation(self):
        from swgoh_comlink import StatCalc
        from tests.test_mod_optimizer_browser import prepared
        data = prepared()
        data['rows']['A']['P'].append(row('Potency', 0))
        calc = StatCalc(json.loads((Path(__file__).parent/'fixtures/mod_optimizer_statcalc.json').read_text()))
        allocation = {'A': [m['id'] for m in data['mods']]}
        recipes = [dict(source='1', slot=1, set=7, primary=5), dict(source='2', slot=2, set=7, primary=5)]
        results, passed, donors = browser.assess(calc, data, allocation, {'A': [recipes]})
        self.assertEqual(passed, ['P'])
        self.assertEqual([m['id'] for m in results[0]['used_mods']], allocation['A'])
        suggestion = results[0]['recommendations'][0]
        self.assertEqual(suggestion['kind'], 'upgrade')
        self.assertGreater(next(g for g in suggestion['gains'] if g['stat_name'] == 'Potency')['delta'], 0)
        self.assertEqual(results[0]['passed'][1]['current'], engine.final_stats(calc, data['units']['A'], data['mods'])['Potency'])
        with self.assertRaises(ValueError):
            browser.assess(calc, data, allocation, {'A': [[dict(recipes[0], unscaled=10**20)]]})


if __name__ == '__main__':
    unittest.main()
