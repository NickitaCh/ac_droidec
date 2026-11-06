from contextlib import closing
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import database
from cogs.unit_forecast import UnitForecastCog
from services import activity_diff


class CompareRefreshTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = patch.object(database, 'DB_NAME', str(Path(self.tmp.name) / 'test.db'))
        self.db.start()
        activity_diff._refresh_locks.clear()
        self.codes = ['111111111', '222222222', '333333333']
        for code in self.codes:
            database.upsert_player_units(code, {'HUX': {'definitionId': 'HUX:SEVEN_STAR', 'marker': 'old'}})
        with closing(sqlite3.connect(database.DB_NAME)) as conn:
            conn.execute("UPDATE player_unit_cache SET updated_at=datetime('now', '-1 hour')")
            conn.commit()
        self.comlink = SimpleNamespace(get_player=Mock(side_effect=lambda allycode: {
            'name': allycode, 'rosterUnit': [
                {'definitionId': 'HUX:SEVEN_STAR', 'marker': 'new-' + allycode},
                {'definitionId': 'OFFICER:SEVEN_STAR', 'marker': 'new-' + allycode}]}))
        self.cog = UnitForecastCog.__new__(UnitForecastCog)
        self.cog.bot = SimpleNamespace(comlink=self.comlink)
        self.profiles = patch.object(database, 'get_player_profiles_bulk', return_value={})
        self.profiles.start()

    def tearDown(self):
        self.profiles.stop()
        self.db.stop()
        self.tmp.cleanup()

    async def test_refresh_updates_full_roster_for_every_compared_player(self):
        players, ages = await self.cog._card_players(self.codes, None, True)
        self.assertCountEqual([c.kwargs['allycode'] for c in self.comlink.get_player.call_args_list], self.codes)
        self.assertEqual(ages, [None, None, None])
        for code, player in zip(self.codes, players):
            self.assertEqual(len(player['rosterUnit']), 2)
            self.assertTrue(all(u['marker'] == 'new-' + code for u in player['rosterUnit']))
            self.assertEqual(database.get_player_units(code)['OFFICER']['marker'], 'new-' + code)

    async def test_without_refresh_uses_existing_rosters(self):
        players, ages = await self.cog._card_players(self.codes, None, False)
        self.comlink.get_player.assert_not_called()
        self.assertTrue(all(age > 3500 for age in ages))
        self.assertTrue(all(p['rosterUnit'][0]['marker'] == 'old' for p in players))

    async def test_failure_for_one_player_preserves_age_and_refreshes_others(self):
        original = self.comlink.get_player.side_effect
        def fetch(allycode):
            if allycode == self.codes[1]:
                raise RuntimeError('offline')
            return original(allycode)
        self.comlink.get_player.side_effect = fetch
        players, ages = await self.cog._card_players(self.codes, None, True)
        self.assertIsNone(ages[0])
        self.assertGreater(ages[1], 3500)
        self.assertIsNone(ages[2])
        self.assertEqual(players[1]['rosterUnit'][0]['marker'], 'old')
        self.assertEqual(self.comlink.get_player.call_count, 3)

    async def test_cooldown_is_per_player_and_keeps_real_cache_age(self):
        database.upsert_player_units(self.codes[1], {'HUX': {'definitionId': 'HUX:SEVEN_STAR', 'marker': 'recent'}})
        players, ages = await self.cog._card_players(self.codes, None, True)
        self.assertEqual(self.comlink.get_player.call_count, 2)
        self.assertIsNotNone(ages[1])
        self.assertLess(ages[1], 30)
        self.assertEqual(players[1]['rosterUnit'][0]['marker'], 'recent')

    async def test_panel_passes_refresh_flag_and_all_selected_accounts(self):
        from cogs.unit_test import CompareSetupView
        inter = SimpleNamespace(author=SimpleNamespace(id=123), edit_original_response=AsyncMock())
        cog = SimpleNamespace(own_code=Mock(return_value=(self.codes[0], None)), render_card=AsyncMock())
        params = {"альт": False, "обновить": True}
        with patch("cogs.unit_test.bot_profiles.current_profile", return_value="test"):
            view = CompareSetupView(cog, inter, 1, params)
        view.with_self = True
        view.codes = [self.codes[2]]
        view.users = [SimpleNamespace(id=456, display_name="Other")]
        click = SimpleNamespace(response=SimpleNamespace(defer=AsyncMock()))
        with patch.object(database, "get_any_main_registration", return_value=(self.codes[1], "Other")):
            await view.go_button.callback(click)
        args = cog.render_card.call_args.args
        self.assertEqual(args[2], self.codes)
        self.assertIs(args[3]["обновить"], True)

    async def test_forced_refresh_bypasses_cooldown_for_every_player(self):
        for code in self.codes:
            database.upsert_player_units(code, {'HUX': {'definitionId': 'HUX:SEVEN_STAR', 'marker': 'recent'}})
        for _ in range(2):
            players, ages = await self.cog._card_players(self.codes, None, True, force_refresh=True)
            self.assertEqual(ages, [None, None, None])
            self.assertTrue(all(len(p['rosterUnit']) == 2 for p in players))
        self.assertEqual(self.comlink.get_player.call_count, 6)

    async def test_unit_command_enables_force_only_when_refresh_requested(self):
        from cogs.unit_test import UnitTestCog
        cards = SimpleNamespace(_card_players=AsyncMock(return_value=([None], [None])))
        cog = UnitTestCog(SimpleNamespace(get_cog=lambda name: cards))
        inter = SimpleNamespace(author=SimpleNamespace(id=123), edit_original_response=AsyncMock())
        with patch('cogs.unit_test.stat_engine.wait_stat_calc', AsyncMock(return_value=True)), \
             patch('cogs.unit_test.guild_resolver.resolve_guild_id', return_value=None):
            for refresh in (False, True):
                await cog.render_card(inter, None, self.codes[:1], {"обновить": refresh})
                cards._card_players.assert_awaited_with(self.codes[:1], None, refresh, force_refresh=refresh)
