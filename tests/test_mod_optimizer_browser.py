import asyncio
from copy import deepcopy
import json
from pathlib import Path
import unittest
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient
from swgoh_comlink import StatCalc
from services import mod_optimizer_browser as browser, mod_optimizer_v2 as engine
from services import mod_optimizer_plans as plans
from tests.test_mod_optimizer_v2 import mod, row
import tests.test_mod_optimizer_web as web_tests
from web.routes import mod_optimizer as route


def prepared():
    return dict(members={'P': ['A']}, rows={'A': {'P': [row(value=100)]}},
        units={'A': {'definitionId': 'VADER:SEVEN_STAR', 'currentRarity': 7, 'currentLevel': 85,
                     'currentTier': 13, 'relic': {'currentTier': 3}, 'skill': [], 'equipment': []}},
        tiers={}, mods=[mod(str(s), s, 'A') for s in range(1, 7)], names={'A': 'Вейдер'},
        locked=[], relic_mode='current', turn_order=[], deep_search=False)


class AllocationChecks(unittest.TestCase):
    def setUp(self):
        self.calc = StatCalc(json.loads((Path(__file__).parent/'fixtures/mod_optimizer_statcalc.json').read_text()))
        self.p = prepared()
        self.ids = {'A': [m['id'] for m in self.p['mods']]}

    def test_browser_code_is_same_statcalc_and_evaluator(self):
        namespace = {}
        exec(browser.python_source(), namespace)
        actual = namespace['final_stats'](namespace['StatCalc'](browser.calculator_data(self.calc)), self.p['units']['A'], self.p['mods'])
        expected = engine.final_stats(self.calc, self.p['units']['A'], self.p['mods'])
        self.assertEqual(actual, expected)
        self.assertEqual(namespace['evaluate'](self.p['rows']['A'], self.p['units']['A'], self.p['mods'], actual, {}),
                         engine.evaluate(self.p['rows']['A'], self.p['units']['A'], self.p['mods'], expected, {}))

    def test_recomputed_exact_report_and_reject_duplicate_foreign_and_wrong_slots(self):
        results, passed, _ = browser.assess(self.calc, self.p, self.ids)
        self.assertTrue(results[0]['ok'])
        self.assertEqual(passed, ['P'])
        for ids in ({'A': ['unknown']}, {'A': ['1', '1']}, {'B': ['1']}, []):
            with self.subTest(ids=ids), self.assertRaises(ValueError):
                browser.assess(self.calc, self.p, ids)
        self.p['mods'].append(mod('extra', 1))
        with self.assertRaises(ValueError):
            browser.assess(self.calc, self.p, {'A': ['1', 'extra']})

    def test_locks_and_g12_cannot_be_bypassed(self):
        self.p['locked'] = ['A']
        with self.assertRaises(ValueError):
            browser.assess(self.calc, self.p, {'A': []})
        self.p['locked'] = []
        self.p['mods'][0]['locked'] = True
        with self.assertRaises(ValueError):
            browser.assess(self.calc, self.p, {'A': self.ids['A'][1:]})
        self.p['mods'][0]['locked'] = False
        self.p['mods'][0]['rarity'] = 6
        self.p['units']['A']['currentTier'] = 11
        with self.assertRaises(ValueError):
            browser.assess(self.calc, self.p, self.ids)


