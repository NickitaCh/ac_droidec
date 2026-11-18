import copy
import itertools
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from services import mod_optimizer_v2 as engine, mod_optimizer_plans as plans
from services import game_mod_actions as actions, game_account as ga


def row(stat='Speed', value=150, op='>=', priority='required', skill='', slot='', other=None, scheme=None, plate='P'):
    return (1, plate, 'A', stat, op, value, priority, '', '', '', '', skill, slot, other, scheme)


def mod(mid, slot=1, owner=None, speed=10, set_id=1, locked=False, rarity=5):
    return {'id': mid, 'level': 15, 'tier': 1, 'rarity': rarity, 'set_id': set_id,
            'slot_id': slot, 'slot_name': engine.legacy.SHAPES[slot-1], 'set_name': str(set_id),
            'locked': locked, 'equipped_to': owner,
            'primary': {'unit_stat_id': 5, 'unscaled': speed * 100_000_000,
                        'value': speed, 'is_percent': False, 'name': 'Speed'}, 'secondaries': []}


def calc(_, unit):
    mods = unit.get('equippedStatMod', [])
    return {'Speed': 100 + sum(float(m['primaryStat']['stat']['unscaledDecimalValue']) / 1e8 for m in mods),
            'Health': 1000}


def candidate(mods, speed, baseline=False):
    return {'mods': mods, 'mod_ids': frozenset(m['id'] for m in mods), 'final_stats': {'Speed': speed},
            'baseline': baseline, 'plate_ok': {'P': speed >= 150}, 'full_pass': speed >= 150,
            'required_matched': int(speed >= 150), 'optional_matched': 0, 'deficit': max(0, 150-speed)/150,
            'moves': 0 if baseline else len(mods)}


class EvaluationTests(unittest.TestCase):
    def test_projection_respects_active_scheme_highest_required_target_and_never_downgrades(self):
        unit = {'relic': {'currentTier': 3}, 'currentTier': 13, 'currentLevel': 85, 'currentRarity': 7}
        mods = [mod(str(s), s, set_id=4 if s < 5 else 1) for s in range(1, 7)]
        rows = {'P': [row('ModSet', 1, skill='2', scheme=1), row('Relic', 9, scheme=1),
                      row('ModSet', 1, skill='4', scheme=2), row('Relic', 5, scheme=2),
                      row('Relic', 10, priority='optional')]}
        self.assertEqual(engine.stat_engine.get_current_relic_level(engine.evaluation_unit(unit, rows, mods, 'plate')), 5)
        rows['Q'] = [row('Relic', 8)]
        self.assertEqual(engine.stat_engine.get_current_relic_level(engine.evaluation_unit(unit, rows, mods, 'plate')), 8)
        r9 = {**unit, 'relic': {'currentTier': 11}}
        self.assertIs(engine.evaluation_unit(r9, rows, mods, 'plate'), r9)
        self.assertIs(engine.evaluation_unit(unit, {'P': [row('Speed', 200)]}, mods, 'plate'), unit)
        self.assertIs(engine.evaluation_unit(unit, rows, mods, 'current'), unit)

    def test_required_and_optional_thresholds_are_not_merged(self):
        result = engine.evaluate({'P': [row(value=150), row(value=180, priority='optional')]}, {}, [], {'Speed': 160}, {})
        self.assertTrue(result['full_pass'])
        self.assertEqual([r['ok'] for r in result['rows']], [True, False])
        self.assertEqual([r['row_index'] for r in result['rows']], [0, 1])

    def test_mod_sets_and_primaries_are_real_requirements(self):
        mods = [mod(str(i), i, set_id=4 if i < 5 else 1) for i in range(1, 7)]
        rows = [row('ModSet', 1, skill='4'), row('ModSetOnly', 1, skill='4,1'),
                row('ModSetAny', 1, skill='3,4'), row('ModPrimary', 55, skill='5,55', slot='arrow')]
        result = engine.evaluate({'P': rows}, {}, mods, {}, {})
        self.assertTrue(result['full_pass'])
        mods[1]['primary']['unit_stat_id'] = 56
        self.assertFalse(engine.evaluate({'P': rows}, {}, mods, {}, {})['full_pass'])

    def test_schemes_follow_set_then_primary_detection(self):
        mods = [mod(str(i), i, set_id=4 if i < 5 else 1) for i in range(1, 7)]
        rows = [row('ModSet', 1, skill='2', scheme=1), row(value=300, scheme=1),
                row('ModSet', 1, skill='4', scheme=2), row(value=150, scheme=2)]
        result = engine.evaluate({'P': rows}, {}, mods, {'Speed': 160}, {})
        self.assertTrue(result['full_pass'])
        self.assertEqual(len(result['rows']), 2)

    def test_nested_plate_schemes_are_independent(self):
        mods = [mod(str(i), i, set_id=4 if i < 5 else 1) for i in range(1, 7)]
        rows = [row('ModSet', 1, skill='2', scheme=1, plate='leaf1'),
                row('ModSet', 1, skill='4', scheme=2, plate='leaf1'),
                row(value=150, scheme=1, plate='leaf2'), row(value=900, scheme=2, plate='leaf2')]
        self.assertTrue(engine.evaluate({'P': rows}, {}, mods, {'Speed': 160}, {})['full_pass'])

    def test_comparison_offset_direction_and_missing_peer(self):
        rows = {'P': [row(value=10, op='<=', other='B')]}
        self.assertTrue(engine.evaluate(rows, {}, [], {'Speed': 150}, {'B': {'Speed': 160}})['full_pass'])
        self.assertFalse(engine.evaluate(rows, {}, [], {'Speed': 150}, {})['full_pass'])

    def test_omicron_and_relic_cannot_be_satisfied_by_mods(self):
        rows = {'P': [row('Relic', 8), row('Omicron', 1, skill='ability')]}
        result = engine.evaluate(rows, {'relic': {'currentTier': 7}, 'skill': [{'id': 'ability', 'tier': 6}]}, [], {}, {}, {'ability': 7})
        self.assertFalse(result['full_pass'])
        self.assertEqual(result['required_matched'], 0)

    def test_invalid_inventory_rejected(self):
        for mods in ([mod('x'), mod('x', 2)], [mod('a', owner='A'), mod('b', owner='A')], [mod('')]):
            with self.assertRaises(ValueError):
                engine.validate_inventory(mods)


