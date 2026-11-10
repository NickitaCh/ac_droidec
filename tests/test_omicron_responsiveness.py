import asyncio
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from cogs.stat_requirements import StatRequirementsCog


class OmicronResponsivenessTests(unittest.IsolatedAsyncioTestCase):
    async def test_slow_telegram_route_lookup_allows_loop_to_resume_and_is_once_per_guild(self):
        started, release = threading.Event(), threading.Event()

        def slow_routes(*args):
            started.set()
            if not release.wait(2):
                raise AssertionError('Telegram DB lookup blocked Discord event loop')
            return False

        cog = object.__new__(StatRequirementsCog)
        cog.bot = SimpleNamespace()
        with patch('database.get_skill_display_info', return_value={}), \
             patch('database.get_guild_config', return_value={}), \
             patch('services.feature_flags.is_enabled', return_value=True), \
             patch('tg_bot.outbox.guild_wants', side_effect=slow_routes) as routes:
            task = asyncio.create_task(cog._announce_omicrons([(1, '123', 'VADER', 'skill', 1), (2, '456', 'VADER', 'skill', 1)]))
            try:
                async with asyncio.timeout(1):
                    while not started.is_set():
                        await asyncio.sleep(0.01)
                release.set()
                await task
            finally:
                release.set()
                if not task.done():
                    await task
        routes.assert_called_once_with(1, 'omicrons')
