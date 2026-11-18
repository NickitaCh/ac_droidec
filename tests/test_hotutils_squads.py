import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import database
import tw_def_engine as engine
from services import hotutils_squads, tw_def_library


class HotUtilsSquadsTests(unittest.TestCase):
    def sample(self):
        return {'name': 'Test', 'categories': ['TW - Defense'], 'definition': {
            'structure': 'char5', 'slots': [{'unit': 'A'}, {'flexGroup': 'X'}],
            'flexGroups': {'X': {'candidates': ['B', 'C'], 'rules': [
                {'type': 'requirement', 'unit': 'B', 'requires': 'relic', 'minTier': 6},
                {'type': 'requirement', 'unit': 'B', 'requires': 'omicron', 'abilityId': 'unique_B'}]}}}}

    def test_alternatives_and_exact_omicron_keep_unit_specific_requirements(self):
        source = self.sample()
        squad = hotutils_squads.convert(source)
        slots, errors = engine.normalize_slots(squad['slots'], 'character', {'A': 'character', 'B': 'character', 'C': 'character'})
        self.assertFalse(errors)
        self.assertEqual(slots[1]['options'], [{'base_id': 'B', 'min_relic': 6, 'required_omicrons': ['unique_B']}, {'base_id': 'C'}])
        roster = engine.Roster(['p'], {}, {'p': {'B': {'currentTier': 13, 'relic': {'currentTier': 8},
            'skill': [{'id': 'unique_other', 'tier': 8}]}}}, {}, {}, {'unique_B': (None, 8, 'ВГ'), 'unique_other': (None, 8, 'ВГ')})
        self.assertIn('омикрона', engine.check_option(roster, 'p', slots[1]['options'][0], 'character'))
        roster.units['p']['B']['skill'].append({'id': 'unique_B', 'tier': 8})
        self.assertIsNone(engine.check_option(roster, 'p', slots[1]['options'][0], 'character'))

    def test_fleet_does_not_invent_seven_star_requirement(self):
        squad = hotutils_squads.convert({'name': 'Fleet', 'definition': {'structure': 'fleet', 'slots': [{'unit': 'SHIP'}]}})
        self.assertEqual(squad['slots'][0]['options'][0]['min_stars'], 1)

    def test_unknown_requirements_fail_instead_of_silent_loss(self):
        source = self.sample()
        source['definition']['slots'][0]['requirements'] = [{'requires': 'unknown'}]
        with self.assertRaises(ValueError):
            hotutils_squads.convert(source)

    def test_import_is_idempotent_and_keeps_original_recommendations(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(database, 'DB_NAME', str(Path(tmp) / 'test.db')), \
                patch.object(database, 'get_unit_types', return_value={'A': 'character', 'B': 'character', 'C': 'character'}), \
                patch.object(database, 'get_omicron_capable', return_value=set()), \
                patch.object(database, 'get_game_unit_names', return_value={}):
            source = self.sample()
            source['definition']['datacrons'] = [{'affixes': ['original'], 'required': False}]
            raw = json.dumps({'version': 1, 'format': 'hotutils-squads', 'squads': [source]})
            self.assertEqual(tw_def_library.import_selected(raw, 1, {0}, 'test'), 1)
            self.assertEqual(tw_def_library.import_selected(raw, 1, {0}, 'test'), 0)
            self.assertEqual(database.list_tw_def_squads(2), [])
            conn, cursor = database._tw_def_connect()
            try:
                stored = json.loads(cursor.execute('SELECT source_json FROM squad_import_sources WHERE guild_id=1').fetchone()[0])
                self.assertEqual(stored, source)
            finally:
                conn.close()
