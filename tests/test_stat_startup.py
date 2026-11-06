import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from cogs.stat_requirements import StatRequirementsCog
from cogs.violations import ViolationsCog


class StatStartupTests(unittest.IsolatedAsyncioTestCase):
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
