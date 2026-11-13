import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from cryptography.fernet import Fernet
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.middleware.sessions import SessionMiddleware

import database
from services import bot_profiles, game_account as ga, game_lab as lab
from web.routes import game_lab as route
from web.deps import require_super_admin


def session(player_id):
    return ga.pack([(4, 2, b"PlayerRpc"), (5, 2, b"GetInitialData"), (6, 2, b""),
                    (7, 2, player_id.encode()), (8, 2, b"private-session-token")])


class LabTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.patches = [
            patch.object(database, "DB_NAME", str(Path(self.tmp.name) / "test.db")),
            patch.object(ga, "_identity", return_value=lab.NOTEST_PLAYER_ID),
            patch.object(ga, "status", return_value={"state": "connected", "provider": "ea", "summary": {"read_at": 1, "inventory_bytes": 573, "inventory_records": 10}}),
        ]
        for p in self.patches:
            p.start()
        key = Path(self.tmp.name) / "cache" / "game-account.key"
        key.parent.mkdir()
        key.write_bytes(Fernet.generate_key())
        self.user = {"discord_id": "admin", "guild_id": 1, "bot_profile": "test", "tier": "officer", "is_super_admin": True, "username": "Admin"}
        self.app = FastAPI()
        self.app.add_middleware(SessionMiddleware, secret_key="test")
        self.app.include_router(route.router, prefix="/admin/game-lab")
        self.app.dependency_overrides[require_super_admin] = lambda: self.user

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()
        self.tmp.cleanup()

    def csrf(self, client):
        response = client.get("/admin/game-lab")
        import re
        return re.search(r'name="csrf" value="([^"]+)"', response.text).group(1)

    async def add(self, code="123456789", player_id="test-player"):
        comlink = Mock()
        comlink.get_player.return_value = {"allyCode": code, "playerId": player_id, "name": "Second test", "level": 85, "guildName": "Test Guild", "guildId": "game-guild"}
        return await lab.add_account("admin", code, True, comlink)

    def test_default_is_only_notest_and_catalog_is_honest(self):
        with TestClient(self.app) as client:
            result = client.get("/admin/game-lab")
            self.assertEqual(result.status_code, 200)
            self.assertIn("notest", result.text)
            self.assertIn(lab.NOTEST_CODE, result.text)
            self.assertNotIn("881789523", result.text)
            self.assertNotIn(lab.NOTEST_PLAYER_ID, result.text)
            self.assertEqual(result.headers["cache-control"], "no-store")
            self.assertEqual(result.text.count('class="card lab-capability"'), 28)
            self.assertIn("Браузерный вход EA/Google ещё не реализован", result.text)
            self.assertEqual(client.get("/admin/game-lab?ally_code=881789523").status_code, 400)
            self.assertEqual(client.get("/admin/game-lab?target=881789523").status_code, 404)

    def test_server_rejects_officer_prod_and_view_as_for_get_and_post(self):
        for changes in [{"is_super_admin": False}, {"bot_profile": "prod"}, {"view_as": "officer"}]:
            original = dict(self.user)
            self.user.update(changes)
            with TestClient(self.app) as client, patch.object(lab, "refresh", AsyncMock()) as read:
                self.assertEqual(client.get("/admin/game-lab").status_code, 403)
                self.assertEqual(client.post("/admin/game-lab/read").status_code, 403)
                read.assert_not_awaited()
            self.user = original
        self.assertFalse(bot_profiles.path_available("/admin/game-lab/read", "prod"))
        self.app.dependency_overrides.clear()
        with TestClient(self.app) as client:
            self.assertEqual(client.get("/admin/game-lab").status_code, 401)

    def test_csrf_and_target_injection_never_touch_network(self):
        with TestClient(self.app) as client, patch.object(lab, "refresh", AsyncMock()) as read:
            csrf = self.csrf(client)
            self.assertEqual(client.post("/admin/game-lab/read", data={"csrf": "wrong"}).status_code, 403)
            self.assertEqual(client.post("/admin/game-lab/read", data={"csrf": csrf, "ally_code": "881789523"}).status_code, 400)
            self.assertEqual(client.post("/admin/game-lab/UpdateMods", data={"csrf": csrf}).status_code, 404)
            read.assert_not_awaited()

    async def test_pinned_notest_identity_and_fixed_refresh(self):
        with patch.object(ga, "_identity", return_value="main-account"), patch.object(ga, "refresh", AsyncMock()) as read:
            with self.assertRaises(ga.AccountError):
                await lab.refresh()
            read.assert_not_awaited()
        with patch.object(ga, "refresh", AsyncMock(return_value={})) as read:
            await lab.refresh()
            self.assertEqual(read.await_args.args, (lab.NOTEST_OWNER, 1, "test"))
            self.assertEqual(read.await_args.kwargs, {"ally_code": lab.NOTEST_CODE})

    async def test_add_account_is_explicit_and_does_not_register_in_real_guild(self):
        with patch.object(database, "set_user_registration") as register:
            target = await self.add()
            self.assertNotEqual(target, "notest")
            self.assertEqual(lab.current(target)["game_guild_name"], "Test Guild")
            self.assertEqual(lab.current(target)["state"], "unconnected")
            register.assert_not_called()
        comlink = Mock()
        for code, consent in [("123456789", False), ("000000000", True)]:
            with self.assertRaises(ga.AccountError):
                await lab.add_account("admin", code, consent, comlink)
        comlink.get_player.assert_not_called()
        self.assertEqual(len(lab.accounts()), 2)
        self.assertEqual(lab.accounts()[0]["id"], "notest")

    async def test_session_identity_allowlist_encryption_and_removal(self):
        target = await self.add()
        with patch.object(ga, "read_initial", AsyncMock(return_value={"read_at": 1, "inventory_bytes": 10, "inventory_records": 2})) as read:
            with self.assertRaises(ga.AccountError):
                await lab.connect(target, "ea", session("main-account"), True)
            with self.assertRaises(ga.AccountError):
                await lab.connect(target, "ea", session("test-player").replace(b"GetInitialData", b"UpdateMods"), True)
            with self.assertRaises(ga.AccountError):
                await lab.connect(target, "ea", session("test-player"), False)
            read.assert_not_awaited()
            await lab.connect(target, "ea", session("test-player"), True)
            self.assertEqual(read.await_args.args[1], "test-player")
            with lab._db() as conn:
                secret = conn.execute("SELECT secret FROM game_lab_accounts WHERE id=?", (target,)).fetchone()[0]
                self.assertNotIn(b"private-session-token", secret)
            self.assertNotIn("secret", lab.current(target))
            with self.assertRaisesRegex(ga.AccountError, "минуту"):
                await lab.refresh(target)
            with self.assertRaises(ga.AccountError):
                await lab.refresh("not-enrolled")
        with self.assertRaises(ga.AccountError):
            lab.remove("notest")
        lab.remove(target)
        self.assertEqual(len(lab.accounts()), 1)

    def test_add_and_connect_forms_with_csrf_and_missing_session(self):
        with TestClient(self.app) as client:
            csrf = self.csrf(client)
            comlink = Mock()
            comlink.get_player.return_value = {"allyCode": "123456789", "playerId": "another", "name": "Other test", "level": 85, "guildName": "AC", "guildId": "ac"}
            with patch("web.routes.registration._get_comlink", return_value=comlink):
                result = client.post("/admin/game-lab/add", data={"csrf": csrf, "ally_code": "123456789", "consent": "yes"}, follow_redirects=False)
            self.assertEqual(result.status_code, 303)
            page = client.get(result.headers["location"])
            self.assertIn("Other test", page.text)
            self.assertIn("Нужна игровая сессия", page.text)
            target = lab.accounts()[1]["id"]
            with patch.object(ga, "read_initial", AsyncMock()) as read:
                result = client.post("/admin/game-lab/connect", data={"csrf": csrf, "target": target, "provider": "ea", "consent": "yes"})
                self.assertIn("Выберите файл игровой сессии", result.text)
                read.assert_not_awaited()
            with patch.object(ga, "read_initial", AsyncMock(return_value={"read_at": 1, "inventory_bytes": 5, "inventory_records": 1})) as read:
                result = client.post("/admin/game-lab/connect", data={"csrf": csrf, "target": target, "provider": "ea", "consent": "yes"}, files={"session_secret": ("test-session.pb", session("another"), "application/octet-stream")})
                self.assertEqual(result.status_code, 200)
                self.assertIn("Сессия проверена и подключена", result.text)
                self.assertNotIn("private-session-token", result.text)
                self.assertNotIn("secret", lab.current(target))
                self.assertEqual(read.await_count, 1)


if __name__ == "__main__":
    unittest.main()
