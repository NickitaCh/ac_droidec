import unittest

from services import datacron_costs


class DatacronCostsTests(unittest.TestCase):
    def test_season_recipes_accumulate_and_quantities_are_independent(self):
        tier = [{'id': n, 'upgradeCostRecipeId': f'level-{n}'} for n in range(1, 16)]
        recipes = [{'id': f'level-{n}', 'ingredients': [
            {'id': 'DATACRON_CURRENCY', 'minQuantity': n, 'maxQuantity': n},
            {'id': 'datacron_set_37_upgrade_1', 'minQuantity': 10, 'maxQuantity': 10},
        ]} for n in range(1, 16)]
        season = {'id': 37, 'expirationTimeMs': 1000, 'tier': tier[:9], 'focusedTier': tier}
        costs = datacron_costs.catalog({'datacronSet': [season], 'recipe': recipes}, now_ms=999)
        total = datacron_costs.calculate(costs['37'], {3: 2, 6: 0, 9: 1}, '8, 14')
        self.assertEqual([amount for _, amount in total], [370, 0, 0, 198])
        with self.assertRaises(ValueError):
            datacron_costs.calculate(costs['37'], {3: 1}, '16')
        self.assertFalse(datacron_costs.catalog({'datacronSet': [season], 'recipe': recipes}, now_ms=1000))
