import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch
from types import SimpleNamespace

from fastapi import FastAPI
from starlette.requests import Request

import database
from cogs.bot_invite import BotInviteCog
from services import bot_presence, bot_profiles
from web.routes import admin, dashboard, telegram


class PublicBotLinkTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.patches = [
            patch.object(database, "DB_NAME", str(Path(self.tmp.name) / "test.db")),
            patch.dict(os.environ, {"DISCORD_CLIENT_ID": "111", "PROD_DISCORD_CLIENT_ID": "222"}),
        ]
        for p in self.patches:
            p.start()
        database.init_db()
        database.set_bot_state("telegram_bot_username", "old_test_bot", guild_id=0)
        database.set_bot_state("prod:telegram_bot_username", "public_bot", guild_id=0)

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()
        self.tmp.cleanup()

    def request(self, path):
        app = FastAPI()
        app.state.public_bot_profile = lambda: "prod"
        return Request({"type": "http", "scheme": "https", "server": ("test", 443), "path": path,
                        "query_string": b"", "headers": [], "app": app, "session": {}})

    async def test_landing_uses_public_pair_even_in_test_context(self):
        with bot_profiles.delivery_context("test"):
            response = await dashboard.home(self.request("/"), user=None)
        self.assertIn("client_id=222", response.context["bot_invite_url"])
        self.assertIn("https://t.me/public_bot", response.body.decode())
        self.assertNotIn("old_test_bot", response.body.decode())

    def test_public_telegram_link_is_separate_from_personal_link(self):
        with bot_profiles.delivery_context("test"):
            self.assertEqual(telegram.public_bot_username(), "public_bot")
            self.assertEqual(telegram.bot_username(), "old_test_bot")

    async def test_start_page_always_invites_public_pair(self):
        user = {"tier": "officer", "bot_profile": "test", "username": "Officer"}
        with bot_profiles.delivery_context("test"):
            response = await dashboard.start_guide(self.request("/start"), user=user)
        html = response.body.decode()
        self.assertIn("client_id=222", html)
        self.assertIn("https://t.me/public_bot", html)
        self.assertNotIn("old_test_bot", html)

    async def test_discord_invite_command_always_invites_public_bot(self):
        inter = SimpleNamespace(response=SimpleNamespace(send_message=AsyncMock()))
        with bot_profiles.delivery_context("test"):
            await BotInviteCog.invite.callback(BotInviteCog(Mock()), inter)
        payload = inter.response.send_message.await_args.kwargs
        self.assertIn("client_id=222", payload["view"].children[0].url)
        self.assertNotIn("AC", payload["embed"].title)
        self.assertNotIn("антиспам", payload["embed"].description)

    async def test_super_admin_checklist_contains_explicit_test_links(self):
        user = {"tier": "officer", "bot_profile": "prod", "is_super_admin": True,
                "real_super_admin": True, "username": "Admin"}
        with patch.object(bot_presence, "overview", AsyncMock(return_value=({}, None))):
            response = await admin.guilds_page(self.request("/admin/guilds"), user=user)
        html = response.body.decode()
        self.assertIn("client_id=111", html)
        self.assertIn("https://t.me/old_test_bot", html)
        self.assertIn("Как подключить гильдию к тестовым ботам", html)
        self.assertIn("Запись, которая уже существует, повторно не создавайте", html)


if __name__ == "__main__":
    unittest.main()
