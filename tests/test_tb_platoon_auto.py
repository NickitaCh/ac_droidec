import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, patch

import database
from services import tb_platoon_auto as auto, bot_profiles
from web.routes.guild_dashboard import tb_platoons_auto_dm


class PlatoonAutoTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = patch.object(database, "DB_NAME", str(Path(self.tmp.name) / "test.db"))
        self.db.start()
        database.init_db()
        self.gid = database.create_guild(name="AC", ally_code="123456789", discord_guild_id="123",
                                         member_role_id="", officer_role_id="")
        self.cfg = {"id": self.gid, "name": "AC", "tb_platoon_auto_enabled": 1, "tb_active_plan_id": 10}
        self.now = datetime(2026, 11, 2, 20, 0, 30, tzinfo=auto.MSK)
        self.anchor = patch.object(auto.tb_schedule, "get_week_anchor_date", return_value=self.now.date())
        self.offset = patch.object(auto.tb_schedule, "effective_dst_offset_minutes", return_value=0)
        self.anchor.start()
        self.offset.start()
        self.plan = {"id": 10, "guild_id": self.gid, "name": "Active"}

    def tearDown(self):
        self.offset.stop()
        self.anchor.stop()
        self.db.stop()
        self.tmp.cleanup()

    async def run_tick(self, cfg=None, plan=None, rows=None, error=None, times=1, now=None):
        with patch.object(bot_profiles, "served_guilds", return_value=[cfg or self.cfg]), \
             patch.object(database, "get_tb_saved_plan", return_value=plan or self.plan), \
             patch.object(auto.feature_flags, "is_enabled", return_value=True), \
             patch.object(bot_profiles, "token", return_value="test-token"), \
             patch.object(auto.tb_platoon_notify, "build_player_rows", AsyncMock(return_value=rows if rows is not None else [{"ally_code": "123"}])) as build, \
             patch.object(auto.tb_platoon_notify, "send_broadcast", AsyncMock(side_effect=error, return_value=[{"status": "sent"}])) as send:
            for _ in range(times):
                await auto.tick(now or self.now)
        return build, send

    def test_schedule_defaults_match_order_and_custom_is_absolute_msk(self):
        self.assertIsNone(auto.due(self.now, {}))
        self.assertEqual(auto.due(self.now, self.cfg), 1)
        custom = {**self.cfg, "tb_platoon_auto_time": "21:15"}
        self.assertIsNone(auto.due(self.now, custom))
        self.assertEqual(auto.due(self.now.replace(hour=21, minute=15), custom), 1)
        with patch.object(auto.tb_schedule, "effective_dst_offset_minutes", return_value=60):
            self.assertIsNone(auto.due(self.now, self.cfg))
            self.assertEqual(auto.due(self.now.replace(hour=21), self.cfg), 1)
            self.assertEqual(auto.due(self.now.replace(hour=21, minute=15), custom), 1)

    def test_only_tb_week_and_stage_days_and_short_restart_window(self):
        self.assertIsNone(auto.due(self.now.replace(day=9), self.cfg))
        self.assertIsNone(auto.due(self.now.replace(day=8), self.cfg))
        self.assertEqual(auto.due(self.now.replace(minute=9), self.cfg), 1)
        self.assertIsNone(auto.due(self.now.replace(minute=10), self.cfg))
        self.assertEqual(auto.due(self.now.replace(day=7), self.cfg), 6)

    async def test_auto_sends_current_stage_once_across_ticks_and_restart(self):
        build, send = await self.run_tick(times=2)
        send.assert_awaited_once_with("test-token", self.gid, self.plan, 1, {"123"})
        self.assertEqual(build.await_count, 1)
        _, after_restart = await self.run_tick()
        after_restart.assert_not_awaited()
        result = json.loads(database.get_bot_state(auto.LAST_STATE, guild_id=self.gid))
        self.assertEqual((result["sent"], result["failed"]), (1, 0))

    async def test_disabled_no_plan_wrong_owner_and_no_assignments_do_not_send(self):
        for cfg, plan, rows in (({**self.cfg, "tb_platoon_auto_enabled": 0}, None, None),
                                ({**self.cfg, "tb_active_plan_id": None}, None, None),
                                (self.cfg, {**self.plan, "guild_id": 999}, None),
                                (self.cfg, None, [])):
            _, send = await self.run_tick(cfg, plan, rows)
            send.assert_not_awaited()

    async def test_empty_stage_can_be_filled_before_window_ends(self):
        await self.run_tick(rows=[])
        _, send = await self.run_tick()
        send.assert_awaited_once()

    async def test_exception_does_not_repeat_possibly_delivered_dms(self):
        _, send = await self.run_tick(error=RuntimeError("interrupted"), times=2)
        send.assert_awaited_once()
        self.assertEqual(json.loads(database.get_bot_state(auto.LAST_STATE, guild_id=self.gid))["status"], "failed")

    async def test_time_or_plan_change_does_not_repeat_current_stage(self):
        await self.run_tick()
        _, send = await self.run_tick(cfg={**self.cfg, "tb_platoon_auto_time": "21:00"},
                                      plan={**self.plan, "id": 20}, now=self.now.replace(hour=21))
        send.assert_not_awaited()

    async def test_web_saves_opt_in_and_follow_order_and_custom_and_disable(self):
        self.assertFalse(database.get_guild_config(self.gid)["tb_platoon_auto_enabled"])
        for enabled, timing, time, expected in ((True, "order", "21:00", None),
                                                (True, "custom", "21:15", "21:15"),
                                                (False, "custom", "21:15", "21:15")):
            response = await tb_platoons_auto_dm(enabled, timing, time, "/tb/platoons", {"guild_id": self.gid})
            self.assertEqual(response.status_code, 303)
            cfg = database.get_guild_config(self.gid)
            self.assertEqual(cfg["tb_platoon_auto_enabled"], int(enabled))
            self.assertEqual(cfg["tb_platoon_auto_time"], expected)

    async def test_invalid_time_does_not_change_settings(self):
        from fastapi import HTTPException
        for time in ("24:00", "12:60", "garbage", ""):
            with self.assertRaises(HTTPException):
                await tb_platoons_auto_dm(True, "custom", time, "/tb/platoons", {"guild_id": self.gid})
        self.assertFalse(database.get_guild_config(self.gid)["tb_platoon_auto_enabled"])
