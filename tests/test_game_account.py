import tempfile
import unittest
import gzip
import sqlite3
import json
from pathlib import Path
from unittest.mock import AsyncMock, patch
from types import SimpleNamespace
import httpx

from cryptography.fernet import Fernet
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.middleware.sessions import SessionMiddleware

import database
from services import bot_profiles, game_account as ga, test_accounts
from web.routes import game_account as route
from web.deps import require_officer_access


def request_bytes(player="player"):
    return ga.pack([(4, 2, b"PlayerRpc"), (5, 2, b"GetInitialData"), (6, 2, b""),
                    (7, 2, player.encode()), (8, 2, b"private-token"), (11, 0, 1), (20, 0, 1)])


class PilotTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.patches = [
            patch.object(database, "DB_NAME", str(Path(self.tmp.name) / "test.db")),
            patch("guild_resolver.resolve_access", return_value={"guild_id": 1, "tier": "officer"}),
            patch.object(bot_profiles, "guild_profile", return_value="test"),
            patch.object(database, "get_user_registrations", return_value=[("123456789", "Player", 1)]),
            patch.object(database, "get_roster_by_player_id", return_value={"player": ("123456789", "Player")}),
        ]
        for p in self.patches:
            p.start()
        key = Path(self.tmp.name) / "cache" / "game-account.key"
        key.parent.mkdir()
        key.write_bytes(Fernet.generate_key())

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()
        self.tmp.cleanup()

    async def provision(self):
        with patch.object(ga, "read_initial", AsyncMock(return_value={"inventory_bytes": 4, "inventory_records": 1, "read_at": 1})):
            await ga.provision("owner", 1, "test", "123456789", "ea", request_bytes(), consent=True)

    def test_gate_rejects_prod_other_guild_member_and_impersonation(self):
        for guild, profile, tier in [(1, "prod", "officer"), (2, "test", "officer"), (1, "test", "member")]:
            with self.assertRaises(ga.AccountError):
                ga.require_pilot("owner", guild, profile, tier)
        with patch("guild_resolver.resolve_access", return_value={"guild_id": 2, "tier": "officer"}):
            with self.assertRaises(ga.AccountError):
                ga.require_pilot("owner", 1, "test", "officer")

    def test_allowlist_and_identity_checked_before_network(self):
        for method in [b"UpdateMods", b"DoAuthGuest", b"LinkNucleusAccount"]:
            raw = request_bytes().replace(b"GetInitialData", method)
            with self.assertRaises(ga.AccountError):
                ga.read_request(raw, "player")
        with self.assertRaises(ga.AccountError):
            ga.read_request(request_bytes(), "other")
        with self.assertRaises(ga.AccountError):
            ga.fields(b"\x12\x10x")

    async def test_encrypted_owner_scoped_storage_and_cooldown(self):
        await self.provision()
        self.assertIsNone(ga.status("other-owner", 1, "test"))
        with ga._db() as conn:
            row = conn.execute("SELECT secret FROM game_account_sessions").fetchone()
            self.assertNotIn(b"private-token", row["secret"])
        with patch.object(ga, "read_initial", AsyncMock()) as read:
            with self.assertRaisesRegex(ga.AccountError, "минуту"):
                await ga.refresh("owner", 1, "test")
            read.assert_not_awaited()
        ga.disconnect("owner", 1, "test")
        self.assertIsNone(ga.status("owner", 1, "test"))

    async def test_registration_revocation_blocks_reads(self):
        await self.provision()
        with patch.object(database, "get_user_registrations", return_value=[]), patch.object(ga, "read_initial", AsyncMock()) as read:
            with self.assertRaises(ga.AccountError):
                await ga.refresh("owner", 1, "test")
            read.assert_not_awaited()

    async def test_no_retry_or_login_on_expired_session(self):
        await self.provision()
        with ga._db() as conn:
            conn.execute("UPDATE game_account_sessions SET last_attempt=0")
        with patch.object(ga, "read_initial", AsyncMock(side_effect=ga.AccountError("Сессия завершилась"))) as read:
            with self.assertRaises(ga.AccountError):
                await ga.refresh("owner", 1, "test")
            self.assertEqual(read.await_count, 1)
        self.assertEqual(ga.status("owner", 1, "test")["state"], "error")

    async def test_missing_consent_or_foreign_account_never_calls_game(self):
        with patch.object(ga, "read_initial", AsyncMock()) as read:
            for code, consent in [("123456789", False), ("987654321", True)]:
                with self.assertRaises(ga.AccountError):
                    await ga.provision("owner", 1, "test", code, "ea", request_bytes(), consent=consent)
            read.assert_not_awaited()

    async def test_main_alt_sessions_are_independent(self):
        registrations = [("123456789", "Main", 1), ("986884832", "Alt", 0)]
        with patch.object(database, "get_user_registrations", return_value=registrations), patch.object(database, "get_roster_by_player_id", return_value={"player": ("123456789", "Main"), "alt-player": ("986884832", "Alt")}), patch.object(ga, "read_initial", AsyncMock(return_value={"inventory_bytes": 4, "inventory_records": 1, "read_at": 1})) as read:
            await ga.provision("owner", 1, "test", "123456789", "ea", request_bytes(), consent=True)
            await ga.provision("owner", 1, "test", "986884832", "google", request_bytes("alt-player"), consent=True)
            self.assertEqual(ga.select_account("owner", 1, alt=True), "986884832")
            self.assertEqual(ga.status("owner", 1, "test")["ally_code"], "123456789")
            self.assertEqual(ga.status("owner", 1, "test", ally_code="986884832")["provider"], "google")
            with ga._db() as conn:
                conn.execute("UPDATE game_account_sessions SET last_attempt=0 WHERE ally_code='986884832'")
            await ga.refresh("owner", 1, "test", ally_code="986884832")
            self.assertEqual(read.await_args.args[1], "alt-player")
            ga.disconnect("owner", 1, "test", ally_code="986884832")
            self.assertIsNone(ga.status("owner", 1, "test", ally_code="986884832"))
            self.assertIsNotNone(ga.status("owner", 1, "test"))
            with self.assertRaises(ga.AccountError):
                ga.status("owner", 1, "test", ally_code="999999999")

    async def test_single_account_migration_preserves_encrypted_session(self):
        # Reproduce the deployed schema and verify an actual row survives.
        with sqlite3.connect(database.DB_NAME) as conn:
            conn.execute("CREATE TABLE game_account_sessions (owner TEXT, guild_id INTEGER, ally_code TEXT, provider TEXT, secret BLOB, consent_at REAL, last_attempt REAL, summary TEXT, state TEXT, PRIMARY KEY(owner,guild_id))")
            conn.execute("INSERT INTO game_account_sessions VALUES ('owner',1,'123456789','ea',?,1,1,?,'connected')", (b'encrypted-session', json.dumps({"read_at": 1})))
        conn.close()
        self.assertEqual(ga.status("owner", 1, "test")["summary"]["read_at"], 1)
        with ga._db() as conn:
            self.assertEqual(conn.execute("SELECT secret FROM game_account_sessions").fetchone()[0], b'encrypted-session')
            pk = [r[1] for r in conn.execute("PRAGMA table_info(game_account_sessions)") if r[5]]
            self.assertEqual(pk, ["owner", "guild_id", "ally_code"])

    def test_outside_roster_requires_explicit_test_grant_and_is_hidden_in_prod(self):
        with patch.object(database, "get_roster_by_player_id", return_value={}):
            with self.assertRaises(ga.AccountError):
                ga._identity("owner", 1, "123456789")
            with test_accounts._db() as conn:
                conn.execute("INSERT INTO test_account_links VALUES (1,'owner','123456789','player','Test')")
            self.assertEqual(ga._identity("owner", 1, "123456789"), "player")
            self.assertTrue(test_accounts.visible("owner", 1, "123456789", "test"))
            self.assertFalse(test_accounts.visible("owner", 1, "123456789", "prod"))
            self.assertIsNone(test_accounts.player_id("other-owner", 1, "123456789"))

    def test_virtual_alt_registration_is_test_only_and_does_not_change_main_or_roster(self):
        self.patches[3].stop()
        try:
            database.set_user_registration("owner", "123456789", "Main", guild_id=1)
            with bot_profiles.delivery_context("test"):
                test_accounts.grant("owner", 1, "986884832", "test-player", "notest")
                self.assertEqual(database.get_user_registration("owner", guild_id=1), ("123456789", "Main"))
                self.assertEqual(database.get_user_registrations("owner", guild_id=1)[1], ("986884832", "notest", 0))
                with patch("guild_resolver.resolve_access", return_value={"guild_id": 1, "ally_code": "123456789", "tier": "officer"}), patch.object(database, "get_all_guild_configs", return_value=[{"id": 1}]):
                    import guild_resolver
                    self.assertEqual(guild_resolver.alt_account("owner")["ally_code"], "986884832")
            with bot_profiles.delivery_context("prod"):
                self.assertEqual(database.get_user_registrations("owner", guild_id=1), [("123456789", "Main", 1)])
                self.assertEqual(len(database.get_all_registrations_for_discord_id("owner")), 1)
            with sqlite3.connect(database.DB_NAME) as conn:
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM user_registration").fetchone()[0], 1)
            conn.close()
            with bot_profiles.delivery_context("test"):
                database.delete_user_registration("owner", "986884832", guild_id=1)
                self.assertEqual(len(database.get_user_registrations("owner", guild_id=1)), 1)
        finally:
            self.patches[3].start()

    async def test_discord_dm_alt_targets_selected_session(self):
        from cogs.game_account import GameAccountCog
        from unittest.mock import Mock
        inter = SimpleNamespace(author=SimpleNamespace(id=123), guild=None,
                                response=SimpleNamespace(defer=AsyncMock(), is_done=lambda: True),
                                edit_original_response=AsyncMock())
        own = [("123456789", "Main", 1), ("986884832", "notest", 0)]
        with patch.object(database, "get_user_registrations", return_value=own), patch.object(ga, "refresh", AsyncMock(return_value={"inventory_bytes": 10, "inventory_records": 2})) as read, patch.object(bot_profiles, "worker_profile", return_value="test"):
            await GameAccountCog.account.callback(GameAccountCog(Mock()), inter, действие="прочитать", альт=True, аликод=None)
            self.assertEqual(read.await_args.kwargs["ally_code"], "986884832")
            self.assertIn("986884832", inter.edit_original_response.await_args.kwargs["content"])

    async def test_rpc_checks_code_and_player_before_exposing_inventory(self):
        original_client = httpx.AsyncClient
        inventory = ga.pack([(1, 2, b"resource")])
        for rpc, player, ok in [(1, b"player", True), (1, b"other", False), (5, b"player", False)]:
            calls = []
            payload = ga.pack([(1, 2, player), (5, 2, inventory)])
            response = ga.pack([(5, 0, rpc), (4, 2, gzip.compress(payload))])

            def handler(request):
                calls.append(request)
                self.assertEqual(str(request.url), "https://sw-live.cgswprod.com/rpc")
                self.assertEqual(request.headers["X-Rpc-Method-Name"], "GetInitialData")
                return httpx.Response(200, stream=httpx.ByteStream(response))

            with patch.object(ga.httpx, "AsyncClient", side_effect=lambda **kw: original_client(transport=httpx.MockTransport(handler), **kw)):
                if ok:
                    result = await ga.read_initial(request_bytes(), "player")
                    self.assertEqual(result["inventory_records"], 1)
                else:
                    with self.assertRaises(ga.AccountError):
                        await ga.read_initial(request_bytes(), "player")
            self.assertEqual(len(calls), 1)

    def test_web_csrf_and_provider_readiness(self):
        app = FastAPI()
        app.add_middleware(SessionMiddleware, secret_key="test")
        app.include_router(route.router, prefix="/game-account")
        app.dependency_overrides[require_officer_access] = lambda: {
            "discord_id": "owner", "guild_id": 1, "bot_profile": "test", "tier": "officer", "username": "Officer"}
        with TestClient(app) as client:
            response = client.get("/game-account")
            self.assertEqual(response.status_code, 200)
            self.assertIn("вход и обновление токена", response.text)
            self.assertIn("через сайт и продление", response.text)
            self.assertEqual(response.headers["cache-control"], "no-store")
            self.assertEqual(client.post("/game-account/disconnect", data={"csrf": "wrong"}).status_code, 403)
        self.assertFalse(bot_profiles.path_available("/game-account", "prod"))


if __name__ == "__main__":
    unittest.main()