class SearchTests(unittest.TestCase):
    def test_future_relic_does_not_allow_equipping_six_dot_mods_at_current_g11(self):
        pool = [mod(str(s), s, rarity=6) for s in range(1, 7)]
        with patch.object(engine.stat_engine, 'calc_final_stats', calc):
            found = engine.search(None, 'A', {'currentTier': 11, 'currentLevel': 85},
                                  {'P': [row('Relic', 7)]}, pool, [], {}, relic_mode='plate')
        self.assertEqual(found[0]['mods'], [])
    def test_lock_keeps_incomplete_baseline(self):
        current = [mod('a', 1, 'A')]
        with patch.object(engine.stat_engine, 'calc_final_stats', calc):
            found = engine.search(None, 'A', {}, {'P': [row()]}, [], current, {}, locked=True)
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]['mod_ids'], {'a'})

    def test_upper_bound_and_locked_mod_and_six_dot_eligibility(self):
        current = [mod(f'old{s}', s, 'A', speed=20, locked=s == 1) for s in range(1, 7)]
        pool = current + [mod(f'slow{s}', s, speed=1) for s in range(1, 7)] + [mod('six', 2, speed=0, rarity=6)]
        with patch.object(engine.stat_engine, 'calc_final_stats', calc):
            found = engine.search(None, 'A', {'currentTier': 11}, {'P': [row(value=130, op='<=')]}, pool, current, {}, seconds=1)
        self.assertTrue(any(c['full_pass'] for c in found))
        self.assertTrue(any(c['baseline'] for c in found))
        self.assertTrue(all('old1' in c['mod_ids'] for c in found))
        self.assertTrue(all('six' not in c['mod_ids'] for c in found))

    def test_small_inventory_matches_exhaustive_feasibility(self):
        pool = [mod(f'{s}_{v}', s, speed=v, set_id=1 if s < 5 else 3) for s in range(1, 7) for v in (3, 12)]
        rows = {'P': [row(value=160), row(value=165, op='<=')]}
        feasible = any(160 <= 100 + sum(m['primary']['value'] for m in choice) <= 165
                       for choice in itertools.product(*[[m for m in pool if m['slot_id'] == s] for s in range(1, 7)]))
        with patch.object(engine.stat_engine, 'calc_final_stats', calc):
            found = engine.search(None, 'A', {}, rows, pool, [], {}, seconds=2)
        self.assertEqual(any(c['full_pass'] for c in found), feasible)


