import asyncio
import base64
import copy
from pathlib import Path
from tempfile import TemporaryDirectory
import time
import unittest
from unittest.mock import AsyncMock, Mock, patch

from services import game_account as ga, tb_commands as orders, tb_live


def state():
    zones = {}
    for branch in (2, 3, 1):
        key = f'tb3_mixed_phase02_conflict0{branch}'
        zones[key] = {'id': key, 'status': 3, 'points': 100, 'command': 1, 'message': '',
                      'missions': [{'id': key + '_strike01', 'status': 3, 'command': 1, 'message': '', 'players': 0}],
                      'special': [], 'operations': [{'id': 'op', 'slots': [{'unit': 'U', 'donor': ''}]}],
                      'recon_command': {'id': key + '_recon01', 'status': 3, 'command': 1, 'message': ''}}
    return {'event': 'event', 'phase': 2, 'end_ms': int((time.time() + 5 * 3600) * 1000), 'zones': zones, 'players': {}}


def raw():
    return ga.pack([(4, 2, b'PlayerRpc'), (5, 2, b'GetInitialData'), (6, 2, b''),
                    (7, 2, b'player'), (8, 2, b'test-token')])


class RulesTests(unittest.TestCase):
    def setUp(self):
        self.state = state()
        self.keys = list(self.state['zones'])
        self.definitions = patch.object(orders.tb_forecast, 'zones', return_value={'planets': {
            'Geonosis': {'stars': [100, 200, 316_000_000]}, 'Felucia': {'stars': [100, 200, 316_000_000]},
            'Bracca': {'stars': [100, 200, 303_000_000]}}})
        self.definitions.start()
        self.addCleanup(self.definitions.stop)
        self.rules = orders.recipe(self.state, {key: 280_000_000 for key in self.keys})

    def test_complete_sequence_and_priority(self):
        def names():
            return [r['name'] for r in orders.winners(self.rules, self.state).values()]
        self.assertEqual(names(), ['Начальный приказ'] * 3)
        self.state['zones'][self.keys[0]]['points'] = 280_000_000
        self.assertEqual(names()[0], 'Стоп: порог очков')
        for zone in self.state['zones'].values():
            zone['points'] = 280_000_000
        self.assertEqual(names(), ['Равномерная заливка'] * 3)
        self.state['zones'][self.keys[0]]['points'] = 311_000_000
        self.assertEqual(names()[0], 'Стоп: конечный буфер')
        self.state['end_ms'] = (time.time() + 3599) * 1000
        self.assertEqual(names(), ['Финальная заливка'] * 3)
        self.state['zones'][self.keys[0]]['points'] = 316_000_000
        self.assertEqual(orders.winners(self.rules, self.state)[self.keys[0]]['name'], 'Стоп: конечный буфер')

    def test_completed_planet_still_satisfies_group_threshold(self):
        for zone in self.state['zones'].values():
            zone['points'] = 280_000_000
        self.state['zones'][self.keys[0]]['status'] = 4
        self.assertTrue(all(r['name'] == 'Равномерная заливка' for r in orders.winners(self.rules, self.state).values()))

    def test_platoons_unknown_or_empty_never_opens_special_mission(self):
        condition = {'enabled': True, 'conditions': [{'kind': 'platoons', 'zone': self.keys[0]}]}
        self.assertFalse(orders.matches(condition, self.state))
        self.state['zones'][self.keys[0]]['operations'][0]['slots'][0]['donor'] = 'p'
        self.assertTrue(orders.matches(condition, self.state))
        self.state['zones'][self.keys[0]]['operations'] = []
        self.assertFalse(orders.matches(condition, self.state))

    def test_focus_can_target_one_battle_and_invalid_threshold_rejected(self):
        target = self.keys[0] + '_strike01'
        self.assertEqual(orders.targets(self.state)[target]['kind'], 'missions')
        with self.assertRaises(ga.AccountError):
            orders.recipe(self.state, {self.keys[0]: 315_000_000})

    def test_wire_schema_from_android_and_identity_guard(self):
        request = ga.fields(orders.request(raw(), 'player', 'event', 'battle1', 2, 'Бить сюда'))
        self.assertEqual(ga._one(request, 5, 2), b'CommandTerritory')
        self.assertEqual(ga.fields(ga._one(request, 6, 2)), [
            (1, 2, b'event'), (2, 2, b'battle1'), (3, 2, 'Бить сюда'.encode()),
            (4, 0, 2), (5, 0, 1), (6, 0, 0)])
        with self.assertRaises(ga.AccountError):
            orders.request(raw(), 'wrong-player', 'event', 'battle1', 2, '')

    def test_live_parser_retains_actual_commands_and_child_ids(self):
        def msg(tag, fields):
            return (tag, 2, ga.pack(fields))
        def zone(key, command, message):
            return [(1, 2, key.encode()), (2, 0, 3), (3, 0, 100), (5, 2, message.encode()), (6, 0, command)]
        key = self.keys[0]
        payload = ga.pack([msg(1, [(1, 2, b'event'), (9, 0, 2), (11, 0, 9000000000000),
                                 msg(5, [msg(5, zone(key, 3, 'Стоп'))]),
                                 msg(6, [(3, 0, 10), msg(5, zone(key + '_strike01', 2, 'Сначала'))]),
                                 msg(7, [msg(6, zone(key + '_recon01', 2, 'Взводы'))]),
                                 msg(13, [msg(1, zone(key + '_covert01', 3, 'Ждать'))])])])
        parsed = tb_live.parse(payload)['zones'][key]
        self.assertEqual((parsed['command'], parsed['message']), (3, 'Стоп'))
        self.assertEqual(parsed['missions'][0]['command'], 2)
        self.assertEqual(parsed['special'][0]['message'], 'Ждать')
        self.assertEqual(parsed['recon_command']['id'], key + '_recon01')


class ExecutionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = patch.object(orders.database, 'DB_NAME', str(Path(self.tmp.name) / 'db.sqlite'))
        self.db.start()
        self.addCleanup(self.db.stop)
        self.auth = patch.object(orders, '_authorize')
        self.auth.start()
        self.addCleanup(self.auth.stop)
        self.user = {'discord_id': 'owner', 'guild_id': 1, 'bot_profile': 'test', 'tier': 'officer'}
        self.state = state()
        self.key = next(iter(self.state['zones']))
        self.rule = {'id': 'rule', 'name': 'Stop', 'priority': 1, 'target': self.key,
                     'command': 3, 'message': '', 'enabled': True, 'conditions': []}
        orders.save_rules(self.user, '123', self.state, [self.rule], True, 0)

    async def test_stale_edit_does_not_write_or_disable_rules(self):
        self.state['zones'][self.key]['command'] = 2
        with patch.object(orders, 'read', AsyncMock(return_value=self.state)), patch.object(ga, '_read_payload', AsyncMock()) as send:
            with self.assertRaisesRegex(ga.AccountError, 'уже изменился'):
                await orders.change(self.user, '123', 'event', 2, self.key, 3, '', [1, ''])
            send.assert_not_awaited()
        self.assertTrue(orders.config(1)['rules'][0]['enabled'])

    async def test_manual_order_is_verified_and_disables_matching_automation(self):
        after = copy.deepcopy(self.state)
        after['zones'][self.key]['command'] = 3
        load = (('target', 'player', b'secret'), {'read_request': base64.b64encode(raw()).decode()}, lambda: True)
        with patch.object(orders, 'read', AsyncMock(side_effect=[self.state, after])), \
                patch.object(accounts := orders.accounts, '_load', return_value=load), \
                patch.object(ga, '_read_payload', AsyncMock(return_value=b'')) as send:
            await orders.change(self.user, '123', 'event', 2, self.key, 3, '', [1, ''])
            send.assert_awaited_once()
        self.assertFalse(orders.config(1)['rules'][0]['enabled'])
        self.assertEqual(orders.history(1)[0]['result'], 'Подтверждён')

    async def test_unconfirmed_write_pauses_automation_without_retry(self):
        load = (('target', 'player', b'secret'), {'read_request': base64.b64encode(raw()).decode()}, lambda: True)
        with patch.object(orders, 'read', AsyncMock(return_value=self.state)), \
                patch.object(orders.accounts, '_load', return_value=load), \
                patch.object(ga, '_read_payload', AsyncMock(side_effect=ga.AccountError('timeout'))) as send:
            with self.assertRaises(ga.AccountError):
                await orders.change(self.user, '123', 'event', 2, self.key, 3, '', [1, ''])
            send.assert_awaited_once()
        self.assertFalse(orders.config(1)['enabled'])
        self.assertEqual(orders.history(1)[0]['result'], 'Не подтверждён')

    async def test_only_bound_owner_runs_and_changed_phase_pauses(self):
        with patch.object(orders, 'read', AsyncMock()) as read:
            await orders.process(1, 'another', self.state)
            read.assert_not_awaited()
            changed = copy.deepcopy(self.state)
            changed['phase'] = 3
            await orders.process(1, 'owner', changed)
            read.assert_not_awaited()
        self.assertFalse(orders.config(1)['enabled'])

    async def test_revision_conflict_and_cross_worker_lock(self):
        with self.assertRaisesRegex(ga.AccountError, 'уже изменились'):
            orders.save_rules(self.user, '123', self.state, [], False, 0)
        async with orders.locked(1):
            with self.assertRaises(orders.Busy):
                async with orders.locked(1):
                    self.fail('Second writer entered')
            await orders.process(1, 'owner', self.state)
            self.assertTrue(orders.config(1)['enabled'])

    async def test_identical_order_does_not_send_again(self):
        self.state['zones'][self.key]['command'] = 3
        with patch.object(orders, 'read', AsyncMock(return_value=self.state)), patch.object(ga, '_read_payload', AsyncMock()) as send:
            await orders.process(1, 'owner', self.state)
            await orders.process(1, 'owner', self.state)
            send.assert_not_awaited()

    async def test_restart_restores_observation_without_login(self):
        from services import game_watch
        with patch.object(game_watch, 'start', AsyncMock()) as start, patch.object(ga, 'reconnect', AsyncMock()) as login:
            await orders.restore()
            start.assert_awaited_once_with('owner', 1, 'test', 'officer', '123', 'tb', 60, saved_session=True)
            login.assert_not_awaited()
        orders.pause_owner(1, 'another', 'ended')
        self.assertTrue(orders.config(1)['enabled'])
        orders.pause_owner(1, 'owner', 'ended')
        self.assertFalse(orders.config(1)['enabled'])

    async def test_lost_officer_access_prevents_game_read(self):
        with patch.object(orders, '_authorize', side_effect=ga.AccountError('denied')), \
                patch.object(orders.accounts, 'read', AsyncMock()) as read:
            with self.assertRaises(ga.AccountError):
                await orders.read(self.user, '123')
            read.assert_not_awaited()

    def test_other_guild_has_no_rules_or_history(self):
        self.assertIsNone(orders.config(2))
        self.assertEqual(orders.history(2), [])
