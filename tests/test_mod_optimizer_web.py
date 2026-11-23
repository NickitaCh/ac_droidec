import re
import tempfile
from pathlib import Path
import unittest
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.middleware.sessions import SessionMiddleware

from services import game_account as ga, game_mod_actions as actions, mod_optimizer_plans as plans
from services.registration import RegistrationResult
from web.routes import mod_optimizer as route
from web.deps import require_member_access


class OptimizerWebTests(unittest.TestCase):
    def test_restore_lock_error_stays_on_loadout_page(self):
        mod = {'id': 'm1', 'equipped_to': 'A', 'slot_id': 1, 'set_id': 1,
               'set_name': 'Health', 'primary': {'name': 'Health', 'value': 5, 'is_percent': True}}
        token = plans.save_baseline('owner', 1, '123456789', [mod], name='Рейд', units=['A'], scope='all')
        fresh = {'mods': [{**mod, 'equipped_to': 'B', 'locked': True}],
                 'unit_ids': {'A': 'a', 'B': 'b'}, 'fingerprint': 'fresh', 'connection': 'c'}
        with TestClient(self.app) as client, patch.object(actions, 'snapshot', new=AsyncMock(return_value=fresh)):
            response = client.post('/mod-loadouts/restore', data={'csrf': self.csrf(client), 'plan': token})
        self.assertIn('text/html', response.headers['content-type'])
        self.assertIn('модуль заблокирован в игре', response.text)
        self.assertIn('Снимите его блокировку в игре', response.text)
        self.assertIn('action="/mod-loadouts/restore"', response.text)
        self.assertEqual(plans.get(token, 'owner', 1)['state'], 'saved')

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.user = {'discord_id': 'owner', 'guild_id': 1, 'bot_profile': 'test',
                     'tier': 'officer', 'is_super_admin': True, 'username': 'Tester'}
        self.actual_guild_id = 1
        self.context = {'user': self.user, 'plates': ['P'], 'self_ally_code': '123456789',
                        'self_player_name': 'Tester', 'selected_plates': [], 'results': None,
                        'error': None, 'loading': False, 'game_available': True,
                        'accounts': [('123456789', 'Tester')], 'characters': [('A', 'Персонаж')],
                        'locked': [], 'plan': None, 'donors': [], 'can_apply': True, 'dry_run': True}
        self.patches = [patch.object(plans.database, 'DB_NAME', str(Path(self.temp.name)/'plans.db')),
                        patch.object(route, '_base_context', side_effect=lambda *_: dict(self.context)),
                        patch.object(route.database, 'get_player_units', return_value={'A': {}}),
                        patch.object(route.database, 'get_unit_types', return_value={}),
                        patch.object(route.database, 'get_game_unit_names', return_value={'A': 'Персонаж'}),
                        patch.object(ga, 'link_path', return_value='/game-account/link?alt=0'),
                        patch.object(ga, 'select_account', side_effect=lambda *_, ally_code=None: ally_code),
                        patch.object(ga.guild_resolver, 'resolve_access', side_effect=lambda _: {
                            'guild_id': self.actual_guild_id, 'tier': self.user['tier']}),
                        patch('services.bot_profiles.guild_profile', return_value='test'),
                        patch.object(route.game_account_readonly, '_connection', return_value=('target', 'player', b'secret'))]
        for p in self.patches:
            p.start()
        self.app = FastAPI()
        self.app.add_middleware(SessionMiddleware, secret_key='local-test')
        self.app.include_router(route.router, prefix='/mod-optimizer')
        self.app.include_router(route.loadouts_router, prefix='/mod-loadouts')
        self.app.dependency_overrides[require_member_access] = lambda: self.user

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()
        self.temp.cleanup()

    def csrf(self, client):
        response = client.get('/mod-optimizer')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers['cache-control'], 'no-store')
        return re.search('name="csrf" value="([^"]+)"', response.text).group(1)

    def test_form_and_csrf_and_lock_submission(self):
        with TestClient(self.app) as client:
            csrf = self.csrf(client)
            self.assertEqual(client.post('/mod-optimizer', data={'source': 'game', 'plates': 'P'}).status_code, 403)
            def close(coroutine):
                coroutine.close()
            with patch.object(route, '_spawn', side_effect=close) as spawn:
                response = client.post('/mod-optimizer', data={'csrf': csrf, 'source': 'game',
                    'plates': 'P', 'ally_code': '123456789', 'locked': 'A', 'relic_mode': 'plate'}, follow_redirects=False)
            self.assertEqual(response.status_code, 303)
            spawn.assert_called_once()
            token = response.headers['location'].split('plan=')[1]
            job = plans.get(token, 'owner', 1)
            self.assertEqual(job['data']['locked'], ['A'])
            self.assertEqual(job['data']['relic_mode'], 'plate')
            pending = client.get(response.headers['location'])
            self.assertIn('Идёт подбор', pending.text)
            self.assertNotIn('action="/mod-optimizer/apply"', pending.text)

    def test_loadouts_register_alt_and_select_its_saved_items(self):
        code = '987654321'
        result = RegistrationResult(ok=True, ally_code=code, ingame_name='Alt', guild_id=1)
        with TestClient(self.app) as client, patch.object(route, '_get_comlink'), patch.object(
                route, 'register_player', new_callable=AsyncMock, return_value=result) as register:
            page = client.get('/mod-loadouts')
            csrf = re.search('name="csrf" value="([^"]+)"', page.text).group(1)
            self.assertIn('action="/mod-loadouts/alt"', page.text)
            denied = client.post('/mod-loadouts/alt', data={'ally_code': code, 'csrf': 'wrong'})
            self.assertEqual(denied.status_code, 403)
            register.assert_not_called()
            self.context['accounts'].append((code, 'Alt'))
            saved = client.post('/mod-loadouts/alt', data={'ally_code': code, 'csrf': csrf})
            self.assertEqual(saved.status_code, 200)
            self.assertIn(f'value="{code}" selected', saved.text)
            self.assertIn('Альт «Alt» добавлен.', saved.text)
            self.assertEqual(register.await_args.args[1:], ('owner', code))
            self.assertEqual(register.await_args.kwargs, {'is_alt': True})

    def test_other_profile_and_foreign_guild_are_denied(self):
        for changes in ({'guild_id': 2}, {'bot_profile': 'prod'}):
            original = dict(self.user)
            self.user.update(changes)
            with TestClient(self.app) as client, patch.object(route, '_spawn') as spawn:
                self.assertEqual(client.get('/mod-optimizer').status_code, 404)
                self.assertEqual(client.get('/mod-loadouts').status_code, 404)
                self.assertEqual(client.post('/mod-optimizer', data={'plates': 'P'}).status_code, 404)
                self.assertEqual(client.post('/mod-optimizer/apply', data={'plan': 'anything', 'csrf': 'x', 'confirm': '1'}).status_code, 404)
                spawn.assert_not_called()
            self.user.clear()
            self.user.update(original)

    def test_member_of_another_test_guild_can_open_optimizer_with_dry_run(self):
        self.actual_guild_id = 2
        self.user.update(guild_id=2, tier='member', is_super_admin=False)
        with TestClient(self.app) as client:
            csrf = self.csrf(client)
            self.assertEqual(client.post('/mod-optimizer/apply', data={'csrf': csrf, 'plan': 'anything'}).status_code, 400)

    def test_expired_plan_offers_warned_retry_with_preserved_settings(self):
        token = plans.create('owner', 1, '123456789', {})
        plans.finish(token, 'error', {'connection_action': 'login', 'selected_plates': ['P'],
                                      'locked': ['A'], 'relic_mode': 'plate'})
        with TestClient(self.app) as client:
            page = client.get('/mod-optimizer?plan=' + token)
            self.assertEqual(page.status_code, 200)
            self.assertIn(ga.LOGIN_WARNING, page.text)
            self.assertIn('name="source" value="reconnect"', page.text)
            self.assertIn('name="plates" value="P" checked', page.text)
            self.assertIn('name="locked" value="A" checked', page.text)

    def test_ready_report_apply_confirmation_and_one_shot(self):
        token = plans.create('owner', 1, '123456789', {})
        data = {'game_source': True, 'dry_run': False, 'operation_count': 2, 'results': [], 'mods_total': 10,
                'plates_passed': ['P'], 'plates_total': 1, 'selected_plates': ['P'], 'locked': ['A']}
        plans.finish(token, 'ready', data)
        with TestClient(self.app) as client:
            csrf = self.csrf(client)
            ready = client.get('/mod-optimizer?plan=' + token)
            self.assertEqual(ready.status_code, 200)
            self.assertIn('Применить расстановку в игре', ready.text)
            self.assertIn('Заблокированные персонажи · 1', ready.text)
            self.assertEqual(client.post('/mod-optimizer/apply', data={'csrf': csrf, 'plan': token}).status_code, 400)
            with patch.object(route, '_spawn', side_effect=lambda c: c.close()):
                applied = client.post('/mod-optimizer/apply', data={'csrf': csrf, 'plan': token, 'confirm': '1'}, follow_redirects=False)
                self.assertEqual(applied.status_code, 303)
                duplicate = client.post('/mod-optimizer/apply', data={'csrf': csrf, 'plan': token, 'confirm': '1'})
                self.assertEqual(duplicate.status_code, 409)
            self.assertEqual(client.get('/mod-optimizer?plan=foreign').status_code, 404)

    def test_saved_result_survives_job_cleanup_and_baseline_reads_account(self):
        token = plans.create('owner', 1, '123456789', {})
        plans.finish(token, 'ready', {'results': [], 'plates_passed': [], 'plates_total': 0,
                                      'mods_total': 1, 'dry_run': True})
        mod = {'id': 'mod-id', 'equipped_to': 'A', 'slot_id': 1, 'set_id': 1, 'set_name': 'Health',
               'primary': {'name': 'Health', 'unit_stat_id': 1, 'is_percent': True, 'value': 5}}
        with TestClient(self.app) as client:
            csrf = self.csrf(client)
            saved = client.post('/mod-optimizer/save', data={'csrf': csrf, 'plan': token}, follow_redirects=False)
            self.assertEqual(saved.status_code, 303)
            self.assertTrue(saved.headers['location'].startswith('/mod-loadouts?plan=saved-'))
            with plans._db() as db:
                db.execute('DELETE FROM mod_optimizer_plans WHERE id=?', (token,))
            saved_page = client.get(saved.headers['location'])
            self.assertIn('<h1>Подборки модулей</h1>', saved_page.text)
            self.assertIn('Результат подбора', saved_page.text)
            self.assertNotIn('id="mo-run"', saved_page.text)
            legacy = client.get('/mod-optimizer?plan=saved-' + token, follow_redirects=False)
            self.assertEqual(legacy.headers['location'], saved.headers['location'])
            with self.assertRaises(ValueError):
                plans.get('saved-' + token, 'other', 1)
            with patch.object(actions, 'snapshot', new=AsyncMock(return_value={'mods': [mod]})) as snapshot:
                baseline = client.post('/mod-optimizer/baseline', data={'csrf': csrf, 'ally_code': '123456789'})
            snapshot.assert_awaited_once_with(self.user, '123456789', read_only=True)
            self.assertEqual(baseline.status_code, 200)
            self.assertIn('Baseline · 1 модулей', baseline.text)
            self.assertIn('Персонаж', baseline.text)

    def test_baseline_expiry_preserves_selection_and_requires_explicit_reconnect(self):
        with TestClient(self.app) as client, patch.object(ga, 'reconnect', new_callable=AsyncMock) as reconnect:
            data = {'csrf': self.csrf(client), 'ally_code': '123456789',
                    'characters': 'A', 'name': 'Рейд'}
            with patch.object(actions, 'snapshot', new=AsyncMock(side_effect=ga.SessionExpired('Войдите в игру заново.'))):
                page = client.post('/mod-optimizer/baseline', data=data)
            self.assertEqual(page.status_code, 200)
            self.assertIn('Войти заново и сохранить раскладку', page.text)
            self.assertIn('value="Рейд"', page.text)
            self.assertIn('name="characters" value="A" checked', page.text)
            reconnect.assert_not_awaited()
            with patch.object(actions, 'snapshot', new=AsyncMock(return_value={'mods': [], 'unit_ids': {'A': 'a'}})):
                saved = client.post('/mod-optimizer/baseline', data={**data, 'reconnect': 'true'},
                                    follow_redirects=False)
            self.assertEqual(saved.status_code, 303)
            reconnect.assert_awaited_once_with('owner', 1, 'test', self.user['tier'], '123456789',
                                               optimizer_read=True)

    def test_named_preset_preview_restores_current_copies_and_requires_confirmation(self):
        mod = {'id': 'm1', 'equipped_to': 'A', 'slot_id': 1, 'set_id': 1, 'set_name': 'Health',
               'primary': {'name': 'Health', 'unit_stat_id': 1, 'is_percent': True, 'value': 5}}
        snap = {'unit_ids': {'A': 'a', 'B': 'b'}, 'mods': [mod], 'fingerprint': 'old', 'connection': 'same'}
        with TestClient(self.app) as client, patch.object(ga, 'require_pilot'):
            csrf = self.csrf(client)
            with patch.object(actions, 'snapshot', new=AsyncMock(return_value=snap)):
                saved = client.post('/mod-optimizer/baseline', data={
                    'csrf': csrf, 'ally_code': '123456789', 'characters': 'A', 'name': 'Рейд',
                }, follow_redirects=False)
            self.assertEqual(saved.status_code, 303)
            token = saved.headers['location'].split('plan=')[1]
            fresh = {**snap, 'fingerprint': 'fresh', 'mods': [{**mod, 'equipped_to': 'B'}]}
            with patch.object(actions, 'snapshot', new=AsyncMock(return_value=fresh)), \
                    patch.object(actions, 'apply', new=AsyncMock()) as apply:
                preview = client.post('/mod-optimizer/restore', data={'csrf': csrf, 'plan': token})
                self.assertEqual(preview.status_code, 200)
                self.assertIn('Переодевание · Рейд', preview.text)
                self.assertIn('B — снять 1', preview.text)
                self.assertIn('Персонаж — надеть 1', preview.text)
                draft = str(preview.url).split('plan=')[1]
                self.assertEqual(client.post('/mod-optimizer/apply', data={'csrf': csrf, 'plan': draft}).status_code, 400)
                apply.assert_not_awaited()
                with patch.object(route, '_spawn', side_effect=lambda coroutine: coroutine.close()):
                    applied = client.post('/mod-loadouts/apply', data={'csrf': csrf, 'plan': draft, 'confirm': '1'},
                                          follow_redirects=False)
                    self.assertEqual(applied.headers['location'], '/mod-loadouts?plan=' + draft)
            data = plans.get(draft, 'owner', 1)['data']
            self.assertEqual(data['fingerprint'], 'fresh')
            self.assertEqual(data['results'][0]['used_mods'][0]['equipped_to'], 'B')
            with patch.object(actions, 'snapshot', new=AsyncMock(return_value={**fresh, 'mods': []})):
                self.assertEqual(client.post('/mod-optimizer/restore', data={'csrf': csrf, 'plan': token}).status_code, 409)

    def test_refresh_and_save_different_loadouts_after_in_game_changes(self):
        mod = {'id': 'm1', 'equipped_to': 'A', 'slot_id': 1, 'set_id': 1, 'set_name': 'Health',
               'primary': {'name': 'Health', 'unit_stat_id': 1, 'is_percent': True, 'value': 5}}
        units = {'A': 'a', 'B': 'b', 'SHIP': 'ship'}
        first = {'unit_ids': units, 'mods': [mod, {**mod, 'id': 'loose', 'equipped_to': None}]}
        second = {'unit_ids': units, 'mods': [{**mod, 'equipped_to': 'B'}]}
        with TestClient(self.app) as client, patch.object(route.database, 'get_unit_types', return_value={'SHIP': 'ship'}):
            form = {'csrf': self.csrf(client), 'ally_code': '123456789'}
            optimizer = client.get('/mod-optimizer')
            self.assertIn('href="/mod-loadouts"', optimizer.text)
            self.assertNotIn('Сохранённые подборки', optimizer.text)
            loadouts = client.get('/mod-loadouts')
            self.assertIn('action="/mod-loadouts/baseline"', loadouts.text)
            self.assertNotIn('Параметры подбора', loadouts.text)
            with patch.object(actions, 'snapshot', new=AsyncMock(side_effect=[first, second, second])) as read:
                self.assertEqual(client.post('/mod-loadouts/baseline', data={**form, 'scope': 'selected'}).status_code, 409)
                full = client.post('/mod-loadouts/baseline', data={**form, 'scope': 'all', 'name': 'Обычная'},
                                   follow_redirects=False)
                refresh = client.post('/mod-loadouts/baseline', data={**form, 'scope': 'selected',
                                      'action': 'refresh', 'characters': 'B', 'name': 'Рейд'})
                self.assertIn('name="characters" value="B" checked', refresh.text)
                self.assertNotIn('name="characters" value="SHIP"', refresh.text)
                self.assertIn('· 1/6', refresh.text)
                self.assertEqual(len(plans.saved('owner', 1, '123456789')), 1)
                partial = client.post('/mod-loadouts/baseline', data={**form, 'scope': 'selected',
                                      'characters': 'B', 'name': 'Рейд'}, follow_redirects=False)
                self.assertEqual(read.await_count, 3)
        self.assertEqual((full.status_code, partial.status_code), (303, 303))
        self.assertTrue(full.headers['location'].startswith('/mod-loadouts?plan='))
        original = plans.get(full.headers['location'].split('plan=')[1], 'owner', 1)['data']
        raid = plans.get(partial.headers['location'].split('plan=')[1], 'owner', 1)['data']
        self.assertEqual(original['preset_units'], ['A', 'B'])
        self.assertEqual({mod['id'] for mod in original['baseline']}, {'m1', 'loose'})
        self.assertEqual(raid['preset_units'], ['B'])
        self.assertEqual(raid['baseline'][0]['equipped_to'], 'B')

    def test_partial_restore_of_empty_slots_can_be_previewed_by_member(self):
        self.user.update(tier='member', is_super_admin=False)
        mod = {'id': 'm1', 'equipped_to': 'B', 'slot_id': 1, 'set_id': 1, 'set_name': 'Health',
               'primary': {'name': 'Health', 'unit_stat_id': 1, 'is_percent': True, 'value': 5}}
        token = plans.save_baseline('owner', 1, '123456789', [{**mod, 'id': 'missing', 'equipped_to': 'A'}],
                                    units=['A', 'B'])
        snap = {'unit_ids': {'A': 'a', 'B': 'b'}, 'mods': [mod], 'fingerprint': 'now', 'connection': 'same'}
        with TestClient(self.app) as client, patch.object(actions, 'snapshot', new=AsyncMock(return_value=snap)):
            form = {'csrf': self.csrf(client), 'plan': token, 'scope': 'selected'}
            self.assertEqual(client.post('/mod-loadouts/restore', data={**form, 'characters': 'C'}).status_code, 409)
            preview = client.post('/mod-loadouts/restore', data={**form, 'characters': 'B'})
            self.assertEqual(preview.url.path, '/mod-loadouts')
            self.assertIn('B — снять 1', preview.text)
            self.assertIn('action="/mod-loadouts/apply"', preview.text)
            draft = str(preview.url).split('plan=')[1]
            data = plans.get(draft, 'owner', 1)['data']
            self.assertEqual(data['results'], [{'owned': True, 'base_id': 'B', 'used_mods': []}])
            self.assertFalse(data['dry_run'])

    def test_member_can_opt_out_of_dry_run_and_apply_with_confirmation(self):
        self.user.update(tier='member', is_super_admin=False)
        with TestClient(self.app) as client:
            csrf = self.csrf(client)
            checkbox = re.search(r'<input[^>]+name="dry_run"[^>]*>', client.get('/mod-optimizer').text).group(0)
            self.assertIn('checked', checkbox)
            self.assertNotIn('disabled', checkbox)
            with patch.object(route, '_spawn', side_effect=lambda c: c.close()):
                posted = client.post('/mod-optimizer', data={'csrf': csrf, 'source': 'game', 'plates': 'P',
                    'ally_code': '123456789', 'dry_run': '0'}, follow_redirects=False)
                self.assertEqual(posted.status_code, 303)
            token = posted.headers['location'].split('plan=')[1]
            data = plans.get(token, 'owner', 1)['data']
            self.assertFalse(data['dry_run'])
            data['game_source'] = True
            plans.finish(token, 'ready', data)
            with patch.object(route, '_spawn', side_effect=lambda c: c.close()) as spawn:
                self.assertEqual(client.post('/mod-optimizer/apply', data={'csrf': csrf, 'plan': token, 'confirm': '1'}, follow_redirects=False).status_code, 303)
                spawn.assert_called_once()

    def test_populated_result_formats_and_partial_status(self):
        result = {'base_id': 'A', 'char_name': 'Персонаж', 'owned': True, 'plate': 'P', 'locked': True,
                  'reason': 'Персонаж заблокирован.', 'current_relic': 1, 'target_relic': 7, 'projected': True, 'used_mods': [], 'moves': [],
                  'passed': [], 'failed': [{'stat_name': 'Speed', 'label': 'Скорость', 'operator': '>=',
                                          'threshold': 200, 'priority': 'required', 'current': 190, 'before': 180, 'now': 185, 'plate': 'P', 'ok': False}]}
        route._enrich_results([result])
        token = plans.create('owner', 1, '123456789', {})
        data = {'game_source': True, 'results': [result], 'mods_total': 10, 'plates_passed': [], 'relic_mode': 'plate',
                'plates_total': 1, 'donors': [{'name': 'Донор', 'removed': 1, 'lost_sets': ['Скорость'],
                                             'changes': [{'stat_name': 'Speed', 'before_fmt': '200', 'current_fmt': '170'}]}],
                'completed_steps': 1, 'in_flight': {'step': 2, 'total': 3}}
        plans.finish(token, 'ready', data)
        with TestClient(self.app) as client:
            self.csrf(client)
            report = client.get('/mod-optimizer?plan=' + token).text
            self.assertIn('Донор', report)
            self.assertIn('Подбор на R7', report)
            self.assertIn('На R1: 185', report)
            self.assertIn('плейтов по прогнозу', report)
            plans.finish(token, 'partial', data, 'Проверка прервана.', expected='ready')
            partial = client.get('/mod-optimizer?plan=' + token)
            self.assertIn('Применение остановлено', partial.text)
            self.assertNotIn('action="/mod-optimizer/apply"', partial.text)


