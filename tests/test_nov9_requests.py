import asyncio
import concurrent.futures
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import database
import stat_engine
from services import alliances, alliance_invites, guild_sync, trials


class DatabaseCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = patch.object(database, 'DB_NAME', str(Path(self.tmp.name) / 'test.db'))
        self.db.start()
        database.init_db()

    def tearDown(self):
        self.db.stop()
        self.tmp.cleanup()

    def guild(self, n=1, **kw):
        return database.create_guild(name=f'Guild {n}', ally_code='123456789', discord_guild_id='',
            member_role_id='', officer_role_id='', swgoh_guild_id=f'game-{n}',
            bot_profile='prod', is_active=0, subscription_source='paid', **kw)


class TrialTests(DatabaseCase):
    def test_single_trial_survives_recreated_guild_and_paid_extension(self):
        gid = self.guild()
        expiry = trials.activate(gid, 'actor')
        cfg = database.get_guild_config(gid)
        self.assertEqual((cfg['is_active'], cfg['subscription_source']), (1, 'trial'))
        database.extend_guild_subscription(gid, 30, 'paid')
        self.assertGreater(database.get_guild_config(gid)['subscription_expires_at'], expiry)
        second = self.guild()
        with self.assertRaises(trials.TrialError):
            trials.activate(second, 'other')

    def test_only_new_public_guilds(self):
        for n, fields in enumerate([{'bot_profile': 'test'}, {'is_active': 1},
                                   {'subscription_expires_at': '2020-01-01 00:00:00'},
                                   {'subscription_source': 'manual'}], 1):
            gid = self.guild(n)
            database.update_guild_config(gid, **fields)
            with self.assertRaises(trials.TrialError):
                trials.activate(gid, 'actor')

    def test_campaign_toggle_promo_limit_and_atomic_concurrent_redemption(self):
        database.set_bot_state('trial_campaign_enabled', '0', guild_id=0)
        gids = [self.guild(1), self.guild(2)]
        with self.assertRaises(trials.TrialError):
            trials.activate(gids[0], 'actor')
        trials.save_promo('WELCOME', 1, '2099-01-01')
        def redeem(gid):
            try:
                trials.activate(gid, 'actor', ' welcome ')
                return True
            except trials.TrialError:
                return False
        with concurrent.futures.ThreadPoolExecutor(2) as pool:
            self.assertEqual(sum(pool.map(redeem, gids)), 1)
        self.assertEqual(trials.promos()[0]['uses'], 1)

    def test_invalid_promo_does_not_activate(self):
        gid = self.guild()
        with self.assertRaises(trials.TrialError):
            trials.activate(gid, 'actor', 'missing')
        self.assertFalse(database.get_guild_config(gid)['is_active'])


class AllianceBulkTests(DatabaseCase):
    def test_invalid_resource_rolls_back_entire_bulk_and_keeps_existing_access(self):
        gid = self.guild(1)
        other = self.guild(2)
        for g in (gid, other):
            database.update_guild_config(g, is_active=1)
        alliances.create(gid, 'Alliance', 'a')
        invite_id = alliances.invite(gid, other, 'a')
        alliances.respond(other, invite_id, True)
        database.create_stat_plate('First', '', 'a', gid)
        database.create_stat_plate('Second', '', 'a', gid)
        with self.assertRaises(alliances.AllianceError):
            alliances.share_many(gid, ['plate|First', 'plate|missing'], [other], True, True)
        self.assertEqual(alliances.page(gid)['mine'], [])
        self.assertEqual(alliances.share_many(gid, ['plate|First', 'plate|Second'], [other], True, True), 2)
        self.assertEqual(len(alliances.page(other)['available']), 2)


class SyncTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = patch.object(database, 'DB_NAME', str(Path(self.tmp.name) / 'test.db'))
        self.db.start()
        database.init_db()

    async def asyncTearDown(self):
        await asyncio.gather(*list(guild_sync._tasks), return_exceptions=True)
        self.db.stop()
        self.tmp.cleanup()

    async def test_double_start_single_worker_and_partial_failure_visible_after_reload(self):
        entered, release = asyncio.Event(), asyncio.Event()
        async def run(comlink, code, *args):
            entered.set()
            await release.wait()
            if code == 'bad':
                raise ValueError('network')
            return True, 3, []
        with patch.object(database, 'get_all_user_mappings', return_value=[('a', 'ok', 'A'), ('b', 'bad', 'B')]), \
             patch.object(database, 'get_all_skill_tier_thresholds', return_value={}), \
             patch.object(database, 'get_guild_ids_for_ally_code', return_value={1}), \
             patch.object(guild_sync.activity_diff, 'sync_player', side_effect=run) as fetch:
            await guild_sync.start(1, object())
            await entered.wait()
            again = await guild_sync.start(1, object())
            self.assertEqual(again['state'], 'running')
            release.set()
            await asyncio.gather(*list(guild_sync._tasks))
            job = guild_sync.status(1)
            self.assertEqual((job['state'], job['completed'], job['failed'], job['added']), ('done', 2, 1, 3))
            self.assertEqual(fetch.call_count, 2)
            self.assertNotIn('token', job)

    async def test_dead_process_can_be_retried(self):
        token = guild_sync._claim(1)
        c = sqlite3.connect(database.DB_NAME)
        c.execute('UPDATE dashboard_sync_jobs SET heartbeat=0')
        c.commit(); c.close()
        self.assertEqual(guild_sync.status(1)['state'], 'error')
        self.assertNotEqual(guild_sync._claim(1), token)

    async def test_ally_code_lookup_requires_connected_guild(self):
        class Client:
            def get_player(self, code):
                return {'guildId': 'unknown'}
        with self.assertRaisesRegex(alliances.AllianceError, 'не подключена'):
            await alliance_invites.resolve(Client(), '123-456-789')

    async def test_closed_leader_dm_still_notifies_inviter_with_correct_profile(self):
        from services import bot_profiles
        calls = []
        async def send(who, text):
            calls.append((who, bot_profiles.current_profile()))
            if who == 'leader':
                raise ValueError('closed')
        with patch.object(database, 'get_guild_config', side_effect=lambda gid: {'name': str(gid), 'bot_profile': 'test' if gid == 1 else 'prod'}), \
             patch.object(database, 'get_all_user_mappings_with_rank', return_value=[('123456789', 'Leader', 4)]), \
             patch.object(database, 'find_ally_code_owner', return_value='leader'), \
             patch.object(alliance_invites.discord_rest, 'send_dm', side_effect=send):
            self.assertEqual(await alliance_invites.notify(1, 2, 'actor', 'Test'), (True, 1))
        self.assertEqual(calls, [('leader', 'prod'), ('actor', 'test')])


class StatDataTests(unittest.TestCase):
    def test_metadata_alias_preserves_real_growth_and_skips_npc(self):
        raw = {'units': [
            {'baseId': 'NEW', 'rarity': 7, 'obtainable': True, 'obtainableTime': '0', 'statProgressionId': 'growth7'},
            {'baseId': 'OLD', 'rarity': 1, 'obtainable': True, 'obtainableTime': '0', 'statProgressionId': 'growth1'},
            {'baseId': 'NPC', 'rarity': 7, 'obtainable': False},
        ]}
        with patch.object(stat_engine.GameDataBuilder, '_build_game_data', side_effect=lambda x: x):
            units = stat_engine.CompleteGameDataBuilder._build_game_data(raw)['units']
        aliases = [x for x in units if x['baseId'] == 'NEW']
        self.assertEqual([x['rarity'] for x in aliases], [7, 1])
        self.assertNotIn('statProgressionId', aliases[1])
        self.assertEqual(len(raw['units']), 3)


class SubscriptionWebTests(DatabaseCase):
    def setUp(self):
        super().setUp()
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from starlette.middleware.sessions import SessionMiddleware
        from web.routes.subscribe import router
        from web.deps import get_current_user
        self.app = FastAPI()
        self.app.add_middleware(SessionMiddleware, secret_key='test')
        self.app.include_router(router)
        self.app.dependency_overrides[get_current_user] = lambda: {'discord_id': 'actor', 'username': 'User', 'tier': None}
        self.client = TestClient(self.app)

    def tearDown(self):
        self.client.close()
        super().tearDown()

    def test_trial_form_activates_without_payment_and_protects_existing_subscription(self):
        from services.guild_admin import GuildAddResult
        gid = self.guild()
        result = GuildAddResult(ok=True, guild_id=gid, name='New')
        with patch('web.routes.subscribe._get_comlink'), \
             patch('web.routes.subscribe.add_guild', new=AsyncMock(return_value=result)), \
             patch('web.routes.subscribe.build_payment_link', new=AsyncMock()) as payment:
            r = self.client.post('/subscribe', data={'ally_code': '123456789', 'action': 'trial'})
            self.assertEqual(r.status_code, 200)
            self.assertIn('Пробный период включён', r.text)
            expires = database.get_guild_config(gid)['subscription_expires_at']
            r = self.client.post('/subscribe', data={'ally_code': '123456789', 'action': 'trial'})
            self.assertIn('уже пользовалась', r.text)
            self.assertEqual(database.get_guild_config(gid)['subscription_expires_at'], expires)
            payment.assert_not_awaited()

    def test_campaign_admin_and_origin_are_protected(self):
        self.assertEqual(self.client.get('/admin/promos').status_code, 401)
        with patch('web.deps.get_current_user', return_value={'discord_id': 'actor', 'tier': 'member'}):
            self.assertEqual(self.client.get('/admin/promos').status_code, 403)
        self.assertEqual(self.client.post('/subscribe', data={'ally_code': '123456789'}, headers={'Origin': 'https://foreign.example'}).status_code, 403)
