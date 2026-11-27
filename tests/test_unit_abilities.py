import unittest
from unittest.mock import patch

from services import unit_abilities as u
from services import unit_effects
from services import mention_assistant as a


class AbilityTests(unittest.TestCase):
    def test_effect_index_records_linked_abilities_and_merges_same_effect_labels(self):
        effect_rows = [
            {'id': 'wrapper', 'effectReference': [{'id': 'buff'}]},
            {'id': 'buff', 'descriptiveTag': [{'tag': 'featcounter_potency_buff'}, {'tag': 'countable_buff'}]},
            {'id': 'copy', 'persistentLocKey': 'label', 'descriptiveTag': [
                {'tag': 'featcounter_test_buff'}, {'tag': 'countable_buff'}]},
        ]
        abilities = [
            {'id': 'basic', 'tier': [{'effectReference': [{'id': 'wrapper'}]}]},
            {'id': 'special', 'tier': [{'effectReference': [{'id': 'copy'}]}]},
        ]
        skills = [{'id': row['id'], 'abilityReference': row['id']} for row in abilities]
        units = [{'baseId': 'A', 'skillReference': [{'skillId': row['id']} for row in skills]}]
        loc = {'FEATCOUNTER_POTENCY_UP': 'Повышение эффективности', 'label': 'Повышение эффективности'}
        result = unit_effects.parse(effect_rows, abilities, skills, units, loc)
        self.assertEqual(len(result), 1)
        self.assertEqual(next(iter(result.values()))['units'], ['A'])
        self.assertEqual(next(iter(result.values()))['abilities'], {'A': ['basic', 'special']})

    def fixture(self):
        return {'updated_at': 1795528800, 'units': {'SUPREMELEADERKYLOREN': {
            'name': 'Верховный лидер Кайло Рен', 'abilities': [
                {'id': 'basic', 'name': 'Жестокая атака', 'description': 'Наносит урон и получает преимущество.'},
                {'id': 'special', 'name': 'Стазисный удар', 'description': 'Оглушает врага. От этой атаки нельзя увернуться.'},
                {'id': 'ultimate', 'name': 'Глубины ярости', 'description': 'Наносит урон всем врагам (от этой атаки нельзя увернуться).'},
            ]}}}

    def test_parser_uses_latest_description_and_unit_links_including_ultimate(self):
        units = [{'baseId': 'A', 'obtainable': True, 'obtainableTime': '0', 'nameKey': 'unit',
                  'skillReference': [{'skillId': 's'}], 'limitBreakRef': [{'abilityId': 'ult'}]},
                 {'baseId': 'NPC', 'obtainable': False, 'obtainableTime': '0'}]
        skills = [{'id': 's', 'abilityReference': 'ability'}]
        abilities = [{'id': 'ability', 'nameKey': 'name', 'descKey': 'old', 'tier': [{'descKey': 'new'}]},
                     {'id': 'ult', 'nameKey': 'ultimate_name', 'descKey': 'ultimate_text'}]
        loc = {'unit': 'Персонаж', 'name': 'Атака', 'old': 'Старый текст',
               'new': '[c][FFA500]Новый[-][/c]\\n\\nТекст', 'ultimate_name': 'Ульта', 'ultimate_text': 'Без промаха.'}
        parsed = u.parse(units, skills, abilities, loc)
        self.assertEqual(set(parsed), {'A'})
        self.assertEqual(parsed['A']['abilities'][0]['description'], 'Новый\n\nТекст')
        self.assertEqual(len(parsed['A']['abilities']), 2)

    def test_slkr_dodge_question_returns_only_explicit_game_evidence_without_ai(self):
        data = self.fixture()
        with patch.object(u, 'load', return_value=data), patch.object(u.unit_aliases, 'aliases', return_value=('SLKR','слкр')), patch.object(a.openrouter_router, 'call_json') as model:
            plan, _, _ = a.parse_plan('какими скилами слкр не промахивается?', 'test', 'key', {})
        model.assert_not_called()
        self.assertEqual(plan['ids'], ['special', 'ultimate'])
        pages = u.evidence_pages(plan, data, 'какими скилами слкр не промахивается?')
        self.assertEqual(len(pages), 1)
        self.assertIn('Стазисный удар', pages[0])
        self.assertIn('Глубины ярости', pages[0])
        self.assertNotIn('Жестокая атака', pages[0])
        self.assertNotIn('преимущество', pages[0])

    def test_model_cannot_invent_source_id(self):
        context = self.fixture()['units']
        self.assertFalse(a.validate_plan({'action':'abilities','unit':'SUPREMELEADERKYLOREN','ids':['invented']}, {}, [], context))
        self.assertFalse(a.validate_plan({'action':'abilities','unit':'OTHER','ids':['special']}, {}, [], context))
        self.assertTrue(a.validate_plan({'action':'abilities','unit':'SUPREMELEADERKYLOREN','ids':['special']}, {}, [], context))

    def test_jokes_are_not_overridden_by_ability_search(self):
        self.assertIsNone(a.local_plan('Расскажи анекдот про СЛКР, который не промахивается'))


if __name__ == '__main__':
    unittest.main()
