import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from cogs.violations import HybridCache, ViolationsCog


class RosterGuildIdentityTests(unittest.IsolatedAsyncioTestCase):
    async def run_sync(self, cfg, *, player=None, guild=None, guild_error=None, previous=None, player_error=None):
        comlink = SimpleNamespace(
            get_player=Mock(return_value=player or {}, side_effect=player_error),
            get_guild=Mock(return_value=guild or {}, side_effect=guild_error),
        )
        old = HybridCache({'Old player': '123456789'})
        bot = SimpleNamespace(comlink=comlink, guild_roster_caches={1: old},
                              guild_roster_cache=old, change_presence=AsyncMock())
        cog = SimpleNamespace(bot=bot, _resolve_departed_names=AsyncMock(),
                              update_roster_cache=SimpleNamespace(minutes=5, change_interval=Mock()))
        with (
            patch('database.get_all_guild_configs', return_value=[cfg]),
            patch('database.get_roster_by_player_id', return_value=previous or {}),
            patch('database.get_all_user_mappings', return_value=[('123456789', '123456789', 'Old player')]),
            patch('database.update_guild_config') as update,
            patch('database.sync_guild_roster') as sync,
            patch('database.save_member_activity') as activity,
            patch('cogs.violations.asyncio.sleep', new=AsyncMock()),
        ):
            await ViolationsCog._sync_rosters.__wrapped__(cog)
        bot.saved_member_activity = activity.call_args
        return bot, cog, update, sync

    async def test_known_member_reuses_code_but_takes_new_name_and_role_from_guild(self):
        bot, _, _, sync = await self.run_sync(
            {'id': 1, 'name': 'Home', 'swgoh_guild_id': 'home'},
            previous={'known': ('123456789', 'Old name')},
            guild={'guild': {'member': [{'playerId': 'known', 'playerName': 'New name',
                                         'memberLevel': 3, 'lastActivityTime': '1234', 'guildJoinTime': '5678'}]}},
        )
        bot.comlink.get_player.assert_not_called()
        sync.assert_called_once_with(1, [('123456789', '123456789', 'New name', 3, 'known')])
        self.assertEqual(bot.guild_roster_cache, {'New name': '123456789'})
        self.assertEqual(bot.saved_member_activity.args, (1, [('123456789', 1234, 5678)]))

    async def test_complete_guild_member_needs_no_profile_lookup(self):
        bot, _, _, sync = await self.run_sync(
            {'id': 1, 'name': 'Home', 'swgoh_guild_id': 'home'},
            guild={'guild': {'member': [{'playerId': 'new', 'playerName': 'New player', 'allyCode': 222222222}]}},
        )
        bot.comlink.get_player.assert_not_called()
        sync.assert_called_once()
        self.assertEqual(bot.guild_roster_cache, {'New player': '222222222'})

    async def test_placeholder_code_does_not_suppress_recovery_lookup(self):
        for invalid in ('player-id', '000000000', '0'):
            with self.subTest(code=invalid):
                bot, _, _, sync = await self.run_sync(
                    {'id': 1, 'name': 'Home', 'swgoh_guild_id': 'home'},
                    previous={'known': (invalid, 'Old name')},
                    player={'name': 'Recovered', 'allyCode': 222222222},
                    guild={'guild': {'member': [{'playerId': 'known', 'playerName': 'Current'}]}},
                )
                bot.comlink.get_player.assert_called_once_with(player_id='known')
                self.assertEqual(bot.guild_roster_cache, {'Recovered': '222222222'})

    async def test_zeroed_guild_name_still_attempts_profile_recovery(self):
        bot, _, _, _ = await self.run_sync(
            {'id': 1, 'name': 'Home', 'swgoh_guild_id': 'home'},
            previous={'known': ('123456789', 'Old name')},
            player={'name': 'Recovered name', 'allyCode': 123456789},
            guild={'guild': {'member': [{'playerId': 'known'}]}},
        )
        bot.comlink.get_player.assert_called_once_with(player_id='known')
        self.assertEqual(bot.guild_roster_cache, {'Recovered name': '123456789'})

    async def test_failed_profile_recovery_keeps_previous_identity(self):
        bot, _, _, sync = await self.run_sync(
            {'id': 1, 'name': 'Home', 'swgoh_guild_id': 'home'},
            previous={'known': ('123456789', 'Old name')},
            player_error=RuntimeError('offline'),
            guild={'guild': {'member': [{'playerId': 'known'}]}},
        )
        self.assertEqual(bot.comlink.get_player.call_count, 2)
        self.assertEqual(bot.guild_roster_cache, {'Old name': '123456789'})
        sync.assert_called_once()

    async def test_mixed_roster_only_fetches_new_player_and_drops_departed_player(self):
        bot, _, _, sync = await self.run_sync(
            {'id': 1, 'name': 'Home', 'swgoh_guild_id': 'home'},
            previous={'known': ('123456789', 'Old name'), 'departed': ('333333333', 'Left')},
            player={'name': 'New player', 'allyCode': 222222222},
            guild={'guild': {'member': [{'playerId': 'known', 'playerName': 'Renamed'},
                                         {'playerId': 'new', 'playerName': 'New player'}]}},
        )
        bot.comlink.get_player.assert_called_once_with(player_id='new')
        self.assertEqual(bot.guild_roster_cache, {'Renamed': '123456789', 'New player': '222222222'})
        self.assertEqual(len(sync.call_args.args[1]), 2)

    async def test_fresh_guild_code_takes_precedence_over_saved_code(self):
        bot, _, _, _ = await self.run_sync(
            {'id': 1, 'name': 'Home', 'swgoh_guild_id': 'home'},
            previous={'known': ('123456789', 'Old name')},
            guild={'guild': {'member': [{'playerId': 'known', 'playerName': 'Current', 'allyCode': 222222222}]}},
        )
        bot.comlink.get_player.assert_not_called()
        self.assertEqual(bot.guild_roster_cache, {'Current': '222222222'})

    async def test_departed_anchor_does_not_block_known_guild(self):
        bot, cog, update, sync = await self.run_sync(
            {'id': 1, 'name': 'Home', 'ally_code': '111111111', 'swgoh_guild_id': 'home'},
            player={'name': 'Current player', 'allyCode': 222222222, 'guildId': 'other'},
            guild={'guild': {'member': [{'playerId': 'current'}]}},
        )
        bot.comlink.get_guild.assert_called_once_with(guild_id='home', include_recent_guild_activity_info=True)
        bot.comlink.get_player.assert_called_once_with(player_id='current')
        update.assert_not_called()
        sync.assert_called_once()
        self.assertEqual(bot.guild_roster_cache, {'Current player': '222222222'})
        self.assertNotIn('[кеш]', bot.change_presence.call_args.kwargs['activity'].name)
        cog.update_roster_cache.change_interval.assert_called_once_with(hours=0, minutes=15)

    async def test_first_sync_discovers_and_saves_identity(self):
        bot, _, update, sync = await self.run_sync(
            {'id': 1, 'name': 'Home', 'ally_code': '111111111'},
            player={'guildId': 'home', 'name': 'Current player', 'allyCode': 222222222},
            guild={'guild': {'member': [{'playerId': 'current'}]}},
        )
        self.assertEqual(bot.comlink.get_player.call_args_list[0].kwargs, {'allycode': '111111111'})
        update.assert_called_once_with(1, swgoh_guild_id='home')
        sync.assert_called_once()

    async def test_guildless_anchor_does_not_block_known_guild(self):
        bot, _, update, sync = await self.run_sync(
            {'id': 1, 'name': 'Home', 'ally_code': '111111111', 'swgoh_guild_id': 'home'},
            player={'name': 'Current player', 'allyCode': 222222222},
            guild={'guild': {'member': [{'playerId': 'current'}]}},
        )
        bot.comlink.get_player.assert_called_once_with(player_id='current')
        update.assert_not_called()
        sync.assert_called_once()
        self.assertNotIn('[кеш]', bot.change_presence.call_args.kwargs['activity'].name)

    async def test_missing_identity_uses_cache_without_overwriting_roster(self):
        bot, _, update, sync = await self.run_sync({'id': 1, 'name': 'Home', 'ally_code': '111111111'})
        bot.comlink.get_guild.assert_not_called()
        update.assert_not_called()
        sync.assert_not_called()
        self.assertEqual(bot.guild_roster_cache, {'Old player': '123456789'})
        self.assertIn('[кеш]', bot.change_presence.call_args.kwargs['activity'].name)

    async def test_network_failure_does_not_follow_anchor_to_another_guild(self):
        bot, _, update, sync = await self.run_sync(
            {'id': 1, 'name': 'Home', 'ally_code': '111111111', 'swgoh_guild_id': 'home'},
            player={'guildId': 'other'}, guild_error=RuntimeError('offline'),
        )
        bot.comlink.get_player.assert_not_called()
        update.assert_not_called()
        sync.assert_not_called()
        self.assertIn('[кеш]', bot.change_presence.call_args.kwargs['activity'].name)

    async def test_empty_response_preserves_existing_roster(self):
        _, _, update, sync = await self.run_sync(
            {'id': 1, 'name': 'Home', 'ally_code': '111111111'}, player={'guildId': 'home'},
        )
        update.assert_not_called()
        sync.assert_not_called()
