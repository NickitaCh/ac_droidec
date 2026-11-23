import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from tests import test_mod_recommendations as fixtures
from cogs import stat_requirements as stats
from services import stat_conditions, mod_progress


class ConditionTests(unittest.TestCase):
    def setUp(self):
        fixtures.RecommendationTests.setUp(self)
        database = stats.database
        self.rid = self.rows[0][0]
        database.update_stat_requirement(self.rid, 'Plate', 'HUX', 'Speed', '>=', 300,
                                         'optional', None, guild_id=1)
        database.set_unit_omicron_skills({'IG90': ['unique-ig']})
        database.set_skill_tier_thresholds({'unique-ig': (None, 7, 'Баффы', None, None, None)})
        self.rows = database.get_stat_requirements('Plate', 'HUX', 1)
        self.url = f'/plates/Plate/requirements/{self.rid}/condition'

    def tearDown(self):
        fixtures.RecommendationTests.tearDown(self)

    def test_condition_roundtrip_and_guild_boundary(self):
        from services import mod_optimizer_plans
        db = stats.database
        before = mod_optimizer_plans.requirements_fingerprint(['Plate'], 1)
        self.assertEqual(self.client.post(self.url, data={'base_id': 'IG90', 'skill_id': 'unique-ig'}).status_code, 303)
        self.assertNotEqual(before, mod_optimizer_plans.requirements_fingerprint(['Plate'], 1))
        self.assertEqual(db.get_stat_requirement_conditions([self.rid], 1), {self.rid: ('IG90', 'unique-ig')})
        self.assertEqual(db.get_stat_requirement_conditions([self.rid], 2), {})
        self.assertEqual(db.get_stat_requirements('Plate', 'HUX', 1), self.rows)
        html = self.client.get('/plates/Plate').text
        self.assertIn('Обязательно без омикрона: IG90', html)
        self.assertIn('data-condition-form', html)
        self.assertIn('error=', self.client.post(self.url, data={'base_id': 'HUX', 'skill_id': 'unique-ig'}).headers['location'])
        self.assertEqual(db.get_stat_requirement_conditions([self.rid], 1), {self.rid: ('IG90', 'unique-ig')})
        self.client.post(self.url, data={'clear': '1'})
        self.assertEqual(db.get_stat_requirement_conditions([self.rid], 1), {})

    def test_player_priority_uses_exact_skill_tier_without_mutation(self):
        conditions = {self.rid: ('IG90', 'unique-ig')}
        for unit, expected in [(None, 'required'), ({'skill': []}, 'required'),
                               ({'skill': [{'id': 'unique-ig', 'tier': 6}]}, 'required'),
                               ({'skill': [{'id': 'other', 'tier': 9}]}, 'required'),
                               ({'skill': [{'id': 'unique-ig', 'tier': 7}]}, 'optional')]:
            with self.subTest(unit=unit):
                effective = stat_conditions.apply_priorities(self.rows, conditions, {'IG90': unit})
                self.assertEqual(effective[0][6], expected)
                self.assertEqual(self.rows[0][6], 'optional')
        with self.assertRaises(ValueError):
            stat_conditions.apply_priorities(self.rows, {self.rid: ('IG90', 'unknown')}, {})

    def test_discord_counts_and_progress_keep_player_specific_requirement(self):
        stats.database.set_stat_requirement_condition(self.rid, 'IG90', 'unique-ig', 1)
        keys = []
        for has_omicron in (False, True):
            out = []
            async def get_unit(bot, ally, base_id, refresh):
                if base_id == 'HUX':
                    return {'relic': {'currentTier': 2}, 'skill': []}, 'now'
                return {'skill': [{'id': 'unique-ig', 'tier': 7 if has_omicron else 6}]}, 'now'
            with patch.object(stats, '_get_unit_for_player', get_unit), \
                 patch.object(stats.stat_engine, 'calc_final_stats', return_value={'Speed': 200}):
                result = asyncio.run(stats._evaluate_character_player(SimpleNamespace(stat_calc=None),
                    'Plate', 'HUX', '123456789', False, 'Игрок', 1, rows_out=out))
            self.assertEqual(result[6], 0 if has_omicron else 1)
            self.assertEqual(len(result[5]), 0 if has_omicron else 1)
            self.assertEqual(out[0]['ok'], None if has_omicron else False)
            keys.append(out[0]['key'])
        self.assertEqual(keys[0], keys[1])
        metadata = mod_progress.plate_requirements('Plate', 1)
        self.assertEqual(metadata[0]['priority'], 'required')
        self.assertIn('обязательно без омикрона', metadata[0]['req'])

    def test_condition_survives_rename_and_disappears_with_requirement(self):
        db = stats.database
        db.set_stat_requirement_condition(self.rid, 'IG90', 'unique-ig', 1)
        db.rename_stat_plate('Plate', 'New', guild_id=1)
        self.assertIn(self.rid, db.get_stat_requirement_conditions([self.rid], 1))
        db.delete_stat_requirement(self.rid, guild_id=1)
        self.assertEqual(db.get_stat_requirement_conditions([self.rid], 1), {})

    def test_optimizer_loads_condition_unit_outside_selected_plate(self):
        from services import mod_optimizer_v2
        stats.database.set_stat_requirement_condition(self.rid, 'IG90', 'unique-ig', 1)
        for tier, priority in [(6, 'required'), (7, 'optional')]:
            units = {'HUX': {'skill': []}, 'IG90': {'skill': [{'id': 'unique-ig', 'tier': tier}]}}
            _, rows, loaded, _ = asyncio.run(mod_optimizer_v2.prepare_inputs(
                None, ['Plate'], '123456789', [], 1, units_override=units))
            self.assertIn('IG90', loaded)
            self.assertEqual(rows['HUX']['Plate'][0][6], priority)
