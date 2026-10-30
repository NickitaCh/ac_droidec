import unittest
import sqlite3
from unittest.mock import AsyncMock, patch
from starlette.requests import Request

import database
import tb_platoon_notify
from services import dashboard_data, tw_def_notify
from tg_bot import store, outbox
from web.routes import tw_defense


class TelegramDeliveryTests(unittest.IsolatedAsyncioTestCase):
    def test_guild_schema_has_separate_tw_delivery_setting(self):
        conn = sqlite3.connect(":memory:")
        try:
            database._ensure_guilds_table(conn.cursor())
            columns = {r[1] for r in conn.execute("PRAGMA table_info(guilds)")}
            self.assertIn("tw_def_dm_mode", columns)
        finally:
            conn.close()

    def test_explicit_route_does_not_fall_back_to_other_platform(self):
        both = {"discord": True, "tg": True}
        self.assertEqual(tb_platoon_notify.route(both, "discord_only"), ["discord"])
        self.assertEqual(tb_platoon_notify.route(both, "tg_only"), ["tg"])
        self.assertEqual(tb_platoon_notify.route(both, "both"), ["discord", "tg"])
        self.assertEqual(tb_platoon_notify.route({"discord": True, "tg": False}, "tg_only"), [])
        self.assertEqual(tw_def_notify.mode_for(["discord", "tg"]), "both")
        self.assertIsNone(tw_def_notify.mode_for([]))

    async def test_discord_only_failure_does_not_enqueue_telegram(self):
        with patch.object(tb_platoon_notify, "channels", return_value={"discord": True, "tg": True}), \
             patch.object(tb_platoon_notify, "_discord_dm", AsyncMock(return_value=(False, "closed"))), \
             patch.object(outbox, "enqueue_dm") as enqueue:
            ok, _ = await tb_platoon_notify.send_dm(None, "token", "owner", "text", "discord_only")
        self.assertFalse(ok)
        enqueue.assert_not_called()

    async def test_tw_both_delivers_main_and_alt_once_per_platform(self):
        rows = [{"discord_id": "123", "ally_code": code, "name": code, "entries": []} for code in ["a", "b"]]
        with patch.object(database, "get_guild_config", return_value={"name": "Guild"}), \
             patch.object(tw_def_notify, "build_player_rows", return_value=rows), \
             patch.object(tb_platoon_notify, "send_dm", AsyncMock(return_value=(True, "ok"))) as send:
            report = await tw_def_notify.send_broadcast("token", 2, {"name": "Plan"}, None, {"a", "b"}, "both")
        self.assertEqual(send.await_count, 2)
        self.assertEqual([call.args[-1] for call in send.call_args_list], ["discord_only", "tg_only"])
        self.assertEqual([r["status"] for r in report], ["sent", "sent"])

    async def test_partial_delivery_is_reported_when_telegram_unavailable(self):
        rows = [{"discord_id": "123", "ally_code": "a", "name": "Player", "entries": []}]
        with patch.object(database, "get_guild_config", return_value={}), \
             patch.object(tw_def_notify, "build_player_rows", return_value=rows), \
             patch.object(tb_platoon_notify, "send_dm", AsyncMock(side_effect=[(True, "Discord"), (False, "write /start")])):
            report = await tw_def_notify.send_broadcast("token", 2, {"name": "Plan"}, None, {"a"}, "both")
        self.assertEqual(report[0]["status"], "partial")
        self.assertIn("/start", report[0]["detail"])

    async def test_empty_destination_is_rejected_before_sending(self):
        request = Request({"type": "http", "scheme": "https", "server": ("test", 443),
                           "path": "/tw/def/plans/1/notify", "query_string": b"", "headers": []})
        with patch.object(tw_defense, "_get_plan_or_404", return_value={"id": 1, "name": "Plan"}), \
             patch.object(tw_def_notify, "build_player_rows", return_value=[]), \
             patch.object(database, "get_guild_config", return_value={}), \
             patch.object(tw_def_notify, "send_broadcast", AsyncMock()) as send:
            response = await tw_defense.plan_notify_send(1, request, ally_codes=["a"], destinations=[],
                                                        user={"guild_id": 2, "discord_id": "123"})
        self.assertEqual(response.status_code, 400)
        send.assert_not_awaited()

    async def test_telegram_only_does_not_require_discord_token(self):
        request = Request({"type": "http", "scheme": "https", "server": ("test", 443),
                           "path": "/tw/def/plans/1/notify", "query_string": b"", "headers": []})
        with patch.dict(tw_defense.os.environ, {}, clear=True), \
             patch.object(tw_defense, "_get_plan_or_404", return_value={"id": 1, "name": "Plan"}), \
             patch.object(tw_defense, "_notify_catalog", AsyncMock(return_value=None)), \
             patch.object(database, "update_guild_config") as update, \
             patch.object(tw_def_notify, "send_broadcast", AsyncMock(return_value=[{"name": "Player", "status": "sent", "detail": "Telegram"}])) as send:
            response = await tw_defense.plan_notify_send(1, request, ally_codes=["a"], destinations=["tg"],
                                                        user={"guild_id": 2, "discord_id": "123"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(send.call_args.args[-1], "tg_only")
        update.assert_called_once_with(2, tw_def_dm_mode="tg_only")

    def test_dashboard_identifies_telegram_only_discord_only_and_linked_users(self):
        def users(owner):
            return [{"dm_open": owner == "tg:20"}] if owner in ("tg:20", "30") else []
        with patch.object(database, "get_all_user_mappings_with_rank", return_value=[
            ("a", "Discord", 2), ("b", "Telegram", 2), ("c", "Both", 2), ("d", "None", 2)]), \
             patch.object(database, "get_all_registrations", return_value=[
                 ("10", "a", "Discord", True), ("tg:20", "b", "Telegram", True), ("30", "c", "Both", True)]), \
             patch.object(store, "tg_users_for_owner", side_effect=users):
            rows = {r.ally_code: r for r in dashboard_data.get_roster(2)}
        self.assertTrue(rows["a"].discord_registered)
        self.assertFalse(rows["a"].telegram_registered)
        self.assertTrue(rows["b"].telegram_registered)
        self.assertTrue(rows["b"].telegram_dm_open)
        self.assertFalse(rows["b"].discord_registered)
        self.assertTrue(rows["c"].discord_registered and rows["c"].telegram_registered)
        self.assertFalse(rows["c"].telegram_dm_open)
        self.assertFalse(rows["d"].registered)