class BrowserRoutes(unittest.TestCase):
    setUp = web_tests.OptimizerWebTests.setUp
    tearDown = web_tests.OptimizerWebTests.tearDown
    csrf = web_tests.OptimizerWebTests.csrf

    def make_job(self):
        data = {'selected_plates': ['P'], 'requirements_fingerprint': 'f', 'browser_inputs': prepared(),
                'browser_engine': True, 'dry_run': True, 'game_source': False, 'locked': [], 'relic_mode': 'current'}
        token = plans.create('owner', 1, '123456789', {})
        plans.finish(token, 'browser', data)
        return token

    def test_result_csrf_ownership_expiry_and_exact_validation(self):
        token = self.make_job()
        calc = StatCalc(json.loads((Path(__file__).parent/'fixtures/mod_optimizer_statcalc.json').read_text()))
        with TestClient(self.app) as client:
            csrf = self.csrf(client)
            message = {'plan': token, 'csrf': csrf, 'allocation': {'A': ['1', '2', '3', '4', '5', '6']}}
            self.assertEqual(client.post('/mod-optimizer/browser/result', json={**message, 'csrf': 'bad'}).status_code, 403)
            with patch.object(plans, 'requirements_fingerprint', return_value='f'), \
                    patch.object(route.stat_forecast, 'get_stat_calc', new_callable=AsyncMock, return_value=calc):
                self.assertEqual(client.post('/mod-optimizer/browser/result', json={**message, 'allocation': {'A': ['fake']}}).status_code, 400)
                self.assertEqual(client.post('/mod-optimizer/browser/result', json=message).status_code, 200)
                self.assertEqual(client.post('/mod-optimizer/browser/result', json=message).status_code, 400)
        job = plans.get(token, 'owner', 1)
        self.assertEqual(job['state'], 'ready')
        self.assertNotIn('browser_inputs', job['data'])

    def test_cancel_releases_single_active_job(self):
        token = self.make_job()
        with self.assertRaises(ValueError):
            plans.create('owner', 1, '123456789', {})
        with TestClient(self.app) as client:
            csrf = self.csrf(client)
            result = client.post('/mod-optimizer/browser/result', json={'plan': token, 'csrf': csrf, 'cancel': True})
        self.assertEqual(result.status_code, 200)
        self.assertEqual(plans.get(token, 'owner', 1)['state'], 'error')
        self.assertTrue(plans.create('owner', 1, '123456789', {}))

    def test_changed_requirements_reject_browser_output(self):
        token = self.make_job()
        with TestClient(self.app) as client, patch.object(plans, 'requirements_fingerprint', return_value='changed'):
            csrf = self.csrf(client)
            self.assertEqual(client.post('/mod-optimizer/browser/result', json={
                'plan': token, 'csrf': csrf, 'allocation': {}}).status_code, 400)

    def test_prod_always_uses_browser_even_when_submitted_server(self):
        self.user['bot_profile'] = 'prod'
        with TestClient(self.app) as client, patch('services.bot_profiles.guild_profile', return_value='prod'):
            csrf = self.csrf(client)
            captured = []
            def spawn(coroutine):
                captured.append(coroutine)
            with patch.object(route, '_spawn', side_effect=spawn):
                response = client.post('/mod-optimizer', data={'csrf': csrf, 'source': 'game', 'plates': 'P',
                    'ally_code': '123456789', 'engine': 'server'}, follow_redirects=False)
            self.assertEqual(response.status_code, 303)
            # Inspect the coroutine's real arguments without running any game IO.
            inner = captured[0].cr_frame.f_locals
            self.assertEqual(inner['user']['optimizer_engine'], 'browser')
            captured[0].close()
            self.assertFalse(route.can_apply(self.user))

    def test_background_prod_prepares_inputs_without_running_search(self):
        self.user['bot_profile'] = 'prod'
        token = plans.create('owner', 1, '123456789', {})
        p = prepared()
        with patch.object(plans, 'requirements_fingerprint', return_value='f'), \
                patch.object(route.mod_optimizer, 'parse_mods_csv', return_value=p['mods']), \
                patch.object(route.stat_forecast, 'get_stat_calc', new_callable=AsyncMock, return_value=object()), \
                patch.object(route.mod_optimizer_browser, 'runtime_ready', return_value=True), \
                patch.object(engine, 'prepare_inputs', new_callable=AsyncMock,
                             return_value=tuple(p[k] for k in ('members', 'rows', 'units', 'tiers'))), \
                patch.object(engine, 'optimize', new_callable=AsyncMock) as optimize:
            asyncio.run(route._calculate(token, self.user, ['P'], '123456789', 'csv', b'csv', [], False))
        optimize.assert_not_awaited()
        self.assertEqual(plans.get(token, 'owner', 1)['state'], 'browser')
