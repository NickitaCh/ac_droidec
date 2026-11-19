import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from cogs.violations import HybridCache, ViolationsCog


class RosterGuildIdentityTests(unittest.IsolatedAsyncioTestCase):
    async def run_sync(self, cfg, *, player=None, guild=None, guild_error=None):
        comlink = SimpleNamespace(
            get_player=Mock(return_value=player or {}),
            get_guild=Mock(return_value=guild or {}, side_effect=guild_error),
        )
        old = HybridCache({'Old player': '123456789'})
        bot = SimpleNamespace(comlink=comlink, guild_roster_caches={1: old},
                              guild_roster_cache=old, change_presence=AsyncMock())
        cog = SimpleNamespace(bot=bot, _resolve_departed_names=AsyncMock(),
                              update_roster_cache=SimpleNamespace(minutes=5, change_interval=Mock()))
        with (
            patch('database.get_all_guild_configs', return_value=[cfg]),
            patch('database.get_roster_by_player_id', return_value={}),
            patch('database.get_all_user_mappings', return_value=[('123456789', '123456789', 'Old player')]),
            patch('database.update_guild_config') as update,
            patch('database.sync_guild_roster') as sync,
            patch('database.save_member_activity'),
            patch('cogs.violations.asyncio.sleep', new=AsyncMock()),
        ):
            await ViolationsCog._sync_rosters.__wrapped__(cog)
        return bot, cog, update, sync

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