class RealStatCalcTests(unittest.TestCase):
    # Public calculator tables (VADER only), fetched 2026-11-16 from
    # https://github.com/swgoh-utils/gamedata/blob/main/gameData.json
    @classmethod
    def setUpClass(cls):
        from swgoh_comlink import StatCalc
        cls.calc = StatCalc(json.loads((Path(__file__).parent / 'fixtures/mod_optimizer_statcalc.json').read_text()))
        cls.unit = {'definitionId': 'VADER:SEVEN_STAR', 'currentRarity': 7, 'currentLevel': 85,
                    'currentTier': 13, 'relic': {'currentTier': 9}, 'skill': [], 'equipment': []}

    def test_real_base_and_percent_secondary_scaling(self):
        base = engine.stat_engine.calc_final_stats(self.calc, self.unit)
        self.assertEqual(base['Speed'], 141)
        self.assertEqual(base['Health'], 51902)
        one = mod('one', speed=10)
        one['secondaries'] = [{'unit_stat_id': 55, 'unscaled': 10_000_000, 'value': 10,
                               'is_percent': True, 'name': 'Health'}]
        raw = engine.legacy._to_equipped_stat_mod(one)
        final = engine.stat_engine.calc_final_stats(self.calc, {**self.unit, 'equippedStatMod': [raw]})
        self.assertEqual(final['Speed'], 151)
        self.assertGreater(final['Health'], base['Health'])
        self.assertEqual(raw['secondaryStat'][0]['stat']['unscaledDecimalValue'], 10_000_000)

    def test_r1_to_r7_uses_real_projected_base_including_percent_mods(self):
        unit = copy.deepcopy(self.unit)
        unit['relic']['currentTier'] = 3
        original = copy.deepcopy(unit)
        one = mod('hp')
        one['primary'] = {'unit_stat_id': 55, 'unscaled': 10_000_000, 'value': 10,
                          'is_percent': True, 'name': 'Health'}
        r1 = engine.final_stats(self.calc, unit, [one])
        r7 = engine.final_stats(self.calc, self.unit, [one])
        self.assertGreater(r7['Health'], r1['Health'])
        target = (r1['Health'] + r7['Health']) / 2
        rows = {'P': [row('Relic', 7), row('Health', target)]}
        current, = engine.search(self.calc, 'VADER', unit, rows, [], [one], {}, locked=True)
        forecast, = engine.search(self.calc, 'VADER', unit, rows, [], [one], {}, locked=True, relic_mode='plate')
        self.assertFalse(current['full_pass'])
        self.assertTrue(forecast['full_pass'])
        self.assertEqual(forecast['final_stats'], r7)
        self.assertEqual(forecast['mods'], [one])
        self.assertEqual(unit, original)

    def test_real_search_matches_exhaustive_small_inventory_and_sets(self):
        pool = [mod(f'{s}_{v}', s, speed=v, set_id=4 if s < 5 else 1)
                for s in range(1, 7) for v in (3, 12)]
        for m in pool:
            m['primary'] = {'unit_stat_id': 55, 'value': 5, 'unscaled': 5_000_000, 'is_percent': True, 'name': 'Health'}
            m['secondaries'] = [{'unit_stat_id': 5, 'value': int(m['id'].split('_')[1]),
                                'unscaled': int(m['id'].split('_')[1]) * 100_000_000, 'is_percent': False, 'name': 'Speed'}]
        rows = {'P': [row(value=210), row(value=230, op='<='), row('ModSet', 1, skill='4')]}
        exhaustive = []
        for choice in itertools.product(*[[m for m in pool if m['slot_id'] == s] for s in range(1, 7)]):
            stats = engine.stat_engine.calc_final_stats(self.calc, {**self.unit, 'equippedStatMod': [engine.legacy._to_equipped_stat_mod(m) for m in choice]})
            if engine.evaluate(rows, self.unit, choice, stats, {})['full_pass']:
                exhaustive.append(choice)
        self.assertTrue(exhaustive)
        found = engine.search(self.calc, 'VADER', self.unit, rows, pool, [], {}, seconds=2)
        self.assertTrue(any(c['full_pass'] for c in found))
        for c in found:
            exact = engine.stat_engine.calc_final_stats(self.calc, {**self.unit, 'equippedStatMod': [engine.legacy._to_equipped_stat_mod(m) for m in c['mods']]})
            self.assertEqual({k: c['final_stats'][k] for k in exact}, exact)
            self.assertEqual(c['final_stats']['Total Life'], exact['Health'] + exact['Protection'])


