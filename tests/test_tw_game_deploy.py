import asyncio
import copy
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import database
from services import game_account as ga, tw_game_deploy as deploy


def state():
    return {'event_id': 'war', 'round': 0, 'end': int(time.time()) + 1000,
            'own': [{'id': 'tw_jakku01_phase01_conflict02', 'status': 1, 'deployed': 1, 'capacity': 3},
                    {'id': 'tw_jakku01_phase03_conflict01', 'status': 1, 'deployed': 1, 'capacity': 3}],
            'units': {b: {'id': 'id-' + b, 'definition': b + ':SEVEN_STAR'} for b in ('A', 'B', 'CAP', 'S1', 'S2', 'S3', 'R1')},
            'datacrons': {'dc': ga.pack([(1, 2, b'dc')])}, 'used': set(), 'used_dc': set(), 'teams': [],
            'commands': {'tw_jakku01_phase01_conflict02': 1, 'tw_jakku01_phase03_conflict01': 1},
            '_raw': ga.pack([(4, 2, b'PlayerRpc'), (5, 2, b'GetInitialData'), (7, 2, b'player'), (8, 2, b'token')]),
            '_player': 'player', '_current': lambda: True}


class DeployTests(unittest.TestCase):
    def setUp(self):
        self.s = state()
        self.rows = [{'zone': '1b', 'name': 'Наземный', 'units': ['A', 'B'], 'datacron': 'dc'}]

    def test_wire_and_fleet_order(self):
        rows = [{'zone': '3f', 'name': 'Флот', 'units': ['CAP', 'S1', 'S2', 'S3', 'R1']}]
        row = deploy.validate(self.s, 'war', rows)[0]
        envelope = ga.fields(deploy.deployment_request(self.s, row))
        self.assertEqual(ga._one(envelope, 5, 2), b'DeployUnits')
        args = ga.fields(ga._one(envelope, 6, 2))
        self.assertEqual(ga._one(args, 1, 2), b'war')
        self.assertEqual(ga._one(args, 5, 0), 8)
        squad = ga.fields(ga._one(args, 6, 2))
        self.assertEqual(ga._one(squad, 4, 0), 54)
        cells = [ga.fields(v) for t, w, v in squad if (t, w) == (1, 2)]
        self.assertEqual([ga._one(c, 12, 0) for c in cells], [3, 1, 1, 1, 5])
        self.assertEqual([ga._one(c, 3, 0) for c in cells], list(range(5)))
        self.assertEqual([ga._one(c, 1, 2) for c in cells], [b'id-CAP', b'id-S1', b'id-S2', b'id-S3', b'id-R1'])

    def test_datacron_sent_as_frozen_message(self):
        row = deploy.validate(self.s, 'war', self.rows)[0]
        env = ga.fields(deploy.deployment_request(self.s, row))
        squad = ga.fields(ga._one(ga.fields(ga._one(env, 6, 2)), 6, 2))
        self.assertEqual(ga._one(squad, 10, 2), self.s['datacrons']['dc'])

    def test_reject_stale_phase_event_full_zone_used_units_and_datacron(self):
        changes = [('round', 1), ('event_id', 'other'), ('end', 1), ('used', {'id-A'}), ('used_dc', {'dc'})]
        for key, value in changes:
            with self.subTest(key=key):
                changed = copy.deepcopy(self.s)
                changed[key] = value
                with self.assertRaises(ga.AccountError):
                    deploy.validate(changed, 'war', self.rows)
        self.s['own'][0]['deployed'] = 3
        with self.assertRaises(ga.AccountError):
            deploy.validate(self.s, 'war', self.rows)

    def test_reject_duplicate_assignments(self):
        with self.assertRaises(ga.AccountError):
            deploy.validate(self.s, 'war', self.rows * 2)

    def test_legacy_required_datacron_allows_deploying_without_one(self):
        self.rows[0].update(datacron='', datacron_required=True)
        row = deploy.inspect_assignments(self.s, 'war', self.rows)[0]
        self.assertFalse(row['error'])
        env = ga.fields(deploy.deployment_request(self.s, row))
        args = ga.fields(ga._one(env, 6, 2))
        self.assertFalse(any(tag == 10 for tag, _, _ in ga.fields(ga._one(args, 6, 2))))

    def test_existing_exact_assignment_skipped_even_when_zone_full(self):
        self.s['teams'] = [{'zone': self.s['own'][0]['id'], 'units': ['A', 'B'], 'types': [2, 1], 'datacron': 'dc'}]
        self.s['used'] = {'id-A', 'id-B'}
        self.s['used_dc'] = {'dc'}
        self.s['own'][0]['deployed'] = 3
        self.assertTrue(deploy.validate(self.s, 'war', self.rows)[0]['done'])
        self.s['teams'][0]['datacron'] = 'other'
        with self.assertRaises(ga.AccountError):
            deploy.validate(self.s, 'war', self.rows)

    def test_manual_pack_with_other_dc_and_zone_is_not_offered_again(self):
        self.s['teams'] = [{'zone': 'different', 'units': ['B', 'A'], 'types': [2, 1], 'datacron': 'manual-dc'}]
        self.s['used'] = {'id-A', 'id-B'}
        self.rows[0]['datacron'] = ''
        self.rows[0]['datacron_required'] = True
        row = deploy.inspect_assignments(self.s, 'war', self.rows)[0]
        self.assertTrue(row['done'])
        self.assertEqual(row['actual_datacron'], 'manual-dc')

    def test_full_zone_does_not_block_other_pack(self):
        self.s['own'][0]['deployed'] = 3
        fleet = {'zone': '3f', 'name': 'Флот', 'units': ['CAP', 'S1', 'S2', 'S3', 'R1']}
        rows = deploy.inspect_assignments(self.s, 'war', self.rows + [fleet])
        self.assertTrue(rows[0]['error'])
        self.assertFalse(rows[1]['error'])


class ExecutionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = patch.object(database, 'DB_NAME', str(Path(self.tmp.name) / 'db.sqlite'))
        self.db.start()
        self.auth = patch.object(deploy, '_authorize')
        self.auth.start()
        self.user = {'discord_id': 'owner', 'guild_id': 1}
        self.rows = [{'zone': '1b', 'name': 'Наземный', 'units': ['A', 'B'], 'datacron': 'dc'}]
        self.ticket = deploy.create(self.user, 'code', 'war', self.rows)

    async def asyncTearDown(self):
        self.auth.stop()
        self.db.stop()
        self.tmp.cleanup()

    async def test_success_verified_and_second_click_rejected(self):
        before = state()
        after = state()
        after['teams'] = [{'zone': after['own'][0]['id'], 'units': ['A', 'B'], 'types': [2, 1], 'datacron': 'dc'}]
        with patch.object(deploy, 'snapshot', AsyncMock(side_effect=[before, before, after])), patch.object(ga, '_read_payload', AsyncMock()) as rpc:
            data = await deploy.prepare(self.user, self.ticket, {'0': 'dc'})
            await deploy.apply(self.user, self.ticket, data['nonce'])
            with self.assertRaises(ga.AccountError):
                await deploy.apply(self.user, self.ticket, data['nonce'])
        self.assertEqual(rpc.await_count, 1)
        self.assertEqual(deploy.load(self.user, self.ticket)['state'], 'complete')

    async def test_next_war_reuses_actual_datacron_and_respects_manual_empty(self):
        live = state()
        live['teams'] = [{'zone': 'any', 'units': ['A', 'B'], 'datacron': 'actual'}]
        with patch.object(deploy, 'snapshot', AsyncMock(return_value=live)):
            await deploy.prepare(self.user, self.ticket, {'0': 'dc'})
        ticket = deploy.create(self.user, 'code', 'next', [
            {'zone': '2b', 'units': ['A', 'B'], 'name': 'Переименованный пак'},
        ])
        record = deploy.load(self.user, ticket)
        live.update(event_id='next', teams=[], datacrons={'actual': b''})
        row = deploy.suggested_assignments(record, live, [], {})[0]
        self.assertEqual((row['datacron'], row['datacron_source']), ('actual', 'history'))
        live['used_dc'].add('actual')
        self.assertFalse(deploy.suggested_assignments(record, live, [], {})[0].get('datacron'))
        live['used_dc'].clear()
        record['data']['assignments'][0].update(datacron='', datacron_chosen=True)
        self.assertEqual(deploy.suggested_assignments(record, live, [], {})[0]['datacron'], '')

    async def test_jocasta_recommendation_matches_only_free_datacron(self):
        record = deploy.load(self.user, self.ticket)
        record['data']['assignments'][0].update(
            datacron='', recommendation='Ход врага → бонусный ход Джокасты Ну, +100% урона')
        live = state()
        live.update(datacrons={'taken': b'', 'free': b''}, used_dc={'taken'})
        catalog = {'seasons': {1: {'level9': [
            ('bonus', 'Джокаста Ню: ход врага → доп. ход с +100% урона (не срабатывает на врагах).'),
        ]}}}
        datacrons = [{'id': dc, 'setId': 1, 'affix': [{}] * 8 + [{'abilityId': 'bonus'}]}
                     for dc in ('taken', 'free', 'not-in-war')]
        row = deploy.suggested_assignments(record, live, datacrons, catalog)[0]
        self.assertEqual((row['datacron'], row['datacron_source']), ('free', 'recommendation'))

    async def test_uncertain_write_persistently_blocks_retry(self):
        with patch.object(deploy, 'snapshot', AsyncMock(return_value=state())), patch.object(ga, '_read_payload', AsyncMock(side_effect=ga.AccountError('timeout'))) as rpc:
            data = await deploy.prepare(self.user, self.ticket, {'0': 'dc'})
            with self.assertRaises(ga.AccountError):
                await deploy.apply(self.user, self.ticket, data['nonce'])
            with self.assertRaises(ga.AccountError):
                await deploy.prepare(self.user, self.ticket, {'0': 'dc'})
        self.assertEqual(rpc.await_count, 1)
        self.assertEqual(deploy.load(self.user, self.ticket)['state'], 'unknown')

    async def test_owner_isolation_and_changed_roster_no_write(self):
        with self.assertRaises(ga.AccountError):
            deploy.load({'discord_id': 'intruder', 'guild_id': 1}, self.ticket)
        with patch.object(deploy, 'snapshot', AsyncMock(return_value=state())):
            data = await deploy.prepare(self.user, self.ticket, {'0': 'dc'})
        changed = state()
        changed['used'].add('id-A')
        with patch.object(deploy, 'snapshot', AsyncMock(return_value=changed)), patch.object(ga, '_read_payload', AsyncMock()) as rpc:
            await deploy.apply(self.user, self.ticket, data['nonce'])
        rpc.assert_not_awaited()
        self.assertEqual(deploy.load(self.user, self.ticket)['state'], 'partial')

    async def test_apply_skips_full_zone_and_places_available_fleet(self):
        fleet = {'zone': '3f', 'name': 'Флот', 'units': ['CAP', 'S1', 'S2', 'S3', 'R1']}
        data = deploy.load(self.user, self.ticket)['data']
        data['assignments'].append(fleet)
        deploy._save(self.ticket, data, 'draft')
        before = state()
        before['own'][0]['deployed'] = 3
        after = copy.deepcopy(before)
        after['teams'] = [{'zone': before['own'][1]['id'], 'units': fleet['units'], 'types': [3, 1, 1, 1, 5], 'datacron': ''}]
        with patch.object(deploy, 'snapshot', AsyncMock(side_effect=[before, before, before, after])), patch.object(ga, '_read_payload', AsyncMock()) as rpc:
            data = await deploy.prepare(self.user, self.ticket, {'0': 'dc', '1': ''})
            await deploy.apply(self.user, self.ticket, data['nonce'])
        self.assertEqual(rpc.await_count, 1)
        saved = deploy.load(self.user, self.ticket)
        self.assertEqual(saved['state'], 'partial')
        self.assertTrue(saved['data']['results'][0]['status'].startswith('Пропущен:'))
        self.assertEqual(saved['data']['results'][1]['status'], 'Выставлен и проверен')


if __name__ == '__main__':
    unittest.main()
