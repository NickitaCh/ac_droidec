import asyncio
import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from jinja2 import Environment, FileSystemLoader
from starlette.requests import Request
from starlette.middleware.sessions import SessionMiddleware

import database
from services import bot_profiles as profiles, feature_flags, guild_admin
from web import deps
from web.routes import admin


def request(path="/admin/guilds", session=None):
    return Request({"type": "http", "scheme": "https", "server": ("test", 443),
                    "path": path, "query_string": b"", "headers": [],
                    "session": session if session is not None else {}})


class BotProfileTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_patch = patch.object(database, "DB_NAME", str(Path(self.tmp.name) / "test.db"))
        self.db_patch.start()
        database.init_db()
        self.gid = database.create_guild(name="Existing", ally_code="123456789", discord_guild_id="123",
                                         member_role_id="", officer_role_id="")
        self.user = {"discord_id": "owner", "username": "Admin", "guild_id": self.gid,
                     "is_super_admin": True, "tier": "officer"}

    def tearDown(self):
        self.db_patch.stop()
        self.tmp.cleanup()

    def production_guild(self):
        return database.create_guild(name="Public", ally_code="223456789", discord_guild_id="234",
                                     member_role_id="", officer_role_id="", bot_profile="prod")

    def http_app(self):
        app = FastAPI()
        app.add_middleware(profiles.ProfileMiddleware)
        app.add_middleware(SessionMiddleware, secret_key="test-profile-session")
        app.include_router(admin.router, prefix="/admin")

        @app.post("/__login")
        def login(req: Request):
            req.session["user"] = self.user
            return {"ok": True}

        return app

    def test_http_switch_to_empty_prod_and_back(self):
        from services import bot_presence
        with patch.object(deps, "_refresh_access", return_value=self.user), \
             patch.object(deps, "_with_subscription_status", side_effect=lambda user: user), \
             patch.object(bot_presence, "overview", AsyncMock(return_value=({}, None))), \
             TestClient(self.http_app()) as client:
            client.post("/__login")
            response = client.post("/admin/switch-profile", data={"profile": "prod"})
            self.assertEqual(response.status_code, 200)
            self.assertIn("В этой версии пока нет гильдий", response.text)
            self.assertNotIn("<strong>Existing</strong>", response.text)
            response = client.post("/admin/switch-profile", data={"profile": "test"})
            self.assertEqual(response.status_code, 200)
            self.assertIn("<strong>Existing</strong>", response.text)
            self.assertEqual(database.get_guild_config(self.gid)["bot_profile"], "test")

    def test_http_profile_actions_require_super_admin_and_trusted_origin(self):
        with patch.object(deps, "_refresh_access", return_value={**self.user, "is_super_admin": False}), \
             TestClient(self.http_app()) as client:
            client.post("/__login")
            self.assertEqual(client.post("/admin/switch-profile", data={"profile": "prod"}).status_code, 403)
            self.assertEqual(client.post(f"/admin/guilds/{self.gid}/profile", data={"profile": "prod"}).status_code, 403)
        with patch.object(deps, "_refresh_access", return_value=self.user), TestClient(self.http_app()) as client:
            client.post("/__login")
            self.assertEqual(client.post("/admin/switch-profile", data={"profile": "prod"}, headers={"Origin": "https://foreign.invalid"}).status_code, 403)

    def test_feature_form_only_changes_selected_profile(self):
        prod = self.production_guild()
        database.set_bot_state("feature:tasks", "1", guild_id=self.gid)
        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}
        req = request("/admin/features/save")
        req._receive = receive
        asyncio.run(admin.features_save(req, user={**self.user, "bot_profile": "prod"}))
        self.assertTrue(feature_flags.is_enabled(self.gid, "tasks"))
        self.assertFalse(feature_flags.is_enabled(prod, "tasks"))

    def test_migration_keeps_existing_guild_and_settings_on_test(self):
        with closing(sqlite3.connect(database.DB_NAME)) as conn:
            conn.execute("ALTER TABLE guilds DROP COLUMN bot_profile")
            conn.commit()
        cfg = database.get_guild_config(self.gid)
        self.assertEqual(cfg["bot_profile"], "test")
        self.assertEqual(cfg["name"], "Existing")
        self.assertEqual(cfg["ally_code"], "123456789")
        self.assertEqual(database.get_guild_config(self.gid)["bot_profile"], "test")

    def test_switching_web_context_does_not_reassign_guild(self):
        prod = self.production_guild()
        result = deps._apply_guild_switch(request(session={"bot_profile": "prod"}), self.user)
        self.assertEqual(result["guild_id"], prod)
        self.assertEqual([g["id"] for g in result["switchable_guilds"]], [prod])
        self.assertEqual(database.get_guild_config(self.gid)["bot_profile"], "test")

    def test_empty_profile_has_no_fallback_to_test_guild(self):
        result = deps._apply_guild_switch(request(session={"bot_profile": "prod", "view_as": "member"}), self.user)
        self.assertIsNone(result["guild_id"])
        self.assertEqual(result["switchable_guilds"], [])
        self.assertTrue(result["is_super_admin"])

    def test_other_profile_guild_override_is_ignored(self):
        prod = self.production_guild()
        result = deps._apply_guild_switch(request(session={"bot_profile": "prod", "impersonate_guild_id": self.gid}), self.user)
        self.assertEqual(result["guild_id"], prod)

    def test_plain_user_cannot_select_profile_through_session(self):
        user = {**self.user, "is_super_admin": False}
        result = deps._apply_guild_switch(request(session={"bot_profile": "prod"}), user)
        self.assertNotIn("bot_profile", result)
        self.assertEqual(result["guild_id"], self.gid)

    def test_transfer_requires_ready_workers_and_credentials(self):
        for env in ({}, {"PROD_BOTS_READY": "1"}):
            with patch.dict(os.environ, env, clear=True), self.assertRaises(ValueError):
                profiles.assign_guild(self.gid, "prod")
        self.assertEqual(database.get_guild_config(self.gid)["bot_profile"], "test")

    def test_explicit_transfer_round_trip_preserves_settings(self):
        database.update_guild_config(self.gid, tasks_log_channel_id="987")
        with patch.dict(os.environ, {"PROD_BOTS_READY": "1", "PROD_DISCORD_TOKEN": "dummy",
                                     "PROD_TELEGRAM_BOT_TOKEN": "dummy"}):
            profiles.assign_guild(self.gid, "prod")
        self.assertEqual(database.get_guild_config(self.gid)["bot_profile"], "prod")
        profiles.assign_guild(self.gid, "test")
        self.assertEqual(database.get_guild_config(self.gid)["tasks_log_channel_id"], "987")

    def test_invalid_profile_is_rejected(self):
        with self.assertRaises(ValueError):
            profiles.assign_guild(self.gid, "other")

    def test_product_policy_overrides_guild_feature_toggle(self):
        prod = self.production_guild()
        database.set_bot_state("feature:tw_order", "1", guild_id=prod)
        self.assertFalse(feature_flags.is_enabled(prod, "tw_order"))
        with self.assertRaises(ValueError):
            feature_flags.set_enabled(prod, "tw_order", True, "owner")
        self.assertTrue(feature_flags.is_enabled(self.gid, "tw_order"))
        for key in ("birthdays", "omicron", "stat_requirements", "mod_builder", "mod_search", "tasks"):
            self.assertTrue(feature_flags.is_enabled(prod, key), key)

    def test_removed_routes_include_nested_actions_but_not_similar_paths(self):
        for path in ("/tw", "/tw/def/plans/1/notify", "/mod-analysis/compare", "/omicron/phrases/set", "/admin/fun/enable"):
            self.assertFalse(profiles.path_available(path, "prod"), path)
            self.assertTrue(profiles.path_available(path, "test"), path)
        for path in ("/mod-progress", "/mod-builder", "/admin/guilds", "/omicrons/report", "/twilight"):
            self.assertTrue(profiles.path_available(path, "prod"), path)

    def test_prod_route_policy_applies_to_super_admin(self):
        prod = self.production_guild()
        req = request("/tw/def", {"user": self.user, "bot_profile": "prod"})
        with patch.object(deps, "_refresh_access", return_value=self.user):
            with self.assertRaises(HTTPException) as raised:
                deps.get_current_user(req)
        self.assertEqual(raised.exception.status_code, 404)

    def test_profile_switch_resets_guild_override_only(self):
        req = request(session={"impersonate_guild_id": self.gid, "view_as": "officer"})
        response = asyncio.run(admin.switch_profile(req, profile="prod", user={"real_super_admin": True}))
        self.assertEqual(response.status_code, 303)
        self.assertEqual(req.session["bot_profile"], "prod")
        self.assertNotIn("impersonate_guild_id", req.session)
        self.assertEqual(req.session["view_as"], "officer")

    def test_admin_guild_lists_are_request_local(self):
        prod = self.production_guild()
        async def run():
            async def query(profile):
                token = profiles.web_profile.set(profile)
                try:
                    await asyncio.sleep(0)
                    return [g["id"] for g in guild_admin.list_guilds()]
                finally:
                    profiles.web_profile.reset(token)
            return await asyncio.gather(query("test"), query("prod"))
        self.assertEqual(asyncio.run(run()), [[self.gid], [prod]])

    def test_production_navigation_has_no_removed_sections(self):
        env = Environment(loader=FileSystemLoader("web/templates"))
        template = env.get_template("base.html")
        req = SimpleNamespace(url=SimpleNamespace(path="/admin/guilds"), scope={})
        user = {**self.user, "real_super_admin": True, "bot_profile": "prod"}
        html = template.render(request=req, user=user)
        for href in ("/tw", "/mod-analysis", "/mod-scan", "/admin/fun", "/farm", "/mod-ku", "/omicron/phrases"):
            self.assertNotIn('href="' + href + '"', html)
        for href in ("/admin/guilds", "/admin/access", "/mod-progress", "/mod-builder", "/birthdays"):
            self.assertIn('href="' + href + '"', html)
        self.assertIn('action="/admin/switch-profile"', html)
        self.assertIn('class="brand-full">Droidec', html)


if __name__ == "__main__":
    unittest.main()
