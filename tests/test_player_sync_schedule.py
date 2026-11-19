import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from contextlib import closing
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import httpx
import database
from swgoh_comlink import SwgohComlink
from swgoh_comlink.exceptions import SwgohComlinkException
from services import player_json
from services.player_sync_schedule import PlayerSyncSchedule
from cogs.stat_requirements import StatRequirementsCog


class ScheduleTests(unittest.TestCase):
    def test_oldest_first_and_no_burst_after_restart(self):
        s = PlayerSyncSchedule()
        s.reconcile(['old', 'older', 'fresh'], {'old': 100, 'older': 50, 'fresh': 900})
        self.assertEqual(s.take(1000), 'older')
        s.complete('older', 1001)
        self.assertIsNone(s.take(1002))
        self.assertEqual(s.take(1180), 'old')
        restarted = PlayerSyncSchedule()
        restarted.reconcile(['older'], {'older': 1001})
        self.assertIsNone(restarted.take(1200))
        self.assertEqual(restarted.take(1601), 'older')

    def test_manual_refresh_reconciles_and_departed_players_are_removed(self):
        s = PlayerSyncSchedule()
        s.reconcile(['a', 'b'], {})
        s.reconcile(['a', 'new'], {'a': 990})
        self.assertEqual(s.take(1000), 'new')
        self.assertNotIn('b', s.due)
        s.complete('a', 1000)
        s.reconcile(['a'], {'a': 990})
        self.assertEqual(s.due['a'], 1600)

    def test_failure_backoff_survives_reconcile_without_starving_others(self):
        s = PlayerSyncSchedule()
        s.reconcile(['a', 'b'], {})
        s.complete('a', 1000, False)
        s.reconcile(['a', 'b'], {})
        self.assertEqual(s.take(1000), 'b')
        self.assertEqual(s.due['a'], 1060)

    def test_338_overdue_players_are_paced_within_target_period(self):
        s = PlayerSyncSchedule()
        codes = [str(n) for n in range(338)]
        s.reconcile(codes, {})
        now = 10000.0
        starts = []
        for _ in codes:
            code = s.take(now)
            self.assertIsNotNone(code)
            starts.append(now)
            s.complete(code, now + .7)
            now += 1.75  # 250 ms polling, typical request finishes inside this slot
        self.assertLess(starts[-1] - starts[0], 600)


class PlayerJsonTests(unittest.TestCase):
    def test_round_trip_preserves_game_types_unicode_and_wide_integers(self):
        for value in [{'имя': 'Игрок 😀', 'skill': [{'id': 'a', 'tier': 8}], 'speed': 123.125,
                       'nullable': None, 'yes': True, 'number': 18446744073709551615},
                      {'number': 2 ** 85}, {'number': -(2 ** 70)}]:
            encoded = player_json.dumps(value)
            self.assertIsInstance(encoded, str)
            self.assertEqual(player_json.loads(encoded), value)
            self.assertEqual(json.loads(encoded), value)

    def test_bad_json_raises(self):
        with self.assertRaises(ValueError):
            player_json.loads(b'{broken')

    def test_player_transport_preserves_headers_pool_gzip_and_errors(self):
        seen = []
        def handle(request):
            seen.append(request)
            return httpx.Response(200, json={'name': 'Игрок', 'rosterUnit': []})
        client = SwgohComlink(url='http://example.invalid')
        original = client.client
        client.client = httpx.Client(base_url='http://example.invalid', transport=httpx.MockTransport(handle))
        try:
            with patch.object(client, '_construct_request_headers', return_value={'X-Test-Signing': 'yes'}) as sign:
                self.assertEqual(player_json.get_player(client, '123456789')['name'], 'Игрок')
                self.assertEqual(json.loads(seen[0].content)['payload']['allyCode'], '123456789')
                self.assertEqual(seen[0].headers['X-Test-Signing'], 'yes')
                self.assertIn('gzip', seen[0].headers['Accept-Encoding'])
                self.assertEqual(sign.call_args.args[:2], ('POST', 'player'))
            client.client.close()
            client.client = httpx.Client(base_url='http://example.invalid', transport=httpx.MockTransport(
                lambda request: httpx.Response(503, text='unavailable')))
            with self.assertRaises(SwgohComlinkException):
                player_json.get_player(client, '123456789')
            client.client.close()
            client.client = httpx.Client(base_url='http://example.invalid', transport=httpx.MockTransport(
                lambda request: httpx.Response(200, content=b'{broken')))
            with self.assertRaises(ValueError):
                player_json.get_player(client, '123456789')
        finally:
            original.close()
            client.client.close()


