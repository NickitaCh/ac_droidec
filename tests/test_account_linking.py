import base64
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from cryptography.fernet import Fernet
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.middleware.sessions import SessionMiddleware

import database
from services import account_linking, ea_browser, ea_connect as ea, game_account as ga, game_lab as lab
from web.routes import game_account as route
from web.deps import require_officer_access


def read_request(player):
    return ga.pack([(4, 2, b"PlayerRpc"), (5, 2, b"GetInitialData"), (6, 2, b""), (7, 2, player.encode()), (8, 2, b"secret-session")])


class LinkingTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.patches = [patch.object(database, "DB_NAME", str(Path(self.tmp.name) / "test.db")),
                        patch.object(ga, "require_pilot"),
                        patch("services.bot_profiles.guild_profile", return_value="test"),
                        patch("services.bot_profiles.current_profile", return_value="test"),
                        patch.object(ea, "config", return_value={})]
        for p in self.patches:
            p.start()
        database.init_db()
        database.set_user_registration("admin", "123456789", "Main", guild_id=1)
        key = Path(self.tmp.name) / "cache" / "game-account.key"
        key.parent.mkdir()
        key.write_bytes(Fernet.generate_key())
        self.player = {"playerId": "new-alt", "allyCode": 987654321, "name": "New alt", "level": 85, "guildId": "game-guild", "guildName": "Other guild"}
        self.comlink = Mock()
        self.comlink.get_player.return_value = self.player
        self.credentials = {"expected_id": "new-alt", "read_request": base64.b64encode(read_request("new-alt")).decode()}
        self.summary = {"read_at": 1, "inventory_bytes": 10, "inventory_records": 1}
        self.user = {"discord_id": "admin", "guild_id": 1, "bot_profile": "test", "tier": "officer", "is_super_admin": False, "username": "Officer"}
        self.app = FastAPI()
        self.app.add_middleware(SessionMiddleware, secret_key="test")
        self.app.include_router(route.router, prefix="/game-account")
        self.app.dependency_overrides[require_officer_access] = lambda: self.user

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()
        self.tmp.cleanup()

    async def start(self):
        state = {"stage": "code", "cookies": [], "fields": {}, "url": "https://signin.ea.com/"}
        with patch.object(ea_browser, "start", AsyncMock(return_value=state)):
            return await ea.start_registration("admin", True, "test@example.com", True)

    async def test_new_alt_requires_no_previous_alt_and_preserves_main(self):
        flow = await self.start()
        self.assertEqual(database.get_user_registrations("admin", 1), [("123456789", "Main", 1)])
        async def restore(cfg, tokens, expected, validate_preview=None):
            self.assertIsNone(expected)
            await validate_preview("new-alt")
            return self.credentials, self.summary
        with patch.object(ea_browser, "finish", AsyncMock(return_value={"stage": "complete", "longlive": "access"})), patch.object(ea, "_exchange", AsyncMock(return_value={})), patch.object(ea, "_import", side_effect=restore):
            result = await ea.finish_registration("admin", True, flow, "123456", self.comlink)
        self.assertEqual(database.get_user_registration("admin", 1), ("123456789", "Main"))
        self.assertIn(("987654321", "New alt", 0), database.get_user_registrations("admin", 1))
        self.assertEqual(ea.target_for_registration("admin", 1, "987654321"), result["target"])
        self.assertIsNone(ea.target_for_registration("other", 1, "987654321"))
        self.assertEqual(ga.status("admin", 1, "test", ally_code="987654321")["state"], "connected")
        self.assertEqual(lab._row(result["target"])["game_guild_name"], "Other guild")
        self.assertEqual([r["id"] for r in lab.accounts()], ["notest"])
        with lab._db() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM user_mapping WHERE ally_code='987654321'").fetchone()[0], 0)

    async def test_wrong_mode_cannot_demote_main_or_replace_it(self):
        with self.assertRaises(ga.AccountError):
            await account_linking.validate_player("admin", False, "new-alt", self.comlink)
        self.comlink.get_player.return_value = {**self.player, "allyCode": 123456789}
        with self.assertRaises(ga.AccountError):
            await account_linking.validate_player("admin", True, "new-alt", self.comlink)
        self.assertEqual(database.get_user_registration("admin", 1), ("123456789", "Main"))

    async def test_registered_to_another_owner_is_not_transferred(self):
        database.set_user_registration("other", "987654321", "Alt", guild_id=1)
        with self.assertRaises(ga.AccountError):
            await account_linking.validate_player("admin", True, "new-alt", self.comlink)
        self.assertEqual(database.get_user_registration("other", 1), ("987654321", "Alt"))

    async def test_preview_validation_runs_before_import_and_private_read(self):
        cfg = {"guest_template": base64.b64encode(ga.pack([(4, 2, b"AuthRpc"), (5, 2, b"DoAuthGuest"), (6, 2, ga.pack([(1, 2, b"uid")]))])).decode()}
        auth = ga.pack([(1, 2, b"isolated"), (2, 2, b"guest-token")])
        preview = ga.pack([(5, 2, b"new-alt"), (6, 2, b"ea-player")])
        async def validate(pid):
            await account_linking.validate_player("admin", False, pid, self.comlink)
        with patch.object(ea, "_game", AsyncMock(side_effect=[auth, preview])) as game, patch.object(ga, "read_initial", AsyncMock()) as read:
            with self.assertRaises(ga.AccountError):
                await ea._import(cfg, {"access": "access", "nucleus": "ea-player"}, None, validate_preview=validate)
            self.assertEqual([c.args[1] for c in game.await_args_list], ["DoAuthGuest", "GetNucleusPlayerPreview"])
            read.assert_not_awaited()

    async def test_pending_mode_and_actor_are_bound(self):
        flow = await self.start()
        with patch.object(ea_browser, "finish", AsyncMock()) as finish:
            for owner, alt in [("other", True), ("admin", False)]:
                with self.assertRaises(ga.AccountError):
                    await ea.finish_registration(owner, alt, flow, "123456", self.comlink)
            finish.assert_not_awaited()

    async def test_cancelled_flow_does_not_register_alt(self):
        flow = await self.start()
        async def restore(cfg, tokens, expected, validate_preview=None):
            await validate_preview("new-alt")
            with ea._db() as conn:
                conn.execute("DELETE FROM ea_browser_pending WHERE id=?", (flow,))
            return self.credentials, self.summary
        with patch.object(ea_browser, "finish", AsyncMock(return_value={"stage": "complete", "longlive": "access"})), patch.object(ea, "_exchange", AsyncMock(return_value={})), patch.object(ea, "_import", side_effect=restore):
            with self.assertRaises(ga.AccountError):
                await ea.finish_registration("admin", True, flow, "123456", self.comlink)
        self.assertEqual(database.get_user_registrations("admin", 1), [("123456789", "Main", 1)])
        self.assertIsNone(ea.target_for_registration("admin", 1, "987654321"))

    def test_simple_form_is_available_to_officer_and_uses_email_then_code(self):
        import re
        with TestClient(self.app) as client, patch.object(ea, "available", return_value=True):
            result = client.get("/game-account/link?alt=1")
            self.assertEqual(result.status_code, 200)
            self.assertIn("Привязать альт", result.text)
            self.assertIn('name="ea_email"', result.text)
            self.assertNotIn("name=\"ally_code\"", result.text)
            self.assertNotIn('name="session_secret"', result.text)
            csrf = re.search(r'name="csrf" value="([^"]+)"', result.text).group(1)
            with patch.object(ea, "start_registration", AsyncMock()) as start, patch.object(ea, "pending", return_value={"id": "flow"}):
                result = client.post("/game-account/link/ea-start", data={"csrf": csrf, "alt": "1", "ea_email": "test@example.com", "consent": "yes"})
                self.assertEqual(start.await_args.args, ("admin", True, "test@example.com", True))
                self.assertIn('name="one_time_code"', result.text)
            with patch.object(ea, "finish_registration", AsyncMock(return_value={"target": "owned-target"})) as finish, patch("web.routes.registration._get_comlink", return_value=self.comlink):
                result = client.post("/game-account/link/ea-finish", data={"csrf": csrf, "alt": "1", "flow_id": "flow", "one_time_code": "123456"}, follow_redirects=False)
                self.assertEqual(result.status_code, 303)
                self.assertEqual(finish.await_args.args[:4], ("admin", True, "flow", "123456"))
            self.assertEqual(client.post("/game-account/link/ea-start", data={"csrf": "wrong"}).status_code, 403)
            self.assertEqual(client.get("/game-account/link?alt=2").status_code, 400)

    def test_simple_form_does_not_expose_another_users_success(self):
        with lab._db() as conn:
            conn.execute("INSERT INTO game_lab_accounts (id,ally_code,player_id,name,level,game_guild_id,game_guild_name,added_by,added_at) VALUES ('foreign','555555555','foreign-player','Foreign',85,'','','other',0)")
        with TestClient(self.app) as client, patch.object(ea, "linked", return_value=True):
            self.assertEqual(client.get("/game-account/link?linked=foreign").status_code, 404)
            self.assertEqual(client.get("/game-account/link?linked=unknown").status_code, 404)

    def test_auth_forms_do_not_load_session_recording(self):
        with TestClient(self.app, base_url="https://swgoh-sng.ru") as client:
            result = client.get("/game-account/link?alt=1")
            self.assertEqual(result.status_code, 200)
            self.assertNotIn("mc.yandex.ru", result.text)
            self.assertNotIn("webvisor", result.text)


if __name__ == "__main__":
    unittest.main()
