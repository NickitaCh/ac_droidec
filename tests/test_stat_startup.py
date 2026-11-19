import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from cogs.stat_requirements import StatRequirementsCog
from cogs.violations import ViolationsCog


class StatStartupTests(unittest.IsolatedAsyncioTestCase):
    async def test_player_sync_waits_for_roster_then_refreshes_without_hour_delay(self):
        bot = SimpleNamespace(wait_until_ready=AsyncMock(),
                              guild_roster_initial_attempt=asyncio.Event(),
                              guild_roster_caches={}, comlink=object())
        with patch('services.bot_profiles.start_shared_loop'):
            cog = StatRequirementsCog(bot)

        async def startup():
            await StatRequirementsCog._before_player_units_sync_loop(cog)
            await StatRequirementsCog.player_units_sync_loop.coro(cog)

        with (patch('database.get_unannounced_omicron_events', return_value=[]),
              patch('database.get_player_units_sync_times', return_value={}),
              patch('database.get_player_units_age_seconds', return_value=None),
              patch('database.set_bot_state'),
              patch('database.get_all_skill_tier_thresholds', return_value={}),
              patch('services.activity_diff.sync_player', new=AsyncMock(return_value=(True, 0, []))) as sync,
              patch('services.guild_snapshot.take'),
              patch('services.mod_grade.refresh_reference', return_value=False)):
            task = asyncio.create_task(startup())
            for _ in range(5):
                await asyncio.sleep(0)
            try:
                self.assertFalse(task.done(), 'Player sync must wait outside the shared sync slot')
                bot.guild_roster_caches[1] = {'Player': '123456789'}
                bot.guild_roster_initial_attempt.set()
                await asyncio.wait_for(task, 2)
                sync.assert_awaited_once_with(bot.comlink, '123456789', {1}, unittest.mock.ANY, {})
            finally:
                if not task.done():
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
                cog.cog_unload()

    async def test_roster_attempt_releases_player_sync_on_success_and_failure(self):
        for error in (None, RuntimeError('offline')):
            with self.subTest(error=error):
                bot = SimpleNamespace(guild_roster_initial_attempt=asyncio.Event())
                cog = SimpleNamespace(bot=bot, _sync_rosters=AsyncMock(side_effect=error))
                with patch('services.bot_profiles.worker_profile', return_value='test'):
                    if error:
                        with self.assertRaises(RuntimeError):
                            await ViolationsCog.update_roster_cache.coro(cog)
                    else:
                        await ViolationsCog.update_roster_cache.coro(cog)
                self.assertTrue(bot.guild_roster_initial_attempt.is_set())

    async def test_roster_waits_for_initial_calculator_attempt(self):
        bot = SimpleNamespace(wait_until_ready=AsyncMock(), stat_calc_initial_attempt=asyncio.Event())
        with patch('services.bot_profiles.worker_profile', return_value='test'):
            task = asyncio.create_task(ViolationsCog.before_loop(SimpleNamespace(bot=bot)))
            await asyncio.sleep(0)
            self.assertFalse(task.done())
            bot.stat_calc_initial_attempt.set()
            await asyncio.wait_for(task, 1)

    async def test_failed_calculator_releases_startup_gate(self):
        bot = SimpleNamespace(comlink=object(), stat_calc_initial_attempt=asyncio.Event())
        with patch('stat_engine.build_stat_calc', new=AsyncMock(side_effect=RuntimeError('offline'))):
            await StatRequirementsCog.stat_calc_loop.coro(SimpleNamespace(bot=bot))
        self.assertTrue(bot.stat_calc_initial_attempt.is_set())

    async def test_public_roster_does_not_wait_for_unused_calculator(self):
        bot = SimpleNamespace(wait_until_ready=AsyncMock(), stat_calc_initial_attempt=asyncio.Event())
        with patch('services.bot_profiles.worker_profile', return_value='prod'):
            await asyncio.wait_for(ViolationsCog.before_loop(SimpleNamespace(bot=bot)), 1)