class CogScheduleTests(unittest.IsolatedAsyncioTestCase):
    def make_cog(self):
        bot = SimpleNamespace(comlink=object(), guild_roster_caches={1: {'Player': '123456789'}},
                              wait_until_ready=AsyncMock())
        with patch('services.bot_profiles.start_shared_loop'):
            return StatRequirementsCog(bot)

    async def test_tick_refreshes_one_due_player_and_preserves_omicron_delivery(self):
        cog = self.make_cog()
        hits = [(42, 'UNIT', 'skill', 1)]
        with (patch('database.get_player_units_sync_times', return_value={}),
              patch('database.get_all_skill_tier_thresholds', return_value={}),
              patch('database.set_bot_state'),
              patch('database.get_unannounced_omicron_events', return_value=[]),
              patch.object(cog, '_player_units_sync_one', new=AsyncMock(return_value=(True, 1, hits, 1000))) as sync,
              patch.object(cog, '_announce_omicrons', new=AsyncMock()) as announce,
              patch('cogs.stat_requirements.time.time', return_value=1000)):
            await cog.player_units_sync_loop.coro(cog)
            sync.assert_awaited_once_with('123456789', {1})
            self.assertEqual(list(announce.call_args.args[0]), [(42, '123456789', 'UNIT', 'skill', 1)])
            await cog.player_units_sync_loop.coro(cog)
            self.assertEqual(sync.await_count, 1)
        cog.cog_unload()

    async def test_manual_refresh_is_rechecked_inside_shared_slot(self):
        cog = self.make_cog()
        with (patch('database.get_player_units_age_seconds', return_value=2),
              patch('services.activity_diff.sync_player', new=AsyncMock()) as sync):
            fetched, _, _, checked = await cog._player_units_sync_one('123456789', {1})
            self.assertFalse(fetched)
            self.assertIsNotNone(checked)
            sync.assert_not_awaited()
        cog.cog_unload()

    async def test_pending_omicrons_retry_even_without_a_roster(self):
        cog = self.make_cog()
        cog.bot.guild_roster_caches = {}
        with (patch('database.get_unannounced_omicron_events', return_value=[(42, 1, '123456789', 'UNIT', 'sk')]),
              patch.object(cog, '_announce_omicrons', new=AsyncMock()) as announce):
            await cog.player_units_sync_loop.coro(cog)
            announce.assert_awaited_once()
            self.assertEqual(list(announce.call_args.args[0]), [(42, '123456789', 'UNIT', 'sk', 1)])
        cog.cog_unload()

    async def test_transient_database_error_does_not_stop_polling(self):
        cog = self.make_cog()
        with patch.object(cog, '_player_units_sync_tick', new=AsyncMock(side_effect=[RuntimeError('busy'), None])) as tick:
            await cog.player_units_sync_loop.coro(cog)
            self.assertGreater(cog.player_sync_schedule.next_start, 0)
            await cog.player_units_sync_loop.coro(cog)
            self.assertEqual(tick.await_count, 2)
        cog.cog_unload()

    async def test_legacy_timestamps_are_seeded_once_without_rescanning_units(self):
        import sqlite3
        with tempfile.TemporaryDirectory() as td, patch.object(database, 'DB_NAME', str(Path(td) / 'legacy.db')):
            with closing(sqlite3.connect(database.DB_NAME)) as conn:
                database._ensure_player_unit_cache_table(conn.cursor())
                conn.execute("INSERT INTO player_unit_cache VALUES ('123456789','UNIT','{}','2026-11-19 10:00:00')")
                conn.commit()
            first = database.get_player_units_sync_times(['123456789'])
            # Metadata remains usable even if the large cache cannot be scanned.
            with closing(sqlite3.connect(database.DB_NAME)) as conn:
                conn.execute('DROP TABLE player_unit_cache')
                conn.commit()
            self.assertEqual(database.get_player_units_sync_times(['123456789']), first)

    async def test_database_cache_is_text_and_preserves_activity_events(self):
        import sqlite3
        from services import activity_diff
        with tempfile.TemporaryDirectory() as td, patch.object(database, 'DB_NAME', str(Path(td) / 'test.db')):
            old = {'UNIT': {'definitionId': 'UNIT:SEVEN_STAR', 'currentTier': 12,
                            'currentRarity': 6, 'relic': {'currentTier': 2}, 'skill': [{'id': 'sk', 'tier': 7}]}}
            database.upsert_player_units('123456789', old)
            new = dict(old['UNIT'], currentTier=13, currentRarity=7, skill=[{'id': 'sk', 'tier': 9}])
            client = SimpleNamespace(get_player=Mock(return_value={'rosterUnit': [new], 'datacron': [], 'name': 'Игрок', 'profileStat': []}))
            fetched, added, hits = await activity_diff.sync_player(client, '123456789', {1}, '2026-11-19', {'sk': (8, 9)})
            self.assertTrue(fetched)
            self.assertGreaterEqual(added, 3)
            self.assertEqual(hits[0][1:], ('UNIT', 'sk', 1))
            self.assertEqual(database.get_player_units('123456789')['UNIT'], new)
            with closing(sqlite3.connect(database.DB_NAME)) as conn:
                self.assertEqual(conn.execute('select typeof(unit_json) from player_unit_cache').fetchone()[0], 'text')
            self.assertIn('123456789', database.get_player_units_sync_times(['123456789']))
