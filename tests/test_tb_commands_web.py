from pathlib import Path
import re
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.middleware.sessions import SessionMiddleware

from services import tb_commands as orders, game_account as ga
from web.routes import tb_commands as web
from test_tb_commands import state


class WebTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.patches = [patch.object(orders.database, 'DB_NAME', str(Path(self.tmp.name) / 'db')),
                        patch.object(web, '_code', return_value='123'), patch.object(orders, '_authorize'),
                        patch.object(web.tb_live, 'history', return_value=[(123, state())]),
                        patch.object(web.tb_forecast, 'zones', return_value={})]
        for p in self.patches:
            p.start()
            self.addCleanup(p.stop)
        self.user = {'discord_id': 'owner', 'guild_id': 1, 'bot_profile': 'test', 'tier': 'officer', 'features': {}}
        app = FastAPI()
        app.add_middleware(SessionMiddleware, secret_key='tb-test')
        app.include_router(web.router)
        app.dependency_overrides[web._access] = lambda: self.user
        self.client = TestClient(app)
        self.addCleanup(self.client.close)

    def csrf(self):
        response = self.client.get('/tb/commands')
        self.assertEqual(response.status_code, 200)
        return re.search(r'name="csrf" value="([^"]+)"', response.text)[1]

    def test_manual_form_passes_original_order_and_uses_no_login(self):
        csrf = self.csrf()
        key = next(iter(state()['zones']))
        with patch.object(orders, 'change', AsyncMock()) as change, patch.object(ga, 'reconnect', AsyncMock()) as login:
            response = self.client.post('/tb/commands', data={'csrf': csrf, 'action': 'manual', 'event': 'event',
                'phase': '2', 'target': key, 'command': '2', 'message': '5', 'old_command': '1', 'old_message': ''},
                follow_redirects=False)
            self.assertEqual(response.status_code, 303)
            self.assertEqual(change.call_args.args[4:], (key, 2, '5', [1, '']))
            login.assert_not_awaited()

    def test_invalid_csrf_does_not_read_or_write_game(self):
        with patch.object(orders, 'read', AsyncMock()) as read, patch.object(orders, 'change', AsyncMock()) as change:
            response = self.client.post('/tb/commands', data={'csrf': 'wrong', 'action': 'refresh'})
            self.assertEqual(response.status_code, 403)
            read.assert_not_awaited()
            change.assert_not_awaited()

    def test_rule_editor_roundtrips_conditions_and_stays_paused(self):
        csrf = self.csrf()
        key = next(iter(state()['zones']))
        with patch.object(orders, 'read', AsyncMock(return_value=state())):
            response = self.client.post('/tb/commands', data={'csrf': csrf, 'action': 'save_rule',
                'event': 'event', 'phase': '2', 'revision': '0', 'name': 'Открыть бой после взводов',
                'priority': '1', 'target': key + '_strike01', 'command': '2', 'message': 'Первым',
                'enabled': 'on', 'platoons': key, 'points:' + key: '296', 'minutes': '60'})
        self.assertEqual(response.status_code, 200)
        cfg = orders.config(1)
        self.assertFalse(cfg['enabled'])
        self.assertEqual(cfg['rules'][0]['conditions'], [{'kind': 'points', 'zone': key, 'value': 296_000_000},
            {'kind': 'remaining', 'value': 3600}, {'kind': 'platoons', 'zone': key}])
        self.assertIn('Первым', response.text)

    def test_start_reuses_session_and_recipe_is_not_activated_on_save(self):
        csrf = self.csrf()
        rule = {'id': 'r', 'name': 'Бой', 'priority': 1, 'target': next(iter(state()['zones'])) + '_strike01',
                'command': 2, 'message': '', 'enabled': True, 'conditions': []}
        orders.save_rules(self.user, '123', state(), [rule], False, 0)
        with patch.object(orders, 'read', AsyncMock(return_value=state())), \
                patch.object(web.game_watch, 'start', AsyncMock()) as start:
            response = self.client.post('/tb/commands', data={'csrf': csrf, 'action': 'enable',
                'event': 'event', 'phase': '2', 'revision': '1'}, follow_redirects=False)
        self.assertEqual(response.status_code, 303)
        self.assertTrue(orders.config(1)['enabled'])
        self.assertEqual(start.call_args.kwargs, {'saved_session': True})


class DiscordTests(unittest.IsolatedAsyncioTestCase):
    async def test_large_battle_list_has_paging_and_focus_targets_individual_battle(self):
        from services.tb_command_discord import OrderEditor
        from cogs.event_panels import _view
        import disnake
        self.assertTrue(any(i.custom_id == 'glp:tb:token:commands' for i in _view('tb', 'token').children))
        s = state()
        key = next(iter(s['zones']))
        s['zones'][key]['missions'] *= 25
        for n, row in enumerate(s['zones'][key]['missions']):
            s['zones'][key]['missions'][n] = dict(row, id=key + f'_strike{n:02}')
        editor = OrderEditor({'discord_id': '1'}, '123', s)
        selects = [i for i in editor.children if isinstance(i, disnake.ui.StringSelect)]
        self.assertLessEqual(len(selects[0].options), 20)
        self.assertTrue(any('Другие цели' in (getattr(i, 'label', '') or '') for i in editor.children))
        editor.target = key + '_strike01'
        editor._load_target()
        editor.command = 2
        self.assertIn('Концентрация', editor.text())
