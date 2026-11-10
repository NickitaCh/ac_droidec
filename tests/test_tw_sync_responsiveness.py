import asyncio
import threading
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch

from cogs.tw_counters import TWCounters


async def stream(items):
    for item in items:
        yield item


class TWSyncResponsivenessTests(unittest.IsolatedAsyncioTestCase):
    async def test_forum_sqlite_writes_do_not_block_discord_loop(self):
        started, release = threading.Event(), threading.Event()
        loop_thread = threading.get_ident()
        def slow_write(*args, **kwargs):
            self.assertNotEqual(threading.get_ident(), loop_thread)
            started.set()
            if not release.wait(2):
                raise AssertionError('Forum SQLite write blocked Discord event loop')

        message = SimpleNamespace(id=2, author=SimpleNamespace(bot=False), content='Counter',
                                  created_at=datetime.now(timezone.utc))
        thread = SimpleNamespace(id=1, name='Enemy', applied_tags=[], history=lambda **kw: stream([message]))
        class Forum:
            threads = [thread]
            def archived_threads(self, **kwargs):
                return stream([])
        cog = object.__new__(TWCounters)
        cog.bot = SimpleNamespace(get_channel=lambda _: Forum())
        parsed = dict(enemy_variant='Enemy', counter_leader='Leader', composition='', datacron_note='',
                      battle_plan='', team_code='', video_url='', parsed_ok=True)
        with patch('cogs.tw_counters.disnake.ForumChannel', Forum), \
             patch('cogs.tw_counters._parse_counter_message', return_value=parsed), \
             patch('database.upsert_tw_counter_thread', side_effect=slow_write), \
             patch('database.upsert_tw_counter', side_effect=slow_write) as write:
            task = asyncio.create_task(cog._sync_guild_once({'id': 1, 'tw_guide_forum_channel_id': '10'}))
            try:
                async with asyncio.timeout(1):
                    while not started.is_set():
                        await asyncio.sleep(0.01)
                release.set()
                result = await task
            finally:
                release.set()
                if not task.done():
                    await task
        self.assertEqual(result['messages'], 1)
        self.assertEqual(result['_seen_messages'], {'2'})
        write.assert_called_once()

    async def test_partial_sync_does_not_prune_or_update_baseline(self):
        cog = object.__new__(TWCounters)
        def stats(n):
            return {'threads': n, 'messages': n, '_seen_threads': set(), '_seen_messages': set()}
        from unittest.mock import AsyncMock
        with patch.object(cog, '_sync_guild_once', new=AsyncMock(return_value=stats(2))), \
             patch('database.prune_tw_counters') as prune, patch('database.set_bot_state') as save:
            result = await cog._sync_guild_checked({}, 1, {'threads': 10, 'messages': 10},
                                                 lambda s: s['threads'] < 5, stats(1))
        self.assertTrue(result['suspect_partial'])
        prune.assert_not_called()
        save.assert_not_called()
