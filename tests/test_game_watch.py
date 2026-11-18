import asyncio
import unittest
from unittest.mock import AsyncMock, patch

from services import game_account, game_watch


class SharedWatchTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.account = ("owner", 1, "test", "officer", "123")

    async def asyncTearDown(self):
        await game_watch.shutdown()

    async def test_manual_update_reconnects_only_after_expiry_and_retries_once(self):
        with patch.object(game_watch, "_capture", AsyncMock(side_effect=[
                game_account.SessionExpired(), {"active": [{}]}])) as capture, \
                patch.object(game_account, "reconnect", AsyncMock()) as reconnect:
            self.assertEqual(await game_watch.refresh_saved(*self.account, "raid", reconnect_if_expired=True),
                             {"active": [{}]})
        reconnect.assert_awaited_once_with(*self.account)
        self.assertEqual(capture.await_count, 2)

    async def test_concurrent_watches_share_login_and_expiry_stops_both(self):
        tw_sampled = asyncio.Event()
        raid_sampled = asyncio.Event()
        end_session = asyncio.Event()
        reader = AsyncMock()

        async def capture(reader_arg, guild, owner, kind):
            self.assertIs(reader_arg, reader)
            if kind == "tw":
                tw_sampled.set()
                await end_session.wait()
                raise game_account.SessionExpired("expired")
            raid_sampled.set()
            return {"active": [{}]}

        with patch.object(game_watch.game_account_readonly, "login_for_events", AsyncMock(return_value=reader)) as login:
            with patch.object(game_watch, "_capture", side_effect=capture):
                await asyncio.gather(
                    game_watch.start(*self.account, "tw", 60),
                    game_watch.start(*self.account, "raid", 120),
                )
                war, raid = (game_watch.get(1, "owner", kind) for kind in ("tw", "raid"))
                await asyncio.wait_for(asyncio.gather(tw_sampled.wait(), raid_sampled.wait()), 2)
                end_session.set()
                await asyncio.wait_for(asyncio.gather(war.task, raid.task), 2)
            login.assert_awaited_once_with(*self.account)
        self.assertEqual((war.status, raid.status), ("ended", "ended"))
        self.assertIn("Войдите заново", war.message)
        self.assertEqual(war.message, raid.message)

    async def test_manual_refresh_reuses_watch_and_stopping_one_leaves_other(self):
        both_captured = asyncio.Event()
        kinds = set()
        reader = AsyncMock()

        async def capture(reader_arg, guild, owner, kind):
            self.assertIs(reader_arg, reader)
            kinds.add(kind)
            if {"tb", "tw"} <= kinds:
                both_captured.set()
            return {"active": [{}]}

        with patch.object(game_watch.game_account_readonly, "login_for_events", AsyncMock(return_value=reader)) as login:
            with patch.object(game_watch, "_capture", side_effect=capture):
                await game_watch.start(*self.account, "tb", 60)
                await game_watch.start(*self.account, "tw", 60)
                await asyncio.wait_for(both_captured.wait(), 2)
                await game_watch.refresh(*self.account, "raid")
                battle = game_watch.get(1, "owner", "tb")
                game_watch.stop(1, "owner", "tb")
                await asyncio.wait_for(battle.task, 2)
                self.assertTrue(game_watch.active(1, "owner", "tw"))
            login.assert_awaited_once_with(*self.account)
        self.assertEqual(kinds, {"tb", "tw", "raid"})

    async def test_raid_history_alone_does_not_keep_watch_alive(self):
        with patch.object(game_watch.game_account_readonly, "login_for_events", AsyncMock(return_value=AsyncMock())):
            with patch.object(game_watch, "_capture", AsyncMock(return_value={"active": [], "results": 19})) as capture:
                await game_watch.start(*self.account, "raid", 60)
                watch = game_watch.get(1, "owner", "raid")
                await asyncio.wait_for(watch.task, 2)
                capture.assert_awaited_once()
        self.assertEqual(watch.status, "ended")
        self.assertIn("не вернула активное событие", watch.message)


if __name__ == "__main__":
    unittest.main()
