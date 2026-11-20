import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from services import game_account as ga, telegram_web_login as login
from tg_bot import game_account as tg, more, store
from tg_bot.core import get_session, new_session


def ctx(private=True, args=""):
    return SimpleNamespace(owner_id="tg:123", user={"id": 123}, is_private=private,
        args=args, access={"guild_id": 1, "tier": "member"},
        app=SimpleNamespace(profile="test", comlink=object(), site_url="https://swgoh-sng.ru"),
        reply=AsyncMock(), reply_photo=AsyncMock(), action=AsyncMock(),
        callback_query={"id": "q"}, api=SimpleNamespace(answer_callback=AsyncMock()))


class Reports(unittest.IsolatedAsyncioTestCase):
    async def test_shards_never_read_another_players_inventory(self):
        c = ctx(args="Лорд Вейдер осколки | 999999999")
        with patch.object(more.search, "guides", return_value=[{"base_id": "VADER"}]), \
                patch.object(tg, "_scope", return_value=(c.owner_id, 1, "test", "member")), \
                patch.object(ga, "select_account", side_effect=ga.AccountError("Чужой аккаунт")), \
                patch.object(tg.reader, "read", new_callable=AsyncMock) as read:
            await more.cmd_ready(c)
        read.assert_not_awaited()
        self.assertIn("Чужой аккаунт", c.reply.await_args.args[0])

    async def test_group_shards_only_offers_private_command(self):
        c = ctx(False, "Лорд Вейдер осколки")
        with patch.object(more.search, "guides", return_value=[{"base_id": "VADER"}]), \
                patch.object(tg.reader, "read", new_callable=AsyncMock) as read:
            await more.cmd_ready(c)
        read.assert_not_awaited()
        self.assertIn("личке", c.reply.await_args.args[0])

    async def test_expired_session_requires_explicit_single_use_login(self):
        c = ctx()
        with patch.object(tg, "_scope", return_value=(c.owner_id, 1, "test", "member")), \
                patch.object(ga, "select_account", return_value="881789523"), \
                patch.object(tg.reader, "read", side_effect=ga.SessionExpired()), \
                patch.object(ga, "reconnect", new_callable=AsyncMock) as reconnect:
            await tg.run(c, {"mode": "arena"}, "881789523")
            reconnect.assert_not_awaited()
            callback = c.reply.await_args.args[1][0][0]["callback_data"].split("|", 1)[1]
            await tg.refresh(c, callback)
            await tg.refresh(c, callback)
            reconnect.assert_awaited_once()

    async def test_other_user_cannot_refresh(self):
        c = ctx()
        token = new_session({"user_id": 456, "spec": {"mode": "arena"}, "code": "999999999"})
        with patch.object(tg, "run", new_callable=AsyncMock) as run:
            await tg.refresh(c, token + "|read")
        run.assert_not_awaited()

    async def test_farm_parses_guide_reward_and_alt(self):
        c = ctx(args="руководство Лорд Вейдер награда | альт")
        with patch.object(tg.search, "guides", return_value=[{"base_id": "VADER", "name": "Вейдер"}]), \
                patch.object(tg, "start", new_callable=AsyncMock) as start:
            await tg.cmd_farm(c)
        spec, account = start.await_args.args[1:]
        self.assertTrue(spec["guide"])
        self.assertTrue(spec["reward"])
        self.assertEqual(account.strip(), "альт")


class WebsiteLogin(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        p = patch.object(login.database, "DB_NAME", str(Path(self.tmp.name) / "test.db"))
        p.start()
        self.addCleanup(p.stop)
        self.owner = store.touch_user({"id": 123, "first_name": "Nod"}, True, "test")

    def create(self):
        with patch.object(login.guild_resolver, "resolve_access", return_value={"guild_id": 1, "tier": "member"}), \
                patch.object(ga, "require_account_read"):
            url = login.create_url(123, self.owner, "test", "/game-account/link?alt=0", "https://swgoh-sng.ru")
        return url.split("token=")[1]

    def test_preview_does_not_consume_but_login_does(self):
        token = self.create()
        self.assertEqual(login.lookup(token)[0], self.owner)
        self.assertEqual(login.lookup(token, consume=True)[0], self.owner)
        self.assertIsNone(login.lookup(token, consume=True))

    def test_identity_change_invalidates_link(self):
        token = self.create()
        conn = store._connect()
        conn.execute("UPDATE tg_users SET owner_id='changed' WHERE tg_user_id=123")
        conn.commit()
        conn.close()
        self.assertIsNone(login.lookup(token, consume=True))

    def test_old_link_invalidated_on_reissue_and_expiry(self):
        old = self.create()
        new = self.create()
        self.assertIsNone(login.lookup(old))
        with patch.object(login.time, "time", return_value=10**12):
            self.assertIsNone(login.lookup(new))

    def test_website_login_requires_confirmation_csrf_and_is_single_use(self):
        import re
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from starlette.middleware.sessions import SessionMiddleware
        from web import auth
        app = FastAPI()
        app.add_middleware(SessionMiddleware, secret_key='test-only')
        app.include_router(auth.router)
        token = self.create()
        with TestClient(app, base_url='https://swgoh-sng.ru') as client:
            page = client.get('/login/telegram', params={'token': token})
            self.assertEqual(page.status_code, 200)
            self.assertNotIn('mc.yandex.ru/metrika', page.text)
            self.assertEqual(page.headers['referrer-policy'], 'no-referrer')
            csrf = re.search('name="csrf" value="([^"]+)"', page.text).group(1)
            self.assertIsNotNone(login.lookup(token))
            self.assertEqual(client.post('/login/telegram', data={'token': token, 'csrf': 'bad'}).status_code, 403)
            page = client.get('/login/telegram', params={'token': token})
            csrf = re.search('name="csrf" value="([^"]+)"', page.text).group(1)
            with patch.object(login.guild_resolver, 'resolve_access', return_value={
                    'guild_id': 1, 'tier': 'member', 'is_super_admin': False}), patch.object(ga, 'require_account_read'):
                response = client.post('/login/telegram', data={'token': token, 'csrf': csrf}, follow_redirects=False)
            self.assertEqual(response.status_code, 303)
            self.assertEqual(response.headers['location'], '/game-account/link?alt=0')
            self.assertIsNone(login.lookup(token))
