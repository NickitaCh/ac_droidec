import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.middleware.sessions import SessionMiddleware

import database
from web import deps
from web.deps import require_member_access
from web.routes import guild_dashboard


class ProfileAccountsTests(unittest.TestCase):
    def test_switch_persists_across_pages_and_rejects_foreign_code(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(
            database, "DB_NAME", str(Path(directory) / "test.db")
        ):
            database.init_db()
            database.set_user_registration("1", "123456789", "Main", guild_id=1)
            database.set_user_registration("1", "234567891", "Alt", guild_id=0)
            database.set_user_registration("2", "345678912", "Foreign", guild_id=0)
            app = FastAPI()
            app.add_middleware(SessionMiddleware, secret_key="profile-test-only")
            app.include_router(guild_dashboard.router)

            @app.get("/test-session")
            def session(request: guild_dashboard.Request):
                request.session.setdefault("user", {"discord_id": "1"})
                user = deps.get_current_user(request)
                return {"code": user["active_ally_code"], "csrf": user["account_switch_csrf"]}

            with TestClient(app) as client, patch.object(
                deps, "_refresh_access", return_value={
                    "discord_id": "1", "guild_id": 1, "tier": "member", "is_super_admin": False,
                    "bot_profile": "test",
                }
            ), patch.object(deps, "_with_subscription_status", side_effect=lambda user: user):
                initial = client.get("/test-session").json()
                self.assertEqual(initial["code"], "123456789")
                for code, csrf in [("345678912", initial["csrf"]), ("234567891", "wrong")]:
                    response = client.post("/me/account", data={"ally_code": code, "csrf": csrf})
                    self.assertEqual(response.status_code, 403)
                response = client.post("/me/account", data={
                    "ally_code": "234567891", "csrf": initial["csrf"], "return_to": "/mod-loadouts",
                }, follow_redirects=False)
                self.assertEqual(response.headers["location"], "/mod-loadouts")
                self.assertEqual(client.get("/test-session").json()["code"], "234567891")
                self.assertEqual(client.get("/me", follow_redirects=False).headers["location"], "/player/234567891")
                database.delete_user_registration("1", "234567891", guild_id=0)
                self.assertEqual(client.get("/me", follow_redirects=False).headers["location"], "/player/123456789")

    def test_external_alt_opens_personal_card_and_links_back_to_main(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(
            database, "DB_NAME", str(Path(directory) / "test.db")
        ):
            database.init_db()
            database.set_user_registration("1", "123456789", "Main", guild_id=1)
            database.set_user_registration("2", "345678912", "Foreign", guild_id=0)
            app = FastAPI()
            app.add_middleware(SessionMiddleware, secret_key="profile-test-only")
            app.include_router(guild_dashboard.router)
            app.dependency_overrides[require_member_access] = lambda: {
                "discord_id": "1", "guild_id": 1, "tier": "member",
            }
            with TestClient(app) as client, patch(
                "swgoh_comlink.SwgohComlink"
            ) as comlink, patch.object(guild_dashboard.dashboard_data, "get_player_card") as guild_card:
                comlink.return_value.get_player.return_value = {"name": "Alt", "guildId": "unconnected"}
                response = client.post("/me/alt", data={"ally_code": "234567891"})
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.url.path, "/player/234567891")
                self.assertIn('href="/player/123456789">Main</a>', response.text)
                self.assertIn("Альт «Alt» привязан", response.text)
                self.assertEqual(database.get_user_registration("1", guild_id=1), ("123456789", "Main"))
                self.assertEqual(database.get_user_registration("1", guild_id=0), ("234567891", "Alt"))
                response = client.get("/player/345678912", follow_redirects=False)
                self.assertEqual(response.headers["location"], "/me")
                guild_card.assert_not_called()

                guild_card.return_value = None
                response = client.get("/me")
                self.assertEqual(response.url.path, "/player/123456789")
                self.assertIn('href="/player/234567891">Alt</a>', response.text)
                guild_card.assert_called_once_with(1, "123456789")