class AllocationTests(unittest.TestCase):
    def test_every_numeric_row_constrains_the_allocation(self):
        candidates = {'A': [candidate([], 100, True), candidate([mod('fast')], 200), candidate([mod('balanced')], 160)]}
        rows = {'A': {'P': [row(value=150), row(value=170, op='<=')]}}
        chosen, _ = engine.solve(candidates, {'P': ['A']}, rows, {'A': {}}, {}, {})
        self.assertEqual(chosen['A']['mod_ids'], {'balanced'})

    def test_rare_mod_is_given_to_character_without_an_alternative(self):
        rare, alt = mod('rare'), mod('alt')
        candidates = {'A': [candidate([], 100, True), candidate([rare], 160), candidate([alt], 155)],
                      'B': [candidate([], 100, True), candidate([rare], 160)]}
        chosen, _ = engine.solve(candidates, {'P': ['A', 'B']}, {'A': {'P': [row()]}, 'B': {'P': [row()]}},
                                 {'A': {}, 'B': {}}, {}, {})
        self.assertEqual(chosen['A']['mod_ids'], {'alt'})
        self.assertEqual(chosen['B']['mod_ids'], {'rare'})

    def test_current_working_build_wins_over_unnecessary_move(self):
        candidates = {'A': [candidate([mod('old')], 160, True), candidate([mod('new')], 170)]}
        chosen, _ = engine.solve(candidates, {'P': ['A']}, {'A': {'P': [row()]}}, {'A': {}}, {}, {})
        self.assertTrue(chosen['A']['baseline'])

    def test_relative_speed_uses_both_selected_builds(self):
        candidates = {'A': [candidate([], 100, True), candidate([mod('a')], 180)],
                      'B': [candidate([], 100, True), candidate([mod('b')], 170)]}
        rows = {'A': {'P': [row(value=10, other='B'), row(value=170)]}, 'B': {'P': [row(value=160)]}}
        chosen, _ = engine.solve(candidates, {'P': ['A', 'B']}, rows, {'A': {}, 'B': {}}, {}, {})
        self.assertEqual(chosen['A']['final_stats']['Speed'], 180)
        self.assertEqual(chosen['B']['final_stats']['Speed'], 170)

    def test_unowned_character_does_not_crash_or_pass_plate(self):
        chosen, _ = engine.solve({'A': [candidate([], 160, True)]}, {'P': ['A', 'B']},
                                 {'A': {'P': [row()]}, 'B': {'P': [row()]}}, {'A': {}}, {}, {})
        self.assertEqual(set(chosen), {'A'})

    def test_scheme_two_numeric_rows_are_not_silently_skipped(self):
        mods = [mod(str(i), i, set_id=4 if i < 5 else 1) for i in range(1, 7)]
        c = candidate(mods, 160, True)
        rows = {'A': {'P': [row('ModSet', 1, skill='4', scheme=2), row(value=170, scheme=2)]}}
        chosen, _ = engine.solve({'A': [c]}, {'P': ['A']}, rows, {'A': {}}, {}, {})
        check = engine.evaluate(rows['A'], {}, chosen['A']['mods'], chosen['A']['final_stats'], {})
        self.assertFalse(check['full_pass'])


class PlanTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = patch.object(plans.database, 'DB_NAME', str(Path(self.temp.name) / 'plans.db'))
        self.db.start()

    def tearDown(self):
        self.db.stop()
        self.temp.cleanup()

    def test_plan_is_owner_scoped_expiring_and_one_shot(self):
        token = plans.create('owner', 1, '123', {'game_source': True, 'dry_run': False})
        with self.assertRaises(ValueError):
            plans.get(token, 'stranger', 1)
        with self.assertRaises(ValueError):
            plans.create('owner', 1, '123', {})
        plans.finish(token, 'ready', {'game_source': True, 'dry_run': False})
        self.assertEqual(plans.claim(token, 'owner', 1), {'game_source': True, 'dry_run': False})
        with self.assertRaises(ValueError):
            plans.claim(token, 'owner', 1)

    def test_csv_and_expired_plan_cannot_be_applied(self):
        token = plans.create('o', 1, '123', {})
        plans.finish(token, 'ready', {'game_source': False})
        with self.assertRaises(ValueError):
            plans.claim(token, 'o', 1)
        plans.finish(token, 'ready', {'game_source': True}, expected='ready')
        with patch.object(plans.time, 'time', return_value=10**12), self.assertRaises(ValueError):
            plans.claim(token, 'o', 1)

    def test_fingerprint_detects_calibration_lock_and_progression(self):
        mods = [mod('a')]
        original = plans.fingerprint(mods, {'A': 'old'})
        mods[0]['locked'] = True
        self.assertNotEqual(original, plans.fingerprint(mods, {'A': 'old'}))
        self.assertNotEqual(original, plans.fingerprint([mod('a')], {'A': 'new'}))

    def test_locks_are_persistent_and_account_and_owner_scoped(self):
        plans.set_locks('owner', 1, '123', ['B', 'A', 'A'])
        self.assertEqual(plans.get_locks('owner', 1, '123'), ['A', 'B'])
        self.assertEqual(plans.get_locks('owner', 1, '456'), [])
        self.assertEqual(plans.get_locks('other', 1, '123'), [])

    def test_dry_run_plan_cannot_be_claimed_even_if_game_sourced(self):
        token = plans.create('owner', 1, '123', {})
        plans.finish(token, 'ready', {'game_source': True, 'dry_run': True})
        with self.assertRaises(ValueError):
            plans.claim(token, 'owner', 1)


class WorkflowTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        unit_types = patch.object(engine.database, 'get_unit_types', return_value={})
        unit_types.start()
        self.addCleanup(unit_types.stop)

    async def test_ship_only_plate_does_not_enter_mod_calculation(self):
        with patch.object(engine.database, 'get_stat_requirement_characters', return_value=['SHIP']), \
                patch.object(engine.database, 'get_unit_types', return_value={'SHIP': 'ship'}), \
                patch.object(engine.database, 'get_stat_requirements', return_value=[row()]) as requirements, \
                patch.object(engine.database, 'get_skill_omicron_tiers', return_value={}), \
                patch.object(engine, 'final_stats', return_value={'Speed': 100}) as stats:
            with self.assertRaisesRegex(ValueError, 'В выбранных плейтах нет персонажей'):
                await engine.optimize(None, None, ['P'], '123', [], 1, units_override={'SHIP': {}})
            requirements.assert_not_called()
            stats.assert_not_called()

    async def test_turn_order_changes_allocation_and_keeps_plate_totals(self):
        mods = [mod(f'{b}{s}', s, b, speed=5) for b in ('A', 'B') for s in range(1, 7)]
        mods.append(mod('faster-arrow', 2, speed=20))
        units = {b: {'currentTier': 13} for b in ('A', 'B')}
        with patch.object(engine.database, 'get_stat_requirement_characters', return_value=['A', 'B']), \
                patch.object(engine.database, 'get_stat_requirements', side_effect=lambda *args, **kwargs: [row(value=120)]), \
                patch.object(engine.database, 'get_unit_types', return_value={}), \
                patch.object(engine.database, 'get_skill_omicron_tiers', return_value={}), \
                patch.object(engine.database, 'get_game_unit_name', side_effect=lambda b: b), \
                patch.object(engine.stat_engine, 'calc_final_stats', calc):
            results, passed, _, _ = await engine.optimize(
                None, None, ['P'], '123', mods, 1, units_override=units, turn_order=[('A', 'B')])
        self.assertEqual(passed, ['P'])
        faster = next(result for result in results if result['base_id'] == 'A')
        comparison = next(req for req in faster['passed'] if req['compare'] == 'B')
        self.assertGreaterEqual(comparison['current'], comparison['threshold'])
        self.assertEqual(comparison['before'], 130)

    async def test_comparison_uses_each_characters_future_relic(self):
        from swgoh_comlink import StatCalc
        real_calc = StatCalc(json.loads((Path(__file__).parent / 'fixtures/mod_optimizer_statcalc.json').read_text()))
        unit = {'definitionId': 'VADER:SEVEN_STAR', 'currentRarity': 7, 'currentLevel': 85,
                'currentTier': 13, 'relic': {'currentTier': 3}, 'skill': [], 'equipment': []}
        groups = {'A': [row('Relic', 7), row('Health', 1, op='<=', other='B')], 'B': [row('Relic', 8)]}
        with patch.object(engine.database, 'get_stat_requirement_characters', return_value=['A', 'B']), \
                patch.object(engine.database, 'get_stat_requirements', side_effect=lambda p, b, **_: groups[b]), \
                patch.object(engine.database, 'get_skill_omicron_tiers', return_value={}), \
                patch.object(engine.database, 'get_game_unit_name', side_effect=lambda b: b):
            results, passed, _, _ = await engine.optimize(None, real_calc, ['P'], '123', [], 1,
                                                        units_override={'A': unit, 'B': copy.deepcopy(unit)}, relic_mode='plate')
        self.assertEqual(passed, ['P'])
        compare = next(r for r in results[0]['passed'] if r['compare'])
        self.assertTrue(compare['ok'])
        self.assertFalse(compare['now_ok'])

    async def test_projected_report_distinguishes_now_from_future_and_moves_no_relics(self):
        from swgoh_comlink import StatCalc
        real_calc = StatCalc(json.loads((Path(__file__).parent / 'fixtures/mod_optimizer_statcalc.json').read_text()))
        unit = {'definitionId': 'VADER:SEVEN_STAR', 'currentRarity': 7, 'currentLevel': 85,
                'currentTier': 13, 'relic': {'currentTier': 3}, 'skill': [], 'equipment': []}
        r1 = engine.final_stats(real_calc, unit, [])['Health']
        r7 = engine.final_stats(real_calc, engine.stat_engine.project_unit_relic(unit, 7), [])['Health']
        with patch.object(engine.database, 'get_stat_requirement_characters', return_value=['VADER']), \
                patch.object(engine.database, 'get_stat_requirements', return_value=[row('Relic', 7), row('Health', (r1+r7)/2)]), \
                patch.object(engine.database, 'get_skill_omicron_tiers', return_value={}), \
                patch.object(engine.database, 'get_game_unit_name', side_effect=lambda b: b):
            results, passed, _, _ = await engine.optimize(None, real_calc, ['P'], '123', [], 1,
                                                        units_override={'VADER': unit}, relic_mode='plate')
            self.assertEqual(passed, ['P'])
            result = results[0]
            self.assertTrue(result['projected'])
            self.assertEqual((result['current_relic'], result['target_relic']), (1, 7))
            health = next(r for r in result['passed'] if r['stat_name'] == 'Health')
            self.assertEqual((health['before'], health['now'], health['current']), (r1, r1, r7))
            self.assertFalse(health['now_ok'])
            self.assertEqual(actions.operations([], results, set()), [])
            _, current_passed, _, _ = await engine.optimize(None, real_calc, ['P'], '123', [], 1,
                                                          units_override={'VADER': unit}, relic_mode='current')
            self.assertEqual(current_passed, [])

    async def test_donor_damage_and_character_lock_end_to_end(self):
        mods = [mod(f'A{s}', s, 'A', speed=5) for s in range(1, 7)] + [mod(f'D{s}', s, 'D', speed=20) for s in range(1, 7)]
        units = {'A': {'currentTier': 13}, 'D': {'currentTier': 13}}
        with patch.object(engine.database, 'get_stat_requirement_characters', return_value=['A']), \
                patch.object(engine.database, 'get_stat_requirements', return_value=[row(value=190)]), \
                patch.object(engine.database, 'get_skill_omicron_tiers', return_value={}), \
                patch.object(engine.database, 'get_game_unit_name', side_effect=lambda b: b), \
                patch.object(engine.stat_engine, 'calc_final_stats', calc):
            results, passed, _, donors = await engine.optimize(None, None, ['P'], '123', mods, 1, units_override=units)
            self.assertEqual(passed, ['P'])
            self.assertGreater(len(donors), 0)
            self.assertTrue(any(d['removed'] for d in donors))
            self.assertTrue(results[0]['ok'])
            results, passed, _, donors = await engine.optimize(None, None, ['P'], '123', mods, 1, locked=['D'], units_override=units)
            self.assertEqual(passed, [])
            self.assertEqual(donors, [])
            self.assertTrue(all(m['equipped_to'] == 'A' for m in results[0]['used_mods']))
            results, passed, _, donors = await engine.optimize(None, None, ['P'], '123', mods, 1, locked=['A'], units_override=units)
            self.assertTrue(results[0]['locked'])
            self.assertEqual({m['id'] for m in results[0]['used_mods']}, {f'A{s}' for s in range(1, 7)})
            self.assertEqual(donors, [])


