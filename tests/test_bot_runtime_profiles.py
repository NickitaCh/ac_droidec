import asyncio
import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import database
import guild_resolver
from cogs.datacron_requirements import _fetch_datacron_cache
from cogs.stat_requirements import StatRequirementsCog
from cogs.violations import ViolationsCog
from services import bot_profiles as profiles, discord_invite, discord_rest, shared_datacron_catalog, sync_queue
from services.bot_worker_lock import worker_instance
from tg_bot import core, notifier, outbox, store


class RuntimeProfileTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.patches = [
            patch.object(database, "DB_NAME", str(Path(self.tmp.name) / "test.db")),
            patch.object(sync_queue, "LOCK_PATH", Path(self.tmp.name) / "heavy-sync.lock"),
            patch.object(profiles, "_worker_profile", None),
            patch.dict(os.environ, {"BOT_PROFILE": "test"}),
        ]
        for item in self.patches:
            item.start()
        database.init_db()
        self.test_gid = self.guild("Test", "test")
        self.prod_gid = self.guild("Public", "prod")

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()
        self.tmp.cleanup()

    def guild(self, name, profile):
        return database.create_guild(name=name, ally_code="123456789", discord_guild_id=name,
                                     member_role_id="", officer_role_id="", bot_profile=profile)

    def api(self):
        return SimpleNamespace(send_message=AsyncMock(), answer_callback=AsyncMock())

    def app(self, profile):
        return core.App(self.api(), Mock(), "test_bot", 42, "https://example.invalid", profile=profile)

    def test_token_selection_never_falls_back_to_legacy(self):
        with patch.dict(os.environ, {"DISCORD_TOKEN": "old", "TELEGRAM_BOT_TOKEN": "old-tg"}, clear=True):
            self.assertIsNone(profiles.token("discord", "prod"))
            self.assertIsNone(profiles.token("telegram", "prod"))
            self.assertEqual(profiles.token("discord", "test"), "old")
        with patch.dict(os.environ, {"DISCORD_TOKEN": "same", "PROD_DISCORD_TOKEN": "same"}, clear=True):
            with self.assertRaises(ValueError):
                profiles.token("discord", "prod")

    def test_old_telegram_dm_permission_migrates_only_to_test(self):
        with closing(sqlite3.connect(database.DB_NAME)) as conn:
            conn.execute("CREATE TABLE tg_users(tg_user_id INTEGER PRIMARY KEY,owner_id TEXT,username TEXT,full_name TEXT,dm_open INTEGER,linked_at TEXT,updated_at TEXT)")
            conn.execute("INSERT INTO tg_users VALUES(123,'tg:123',NULL,'Player',1,NULL,'now')")
            conn.commit()
        self.assertTrue(outbox.can_dm("tg:123", "test"))
        self.assertFalse(outbox.can_dm("tg:123", "prod"))
        self.assertTrue(outbox.can_dm("tg:123", "test"))

    def test_new_prod_user_does_not_get_legacy_dm_permission_on_next_read(self):
        store.touch_user({"id": 123, "first_name": "Player"}, dm_open=True, profile="prod")
        for _ in range(3):
            self.assertTrue(outbox.can_dm("tg:123", "prod"))
            self.assertFalse(outbox.can_dm("tg:123", "test"))

    def test_dm_outboxes_are_disjoint_and_identity_is_shared(self):
        for profile in ("test", "prod"):
            owner = store.touch_user({"id": 123}, dm_open=True, profile=profile)
            self.assertEqual(owner, "tg:123")
            self.assertTrue(outbox.enqueue_dm(owner, profile, profile=profile))
        self.assertEqual([m["text"] for m in outbox.pending(profile="test")], ["test"])
        self.assertEqual([m["text"] for m in outbox.pending(profile="prod")], ["prod"])

    def test_guild_queue_follows_explicit_assignment_without_copying_rows(self):
        store.bind_chat(-100, self.test_gid, "Group", "supergroup", "officer")
        outbox.set_route(self.test_gid, "tasks", -100, None, True, "officer")
        self.assertTrue(outbox.enqueue_guild(self.test_gid, "tasks", "Task"))
        before = outbox.pending(profile="test")
        self.assertEqual(len(before), 1)
        self.assertEqual(outbox.pending(profile="prod"), [])
        database.update_guild_config(self.test_gid, bot_profile="prod")
        self.assertEqual(outbox.pending(profile="test"), [])
        after = outbox.pending(profile="prod")
        self.assertEqual(before[0]["id"], after[0]["id"])

    def test_link_token_cannot_be_consumed_by_other_bot(self):
        value = store.create_link_token("1234", "prod")
        self.assertIsNone(store.consume_link_token(value, "test"))
        self.assertEqual(store.consume_link_token(value, "prod"), "1234")
        self.assertIsNone(store.consume_link_token(value, "prod"))

    def test_wrong_bot_does_not_automatically_choose_alt(self):
        for gid, code, name in [(self.test_gid, "123456789", "Main"), (self.prod_gid, "223456789", "Alt")]:
            database.sync_guild_roster(gid, [(code, code, name, 3, name)])
            database.set_user_registration("owner", code, name, guild_id=gid)
        database.set_bot_state(profiles.state_key("telegram_bot_username", "test"), "original_bot", guild_id=0)
        with patch.object(profiles, "_worker_profile", "prod"):
            access = guild_resolver.resolve_access("owner")
            self.assertEqual(access["guild_id"], self.test_gid)
            message = profiles.mismatch_message(access["guild_id"], "telegram")
            self.assertIn("https://t.me/original_bot", message)
            self.assertEqual(access["ally_code"], "123456789")

    async def test_wrong_bot_command_returns_link_without_running_handler(self):
        app = self.app("prod")
        cmd = core.Command("profile_test", AsyncMock(), "Profile", tier=core.TIER_REGISTERED)
        database.set_bot_state("telegram_bot_username", "original_bot", guild_id=0)
        access = {"guild_id": self.test_gid, "tier": "officer", "is_super_admin": False}
        message = {"chat": {"id": 123, "type": "private"}, "from": {"id": 123}, "text": "/profile_test", "message_id": 10}
        with patch.dict(core.COMMANDS, {"profile_test": cmd}), patch.object(guild_resolver, "resolve_access", return_value=access):
            await core._handle_message(app, message)
        cmd.handler.assert_not_awaited()
        self.assertIn("https://t.me/original_bot", app.api.send_message.await_args.args[1])

    async def test_removing_other_bot_from_group_does_not_unbind_it(self):
        store.bind_chat(-100, self.test_gid, "Group", "supergroup", "officer")
        with patch.object(profiles, "_worker_profile", "prod"):
            await core._handle_membership(self.app("prod"), {"chat": {"id": -100, "type": "supergroup"}, "new_chat_member": {"status": "kicked"}})
        self.assertEqual(store.chat_guild(-100), self.test_gid)

    def test_shared_jobs_start_only_once_and_guild_jobs_are_partitioned(self):
        loop = Mock()
        with patch.object(profiles, "_worker_profile", "test"):
            profiles.start_shared_loop(loop)
            self.assertEqual([g["id"] for g in profiles.served_guilds()], [self.test_gid])
        with patch.object(profiles, "_worker_profile", "prod"):
            profiles.start_shared_loop(loop)
            self.assertEqual([g["id"] for g in profiles.served_guilds()], [self.prod_gid])
        loop.start.assert_called_once()

    async def test_public_roster_hydrates_database_without_comlink(self):
        database.sync_guild_roster(self.prod_gid, [("123", "123456789", "Public Player", 3, "pid")])
        bot = SimpleNamespace(comlink=Mock(), guild_roster_caches={})
        cog = SimpleNamespace(bot=bot)
        with patch.object(profiles, "_worker_profile", "prod"):
            await ViolationsCog.update_roster_cache.coro(cog)
        self.assertEqual(bot.guild_roster_caches[self.prod_gid]["Public Player"], "123456789")
        self.assertEqual(bot.comlink.mock_calls, [])

    async def test_public_stats_cog_does_not_start_mass_sync_or_warm_calculator(self):
        with patch.object(profiles, "_worker_profile", "prod"):
            cog = StatRequirementsCog(SimpleNamespace(PLAYER_STATS_SYNC_HOURS=1))
            try:
                self.assertFalse(cog.player_units_sync_loop.is_running())
                self.assertFalse(cog.stat_calc_loop.is_running())
            finally:
                cog.cog_unload()

    async def test_concurrent_catalog_requests_share_one_comlink_fetch(self):
        comlink = Mock(get_game_data=Mock(return_value={}), get_localization=Mock(return_value={}))
        first, second = await asyncio.gather(_fetch_datacron_cache(comlink), _fetch_datacron_cache(comlink))
        self.assertEqual(first, second)
        comlink.get_game_data.assert_called_once()
        comlink.get_localization.assert_called_once()

    def test_shared_catalog_restores_numeric_keys_and_expires(self):
        shared_datacron_catalog.save({"seasons": {32: {"entries": {3: [["branch", "ability"]]}}}})
        loaded = shared_datacron_catalog.load()
        self.assertIn(32, loaded["seasons"])
        self.assertIn(3, loaded["seasons"][32]["entries"])
        with patch.object(shared_datacron_catalog.time, "time", return_value=0):
            self.assertIsNone(shared_datacron_catalog.load())

    async def test_broadcast_context_uses_assigned_pair_and_restores_caller(self):
        @profiles.guild_delivery
        async def send(token, guild_id):
            return token, profiles.current_profile()
        with patch.dict(os.environ, {"PROD_DISCORD_TOKEN": "public-token", "DISCORD_TOKEN": "test-token"}):
            self.assertEqual(await send("wrong-token", self.prod_gid), ("public-token", "prod"))
            self.assertEqual(profiles.current_profile(), "test")
        with patch.object(profiles, "_worker_profile", "test"):
            with self.assertRaises(ValueError):
                await send("test-token", self.prod_gid)

    def test_duplicate_worker_is_refused_but_other_profile_can_start(self):
        with worker_instance("discord", "test"):
            with self.assertRaises(RuntimeError):
                with worker_instance("discord", "test"):
                    self.fail("duplicate worker started")
            with worker_instance("discord", "prod"):
                pass
        with worker_instance("discord", "test"):
            pass

    def test_public_invite_does_not_request_antispam_permissions(self):
        public = discord_invite.required_permissions("prod")
        self.assertTrue(public.send_messages_in_threads)
        self.assertFalse(public.manage_messages)
        self.assertFalse(public.moderate_members)
        self.assertTrue(public.manage_roles)

    def test_web_rest_uses_selected_context(self):
        with patch.dict(os.environ, {"PROD_DISCORD_TOKEN": "public-token", "DISCORD_TOKEN": "test-token"}):
            with profiles.delivery_context("prod"):
                self.assertEqual(discord_rest._headers()["Authorization"], "Bot public-token")
            self.assertEqual(discord_rest._headers()["Authorization"], "Bot test-token")

    async def test_transfer_refuses_unavailable_destination_without_changing_assignment(self):
        with patch.dict(os.environ, {"PROD_BOTS_READY": "1", "PROD_DISCORD_TOKEN": "public", "PROD_TELEGRAM_BOT_TOKEN": "public-tg"}, clear=True), \
             patch.object(discord_rest, "list_bot_guilds", AsyncMock(return_value={})) as servers:
            with self.assertRaises(ValueError):
                await profiles.transfer_guild(self.test_gid, "prod")
        servers.assert_awaited_once_with(profile="prod")
        self.assertEqual(profiles.guild_profile(self.test_gid), "test")

    async def test_transfer_checks_telegram_before_committing_and_can_return_to_test(self):
        store.bind_chat(-100, self.test_gid, "Group", "supergroup", "officer")
        api = SimpleNamespace(get_me=AsyncMock(return_value={"id": 42}),
                              call=AsyncMock(return_value={"status": "left"}), close=AsyncMock())
        env = {"PROD_BOTS_READY": "1", "PROD_DISCORD_TOKEN": "public", "PROD_TELEGRAM_BOT_TOKEN": "public-tg",
               "DISCORD_TOKEN": "original", "TELEGRAM_BOT_TOKEN": "original-tg"}
        with patch.dict(os.environ, env, clear=True), \
             patch.object(discord_rest, "list_bot_guilds", AsyncMock(return_value={"Test": "Guild"})), \
             patch("tg_bot.api.TelegramAPI", return_value=api):
            with self.assertRaises(ValueError):
                await profiles.transfer_guild(self.test_gid, "prod")
            self.assertEqual(profiles.guild_profile(self.test_gid), "test")
            api.call.return_value = {"status": "administrator"}
            await profiles.transfer_guild(self.test_gid, "prod")
            self.assertEqual(profiles.guild_profile(self.test_gid), "prod")
            await profiles.transfer_guild(self.test_gid, "test")
            self.assertEqual(profiles.guild_profile(self.test_gid), "test")
        self.assertEqual(store.chat_guild(-100), self.test_gid)
        self.assertEqual(api.close.await_count, 3)

    async def test_stored_panel_returns_link_after_guild_transfer(self):
        database.set_bot_state(profiles.state_key("discord_bot_id", "prod"), "7654321", guild_id=0)
        inter = SimpleNamespace(response=SimpleNamespace(is_done=lambda: False, send_message=AsyncMock()))
        with patch.object(profiles, "_worker_profile", "test"):
            self.assertFalse(await guild_resolver.check_bot_profile(inter, self.prod_gid))
        self.assertIn("https://discord.com/users/7654321", inter.response.send_message.await_args.args[0])

    async def test_shared_sync_announces_public_omicron_with_public_bot_and_no_joke(self):
        database.update_guild_config(self.prod_gid, omicron_channel_id="987")
        database.set_omicron_phrase("CHAR", "PRIVATE JOKE", guild_id=self.prod_gid, updated_by="owner")
        bot = SimpleNamespace(get_channel=Mock(), fetch_channel=AsyncMock())
        cog = SimpleNamespace(bot=bot, _omicron_line=StatRequirementsCog._omicron_line,
                              OMICRON_COMBINE_THRESHOLD=10)
        with patch.object(discord_rest, "send_message", AsyncMock()) as send, \
             patch.object(database, "mark_activity_event_announced") as mark:
            await StatRequirementsCog._announce_omicrons(cog, [(1, "123456789", "CHAR", "skill", self.prod_gid)])
        send.assert_awaited_once()
        self.assertEqual(send.await_args.args[0], "987")
        self.assertEqual(send.await_args.kwargs["profile"], "prod")
        self.assertNotIn("PRIVATE JOKE", send.await_args.args[1])
        bot.get_channel.assert_not_called()
        mark.assert_called_once_with(1)

    async def test_oauth_callback_uses_application_selected_at_login(self):
        from urllib.parse import parse_qs, urlsplit
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from starlette.middleware.sessions import SessionMiddleware
        from web import auth
        app = FastAPI()
        app.add_middleware(SessionMiddleware, secret_key="test-oauth")
        app.include_router(auth.router)
        api = AsyncMock()
        api.post.return_value = SimpleNamespace(status_code=200, json=lambda: {"access_token": "temporary"})
        api.get.return_value = SimpleNamespace(status_code=200, json=lambda: {"id": "555", "username": "User"})
        manager = AsyncMock()
        manager.__aenter__.return_value = api
        with patch.dict(os.environ, {"PROD_DISCORD_CLIENT_ID": "public-id", "PROD_DISCORD_CLIENT_SECRET": "public-secret"}), \
             patch.object(auth, "DISCORD_CLIENT_ID", "original-id"), \
             patch.object(auth, "DISCORD_CLIENT_SECRET", "original-secret"), \
             patch.object(auth.httpx, "AsyncClient", return_value=manager), TestClient(app) as client:
            response = client.get("/login?bot_profile=prod", follow_redirects=False)
            query = parse_qs(urlsplit(response.headers["location"]).query)
            self.assertEqual(query["client_id"], ["public-id"])
            response = client.get("/callback", params={"code": "fake", "state": query["state"][0]}, follow_redirects=False)
            self.assertEqual(response.status_code, 307)
        self.assertEqual(api.post.await_args.kwargs["data"]["client_id"], "public-id")
        self.assertEqual(api.post.await_args.kwargs["data"]["client_secret"], "public-secret")

    async def test_fresh_catalog_read_does_not_wait_for_heavy_sync_lock(self):
        shared_datacron_catalog.save({"seasons": {}})
        comlink = Mock()
        with patch.object(sync_queue, "_try_lock", return_value=False) as acquire:
            value = await asyncio.wait_for(_fetch_datacron_cache(comlink), timeout=0.5)
        self.assertEqual(value["seasons"], {})
        acquire.assert_not_called()
        self.assertEqual(comlink.mock_calls, [])

    async def test_support_reply_uses_original_bot_even_in_other_admin_context(self):
        from services import feedback
        from web.routes import support
        with profiles.delivery_context("prod"):
            store.touch_user({"id": 123}, dm_open=True)
            report_id = feedback.create("tg:123", "Player", None, "Question", user_agent="Telegram")
        self.assertEqual(database.get_bug_report(report_id)["bot_profile"], "prod")
        with profiles.delivery_context("test"):
            await support.admin_feedback_update(report_id, status="done", reply="Fixed", back="open", user={"discord_id": "admin"})
        self.assertEqual(outbox.pending(profile="test"), [])
        self.assertEqual(len(outbox.pending(profile="prod")), 1)
        self.assertEqual(database.get_bug_report(report_id)["status"], "done")


if __name__ == "__main__":
    unittest.main()
