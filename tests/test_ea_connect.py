import base64
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

import httpx
from cryptography.fernet import Fernet

import database
from services import ea_browser as browser, ea_connect as ea, game_account as ga, game_lab as lab
from web import audit


def request(service="PlayerRpc", method="GetInitialData", player="test-player"):
    return ga.pack([(4, 2, service.encode()), (5, 2, method.encode()),
                    (6, 2, ga.pack([(1, 2, b"device")])), (7, 2, player.encode()), (8, 2, b"old-session")])


def token_result(nucleus="ea-player", refresh=b"rotated"):
    claims = base64.urlsafe_b64encode(json.dumps({"nexus": {"pid": nucleus}}).encode()).rstrip(b"=")
    return {1: b"e30." + claims + b".signature", 3: 3600, 4: refresh}


class EaTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_patch = patch.object(database, "DB_NAME", str(Path(self.tmp.name) / "test.db"))
        self.db_patch.start()
        key = Path(self.tmp.name) / "cache" / "game-account.key"
        key.parent.mkdir()
        key.write_bytes(Fernet.generate_key())
        self.cfg = {"client_id": "SWGOH_GP_CLIENT", "client_secret": "client-secret", "signing_key": base64.b64encode(b"signing-key").decode(),
                    "auth_params": {"client_id": "SWGOH_GP_CLIENT", "redirect_uri": "ealogin://SWGOH_GP_CLIENT", "response_type": "code lnglv_token"},
                    "sig_payload": {"ts": "old"}, "guest_template": base64.b64encode(request("AuthRpc", "DoAuthGuest")).decode(),
                    "read_template": base64.b64encode(request()).decode()}
        self.cfg_patch = patch.object(ea, "config", return_value=self.cfg)
        self.cfg_patch.start()
        with lab._db() as conn:
            conn.execute("INSERT INTO game_lab_accounts (id,ally_code,player_id,name,level,game_guild_id,game_guild_name,added_by,added_at) VALUES ('case','123456789','test-player','Test',85,'guild','Guild','admin',0)")
        self.credentials = {"expected_id": "test-player", "access": "access", "refresh": "refresh", "longlive": "longlive",
                            "nucleus": "ea-player", "expires_at": time.time() + 3600, "device": "device",
                            "guest_request": self.cfg["guest_template"], "read_request": base64.b64encode(request()).decode()}
        self.summary = {"read_at": 1, "inventory_bytes": 10, "inventory_records": 1}

    def tearDown(self):
        self.cfg_patch.stop()
        self.db_patch.stop()
        self.tmp.cleanup()

    async def test_email_code_flow_chooses_code_form_not_resend(self):
        calls = []
        def handler(req):
            calls.append(req)
            if req.method == "GET":
                return httpx.Response(200, text='<form method="post"><input name="email"><input type="hidden" name="_eventId" value="submit"></form>')
            if b"email=" in req.content:
                return httpx.Response(200, text='<form><input type="hidden" name="_eventId" value="resend"></form><form><input name="oneTimeCode"><input type="hidden" name="_eventId" value="submit"></form>', headers={"set-cookie": "ea-cookie=private-cookie; Path=/; Secure"})
            self.assertIn(b"oneTimeCode=123456", req.content)
            return httpx.Response(302, headers={"location": "ealogin://SWGOH_GP_CLIENT?lnglv_token=long-lived-access"})
        original = httpx.AsyncClient
        with patch.object(browser.httpx, "AsyncClient", side_effect=lambda **kw: original(transport=httpx.MockTransport(handler), **kw)):
            state = await browser.start(self.cfg, "test@example.com")
            self.assertEqual(state["stage"], "code")
            result = await browser.finish(state, "123456")
        self.assertEqual(result, {"stage": "complete", "longlive": "long-lived-access"})
        self.assertNotIn(b"password", calls[1].content)
        self.assertEqual(audit._clean_value("one_time_code", "123456"), "***")
        self.assertEqual(audit._clean_value("ea_email", "test@example.com"), "***")

    async def test_unexpected_redirect_and_challenge_are_not_followed(self):
        for location in ["https://evil.example/collect", "http://signin.ea.com/login", "https://signin.ea.com@evil.example/"]:
            original = httpx.AsyncClient
            with patch.object(browser.httpx, "AsyncClient", side_effect=lambda **kw: original(transport=httpx.MockTransport(lambda req: httpx.Response(302, headers={"location": location})), **kw)):
                with self.assertRaises(ga.AccountError):
                    await browser.start(self.cfg, "test@example.com")

    async def test_pending_cookie_encrypted_bound_to_actor_and_rate_limited(self):
        state = {"stage": "code", "url": "https://signin.ea.com/p/juno/nff/login", "fields": {}, "cookies": [{"name": "ea", "value": "private-cookie", "domain": "signin.ea.com", "path": "/"}]}
        with patch.object(browser, "start", AsyncMock(return_value=state)):
            flow = await ea.start("admin", "case", "test@example.com", True)
            with self.assertRaises(ga.AccountError):
                await ea.start("admin", "case", "test@example.com", True)
        with ea._db() as conn:
            secret = conn.execute("SELECT secret FROM ea_browser_pending").fetchone()[0]
            self.assertNotIn(b"private-cookie", secret)
        self.assertIsNone(ea.pending("other-admin", "case"))
        with patch.object(browser, "finish", AsyncMock()) as finish:
            with self.assertRaises(ga.AccountError):
                await ea.finish("other-admin", "case", flow, "123456")
            finish.assert_not_awaited()

    async def test_complete_flow_persists_only_verified_target_and_clears_pending(self):
        with patch.object(browser, "start", AsyncMock(return_value={"stage": "code", "cookies": [], "url": "https://signin.ea.com/", "fields": {}})):
            flow = await ea.start("admin", "case", "test@example.com", True)
        with patch.object(browser, "finish", AsyncMock(return_value={"stage": "complete", "longlive": "access"})), patch.object(ea, "_exchange", AsyncMock(return_value={})), patch.object(ea, "_import", AsyncMock(return_value=(self.credentials, self.summary))) as restore:
            await ea.finish("admin", "case", flow, "123456")
            self.assertEqual(restore.await_args.args[2], "test-player")
        self.assertTrue(ea.linked("case"))
        self.assertIsNone(ea.pending("admin", "case"))
        with ea._db() as conn:
            self.assertNotIn(b"longlive", conn.execute("SELECT secret FROM ea_account_links").fetchone()[0])

    async def test_wrong_player_preview_stops_before_import(self):
        auth = ga.pack([(1, 2, b"isolated-guest"), (2, 2, b"guest-token")])
        preview = ga.pack([(5, 2, b"main-player"), (6, 2, b"ea-player")])
        with patch.object(ea, "_game", AsyncMock(side_effect=[auth, preview])) as game, patch.object(ga, "read_initial", AsyncMock()) as read:
            with self.assertRaises(ga.AccountError):
                await ea._import(self.cfg, self.credentials, "test-player")
            self.assertEqual([call.args[1] for call in game.await_args_list], ["DoAuthGuest", "GetNucleusPlayerPreview"])
            read.assert_not_awaited()

    async def test_expired_game_session_restored_once_and_tokens_rotated(self):
        ea._save("case", self.credentials, self.summary)
        auth = ga.pack([(1, 2, b"test-player"), (2, 2, b"fresh-session")])
        with patch.object(ga, "read_initial", AsyncMock(side_effect=[ga.SessionExpired("expired"), self.summary])) as read, patch.object(ea, "_game", AsyncMock(return_value=auth)) as game, patch.object(ea, "_renew", AsyncMock(return_value=self.credentials)) as renew:
            result = await ea.read("case")
            self.assertEqual(result, self.summary)
            self.assertEqual(read.await_count, 2)
            self.assertEqual(game.await_count, 1)
            self.assertEqual(renew.await_count, 1)
            self.assertEqual(ga._one(ga.fields(read.await_args.args[0]), 8, 2), b"fresh-session")
        with patch.object(ea, "_grpc", AsyncMock(return_value=token_result())):
            renewed = await ea._renew(self.cfg, {**self.credentials, "expires_at": 0})
            self.assertEqual(renewed["refresh"], "rotated")

    async def test_wrong_restored_identity_or_network_error_never_retried(self):
        ea._save("case", self.credentials, self.summary)
        auth = ga.pack([(1, 2, b"main-player"), (2, 2, b"fresh-session")])
        with patch.object(ga, "read_initial", AsyncMock(side_effect=ga.SessionExpired("expired"))) as read, patch.object(ea, "_game", AsyncMock(return_value=auth)), patch.object(ea, "_renew", AsyncMock(return_value=self.credentials)):
            with self.assertRaises(ga.AccountError):
                await ea.read("case")
            self.assertEqual(read.await_count, 1)
        with lab._db() as conn:
            conn.execute("UPDATE game_lab_accounts SET last_attempt=0 WHERE id='case'")
        with patch.object(ga, "read_initial", AsyncMock(side_effect=ga.AccountError("network"))), patch.object(ea, "_game", AsyncMock()) as game:
            with self.assertRaises(ga.AccountError):
                await ea.read("case")
            game.assert_not_awaited()

    async def test_disconnect_during_read_cannot_resurrect_credentials(self):
        ea._save("case", self.credentials, self.summary)
        async def read(*args):
            ea.disconnect("case")
            return self.summary
        with patch.object(ga, "read_initial", side_effect=read):
            with self.assertRaises(ea.ReconnectRequired):
                await ea.read("case")
        self.assertFalse(ea.linked("case"))

    async def test_rotated_token_saved_even_if_game_is_temporarily_unavailable(self):
        ea._save("case", {**self.credentials, "expires_at": 0}, self.summary)
        with patch.object(ea, "_grpc", AsyncMock(return_value=token_result())), patch.object(ga, "read_initial", AsyncMock(side_effect=ga.AccountError("network"))):
            with self.assertRaises(ga.AccountError):
                await ea.read("case")
        with ea._db() as conn:
            stored = conn.execute("SELECT secret FROM ea_account_links WHERE target='case'").fetchone()[0]
        self.assertEqual(json.loads(ga._cipher().decrypt(stored))["refresh"], "rotated")
        self.assertEqual(lab.current("case")["state"], "error")

    async def test_expired_pending_code_and_wrong_code_cannot_bypass_limits(self):
        state = {"stage": "code", "url": "https://signin.ea.com/", "fields": {}, "cookies": []}
        with patch.object(browser, "start", AsyncMock(return_value=state)):
            flow = await ea.start("admin", "case", "test@example.com", True)
        with patch.object(browser, "finish", AsyncMock(return_value=state)), patch.object(ea, "_import", AsyncMock()) as restore:
            for _ in range(5):
                with self.assertRaises(ga.AccountError):
                    await ea.finish("admin", "case", flow, "123456")
            with self.assertRaises(ga.AccountError):
                await ea.finish("admin", "case", flow, "123456")
            restore.assert_not_awaited()
        with ea._db() as conn:
            conn.execute("UPDATE ea_browser_pending SET expires=0")
        with patch.object(browser, "finish", AsyncMock()) as submit:
            with self.assertRaises(ga.AccountError):
                await ea.finish("admin", "case", flow, "123456")
            submit.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