class SnapshotTests(unittest.IsolatedAsyncioTestCase):
    async def test_expired_snapshot_requests_confirmation_without_logging_in(self):
        user = {'discord_id': 'owner', 'guild_id': 1, 'bot_profile': 'test', 'tier': 'member'}
        with patch.object(plans, 'requirements_fingerprint', return_value='current'), \
                patch.object(plans, 'finish') as finish, \
                patch.object(actions, 'snapshot', side_effect=ga.SessionExpired), \
                patch.object(ga, 'reconnect') as reconnect:
            await route._calculate('job', user, ['P'], '123', 'game', None, [], False)
        reconnect.assert_not_called()
        token, state, data = finish.call_args.args
        self.assertEqual((token, state, data['connection_action']), ('job', 'error', 'login'))

    async def test_confirmed_login_precedes_retry_for_member(self):
        user = {'discord_id': 'owner', 'guild_id': 1, 'bot_profile': 'test', 'tier': 'member'}
        with patch.object(plans, 'requirements_fingerprint', return_value='current'), \
                patch.object(plans, 'finish') as finish, \
                patch.object(ga, 'reconnect', new_callable=AsyncMock) as reconnect:
            async def snapshot(*args, **kwargs):
                reconnect.assert_awaited_once_with('owner', 1, 'test', 'member', '123', optimizer_read=True)
                raise ga.ConnectionRequired()
            with patch.object(actions, 'snapshot', side_effect=snapshot):
                await route._calculate('job', user, ['P'], '123', 'game', None, [], False, reconnect=True)
        _, _, data = finish.call_args.args
        self.assertEqual(data['connection_action'], 'link')

    async def test_calculation_never_applies_and_changed_requirements_reject_apply(self):
        user = {'discord_id': 'owner', 'guild_id': 1, 'bot_profile': 'test', 'tier': 'officer', 'is_super_admin': True}
        with tempfile.TemporaryDirectory() as temp, \
                patch.object(plans.database, 'DB_NAME', str(Path(temp)/'jobs.db')), \
                patch.object(plans, 'requirements_fingerprint', return_value='original') as requirement_hash, \
                patch.object(actions, 'snapshot', AsyncMock(return_value={'mods': [], 'roster': {},
                    'fingerprint': 'snapshot', 'connection': 'connection'})), \
                patch.object(route, '_get_comlink', return_value=object()), \
                patch.object(route.stat_forecast, 'get_stat_calc', AsyncMock(return_value=None)), \
                patch.object(route.mod_optimizer_v2, 'optimize', AsyncMock(return_value=([], [], True, []))), \
                patch.object(actions, 'apply', AsyncMock()) as write:
            token = plans.create('owner', 1, '123', {})
            plans.set_locks('owner', 1, '123', [])
            await route._calculate(token, user, ['P'], '123', 'game', None, [], False, dry_run=False)
            job = plans.get(token, 'owner', 1)
            self.assertEqual(job['state'], 'ready')
            self.assertEqual(job['data']['operation_count'], 0)
            write.assert_not_awaited()
            data = plans.claim(token, 'owner', 1)
            requirement_hash.return_value = 'edited'
            await route._apply(token, user, '123', data)
            write.assert_not_awaited()
            self.assertEqual(plans.get(token, 'owner', 1)['state'], 'error')

    async def test_exact_mod_values_and_private_roster_are_used(self):
        primary = ga.pack([(2, 2, ga.pack([(3, 0, 5), (6, 0, 3_200_000_001)]))])
        mod = ga.pack([(1, 2, b'mod'), (2, 2, b'462'), (4, 0, 15), (5, 0, 3), (12, 2, primary)])
        skill = ga.pack([(1, 2, b'ability'), (2, 0, 7)])
        unit = ga.pack([(1, 2, b'instance'), (2, 2, b'A:SEVEN_STAR'), (8, 0, 7), (9, 0, 85),
                        (16, 2, skill), (19, 0, 13), (21, 2, mod), (22, 2, ga.pack([(1, 0, 9)])), (23, 2, b'ultimate')])
        payload = ga.pack([(1, 2, b'player'), (5, 2, ga.pack([(1, 2, unit)]))])
        raw = ga.pack([(4, 2, b'PlayerRpc'), (5, 2, b'GetInitialData'), (6, 2, b''), (7, 2, b'player'), (8, 2, b'token')])
        import base64
        user = {'discord_id': 'owner', 'guild_id': 1, 'bot_profile': 'test', 'tier': 'officer'}
        with patch.object(ga, 'require_mod_actions'), patch.object(actions.accounts, '_load', return_value=(('target', 'player', b'secret'),
                {'read_request': base64.b64encode(raw).decode()}, lambda: True)), \
                patch.object(ga, '_read_payload', AsyncMock(return_value=payload)), \
                patch.object(actions.game_mod_inventory.database, 'get_game_unit_names', return_value={}):
            snap = await actions.snapshot(user, '123')
        self.assertEqual(snap['mods'][0]['primary']['unscaled'], 3_200_000_001)
        self.assertEqual(snap['mods'][0]['equipped_to'], 'A')
        self.assertEqual(snap['roster']['A']['skill'], [{'id': 'ability', 'tier': 7}])
        self.assertEqual(snap['roster']['A']['purchasedAbilityId'], ['ultimate'])
        self.assertEqual(snap['roster']['A']['relic']['currentTier'], 9)


if __name__ == '__main__':
    unittest.main()
