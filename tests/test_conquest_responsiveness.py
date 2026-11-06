import asyncio
import threading
import unittest
from unittest.mock import patch

from services import conquest_who


class ConquestResponsivenessTests(unittest.IsolatedAsyncioTestCase):
    async def test_slow_matching_allows_interactions_to_run(self):
        started = threading.Event()
        release = threading.Event()
        state = {"difficulties": [{"feats": [{"text": "test"}]}]}

        class Comlink:
            def get_game_data(self, **kwargs):
                return {}

        def slow_match(*args):
            started.set()
            if not release.wait(2):
                raise AssertionError("Event loop could not resume during matching")
            return {"kind": "any"}

        with patch("database.get_game_unit_names", return_value={}), patch("services.faction.factions", return_value={}), patch.object(conquest_who, "match", side_effect=slow_match):
            task = asyncio.create_task(conquest_who.annotate(Comlink(), state, [], {}))
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
        self.assertEqual(state["difficulties"][0]["feats"][0]["who"], {"kind": "any"})
