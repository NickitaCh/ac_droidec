import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
from zoneinfo import ZoneInfo

from cogs import guild_events


class TBOrderAutopostTests(unittest.IsolatedAsyncioTestCase):
    async def run_order(self, cfg=None, sent=(), discord_error=False, queue=True, twice=False):
        cfg = {"id": 5, "name": "Guild", "tb_active_plan_id": 10, **(cfg or {})}
        channel = SimpleNamespace(send=AsyncMock(side_effect=RuntimeError("Discord down") if discord_error else None),
                                  guild=SimpleNamespace(get_role=lambda _: SimpleNamespace(mention="<@&99>")))
        cog = object.__new__(guild_events.GuildEvents)
        cog.bot = SimpleNamespace(get_channel=Mock(return_value=channel))
        cog._tb_order_sent_key = {}
        cog._is_ping_week = lambda _: True
        cog._tb_order_phase_for_weekday = lambda _: "1"
        now = datetime(2026, 11, 2, 20, tzinfo=ZoneInfo("Europe/Moscow"))
        row = {"text": "1 этап\nПлан", "source": "manual", "tg_text": None}
        with patch.object(guild_events, "datetime", Mock(now=Mock(return_value=now))), \
             patch.object(guild_events.database, "get_all_guild_configs", return_value=[cfg]), \
             patch.object(guild_events.database, "get_tb_saved_plan", return_value={"id": 10}), \
             patch.object(guild_events.feature_flags, "is_enabled", return_value=True), \
             patch.object(guild_events.tb_schedule, "effective_dst_offset_minutes", return_value=0), \
             patch.object(guild_events.tb_schedule, "apply_correction", return_value=(20, 0)), \
             patch.object(guild_events.tb_order_store, "get_phases", return_value={1: row}), \
             patch.object(guild_events.tb_order_store, "recently_sent", side_effect=lambda _o, _p, where: where in sent), \
             patch.object(guild_events.tb_order_store, "mark_sent") as mark, \
             patch("tg_bot.outbox.enqueue_guild", return_value=int(queue)) as enqueue:
            await guild_events.GuildEvents.tb_order_loop.coro(cog)
            if twice:
                await guild_events.GuildEvents.tb_order_loop.coro(cog)
        return channel, enqueue, mark

    async def test_telegram_only_and_same_minute_dedup(self):
        channel, enqueue, mark = await self.run_order(twice=True)
        channel.send.assert_not_awaited()
        enqueue.assert_called_once()
        mark.assert_called_once_with(10, 1, "tg")

    async def test_manual_discord_send_does_not_suppress_telegram(self):
        channel, enqueue, mark = await self.run_order(
            {"tb_plan_channel_id": 1, "tb_order_role_id": 99}, sent={"discord"})
        channel.send.assert_not_awaited()
        enqueue.assert_called_once()
        mark.assert_called_once_with(10, 1, "tg")

    async def test_manual_telegram_send_does_not_suppress_discord(self):
        channel, enqueue, mark = await self.run_order(
            {"tb_plan_channel_id": 1, "tb_order_role_id": 99}, sent={"tg"})
        channel.send.assert_awaited_once_with("1 этап\nПлан\n\n\n<@&99>")
        enqueue.assert_not_called()
        mark.assert_called_once_with(10, 1, "discord")

    async def test_discord_failure_does_not_suppress_or_repeat_telegram(self):
        _channel, enqueue, mark = await self.run_order(
            {"tb_plan_channel_id": 1, "tb_order_role_id": 99}, discord_error=True, twice=True)
        enqueue.assert_called_once()
        mark.assert_called_once_with(10, 1, "tg")

    async def test_no_telegram_destination_does_not_mark_sent(self):
        _channel, enqueue, mark = await self.run_order(queue=False)
        enqueue.assert_called_once()
        mark.assert_not_called()
