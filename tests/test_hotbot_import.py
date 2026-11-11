import json
from pathlib import Path
import unittest

from services import hotbot_import as h


class HotbotImportTests(unittest.TestCase):
    def setUp(self):
        self.names = ['Cassian Andor (Undercover)', 'Luthen Rael', 'Kleya Marki',
                      'Cinta Kaz', 'Vel Sartha']
        self.by_name = {name: name for name in self.names}

    def parse(self, line):
        return h.parse_line(line, self.names, self.by_name)

    def test_real_compact_message(self):
        messages = json.loads((Path(__file__).parent / 'fixtures/hotbot_compact.json').read_text(encoding='utf-8'))
        parsed = h.parse_messages(messages)
        self.assertEqual(parsed['title'], 'AC_CASSIAN')
        self.assertEqual(parsed['player'], 'TEO Nod')
        self.assertEqual([s['name'] for s in parsed['sections']],
                         ['КАССИАН', 'ЛЮТЕН', 'КЛЕЯ', 'СИНТА_КАЗ', 'ВЕЛ_САРТА'])
        results = [self.parse(line) for s in parsed['sections'] for line in s['lines']]
        rejected = [r for r in results if isinstance(r, str)]
        self.assertEqual(len(results), 45)
        self.assertEqual(len(rejected), 1)
        self.assertIn('число омикронов', rejected[0])

    def test_threshold_and_priority_are_taken_from_requirement(self):
        r = self.parse('🔸 Luthen Rael Speed: 233 (optional 340)')
        self.assertEqual((r['stat'], r['value'], r['priority']), ('Speed', 340, 'optional'))
        r = self.parse('❌ Luthen Rael TotLife: 30,293 (requires 120,000)')
        self.assertEqual((r['stat'], r['value']), ('Total Life', 120000))
        r = self.parse('🔸 Vel Sartha Def: 44.69 (optional 65)')
        self.assertEqual((r['stat'], r['value']), ('Armor', 65))

    def test_mod_targets_ignore_equipped_values(self):
        r = self.parse('🔸 Kleya Marki Cross primary: Potency (optional Offense)')
        self.assertEqual((r['slot'], r['ids']), ('cross', [48]))
        r = self.parse('🔸 Kleya Marki mod sets: Health, Critical Damage (optional Offense, Health)')
        self.assertEqual(r['sets'], {2: 1, 1: 1})

    def test_legacy_format(self):
        r = self.parse('❌ (Optional)Luthen Rael Speed[233] is not >= 340')
        self.assertEqual((r['value'], r['priority']), (340, 'optional'))
        r = self.parse('✅ Luthen Rael [Health, Speed] Mod Set Only Health, Speed')
        self.assertEqual(r['stat'], h.STAT_SET_ONLY)


if __name__ == '__main__':
    unittest.main()