class ApplicationTests(unittest.IsolatedAsyncioTestCase):
    def test_swap_cycles_are_removed_before_equipping_and_donors_included(self):
        mods = [mod('a', owner='A'), mod('b', owner='B')]
        results = [{'owned': True, 'base_id': 'A', 'used_mods': [mods[1]]},
                   {'owned': True, 'base_id': 'B', 'used_mods': [mods[0]]}]
        ops = actions.operations(mods, results, set())
        self.assertEqual([bool(op['remove']) for op in ops], [True, True, False, False])
        with self.assertRaises(ga.AccountError):
            actions.operations(mods, results, {'B'})

    def test_locked_mod_cannot_be_displaced(self):
        old = mod('old', owner='A', locked=True)
        new = mod('new')
        with self.assertRaises(ga.AccountError):
            actions.operations([old, new], [{'owned': True, 'base_id': 'A', 'used_mods': [new]}], set())

    def test_request_uses_instance_id_and_only_fixed_mod_rpc(self):
        raw = ga.pack([(4, 2, b'PlayerRpc'), (5, 2, b'GetInitialData'), (6, 2, b''), (7, 2, b'player'), (8, 2, b'token')])
        snap = {'unit_ids': {'A': 'instance'}, '_raw': raw, '_expected': 'player', '_is_current': lambda: True}
        fields = ga.fields(actions.request(snap, {'unit': 'A', 'equip': ['mod'], 'remove': ['old']}))
        self.assertEqual(ga._one(fields, 4, 2), b'StatModRpc')
        self.assertEqual(ga._one(fields, 5, 2), b'UpdateMods')
        self.assertEqual(ga.fields(ga._one(fields, 6, 2)), [(1, 2, b'instance'), (2, 2, b'mod'), (3, 2, b'old')])

    async def test_stale_plan_sends_no_mutation(self):
        with patch.object(actions, 'snapshot', AsyncMock(return_value={'connection': 'c', 'fingerprint': 'new'})), \
                patch.object(ga, '_read_payload', AsyncMock()) as transport:
            with self.assertRaises(ga.AccountError):
                await actions.apply({}, '123', {'connection': 'c', 'fingerprint': 'old'}, lambda _: None)
        transport.assert_not_awaited()

    async def test_success_and_unverified_response_stop_without_retry(self):
        initial = [mod('a', owner='A'), mod('b')]
        progress = {'A': 'p'}
        def snap(mods):
            return {'mods': copy.deepcopy(mods), 'progress': progress, 'connection': 'c',
                    'fingerprint': plans.fingerprint(mods, progress)}
        removed = copy.deepcopy(initial)
        removed[0]['equipped_to'] = None
        equipped = copy.deepcopy(removed)
        equipped[1]['equipped_to'] = 'A'
        data = {'connection': 'c', 'fingerprint': plans.fingerprint(initial, progress), 'locked': [],
                'results': [{'owned': True, 'base_id': 'A', 'used_mods': [initial[1]]}]}
        with patch.object(actions, 'snapshot', AsyncMock(side_effect=[snap(initial), snap(removed), snap(removed), snap(equipped)])), \
                patch.object(actions, 'request', return_value=b'fixed'), patch.object(ga, '_read_payload', AsyncMock()) as transport:
            result = await actions.apply({}, '123', copy.deepcopy(data), lambda _: None)
        self.assertTrue(result['verified'])
        self.assertEqual(transport.await_count, 2)
        with patch.object(actions, 'snapshot', AsyncMock(side_effect=[snap(initial), snap(initial)])), \
                patch.object(actions, 'request', return_value=b'fixed'), patch.object(ga, '_read_payload', AsyncMock()) as transport:
            bad = copy.deepcopy(data)
            with self.assertRaises(ga.AccountError):
                await actions.apply({}, '123', bad, lambda _: None)
        self.assertEqual(transport.await_count, 1)
        self.assertTrue(bad['in_flight'])


if __name__ == '__main__':
    unittest.main()
