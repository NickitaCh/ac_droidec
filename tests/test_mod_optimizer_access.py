import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from cryptography.fernet import Fernet
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.middleware.sessions import SessionMiddleware

from services import game_account as ga, game_account_readonly as accounts, game_mod_actions as actions
from web.deps import get_current_user
from web.routes import game_account as account_route


class MemberAccountAccessTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.access = {'guild_id': 1, 'tier': 'member'}
        self.user = {'discord_id': 'owner', 'guild_id': 1, 'bot_profile': 'test',
                     'tier': 'member', 'is_super_admin': False, 'username': 'Member'}
        self.patches = [patch.object(ga.database, 'DB_NAME', str(Path(self.temp.name)/'account.db')),
                        patch.object(ga.guild_resolver, 'resolve_access', side_effect=lambda _: dict(self.access)),
                        patch('services.bot_profiles.guild_profile', return_value='test')]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()
        self.temp.cleanup()

    def test_member_read_does_not_enable_officer_operations_or_foreign_guilds(self):
        ga.require_account_read('owner', 1, 'test', 'member')
        with self.assertRaises(ga.AccountError):
            ga.require_pilot('owner', 1, 'test', 'member')
        for gid, profile in ((2, 'test'), (1, 'prod')):
            with self.assertRaises(ga.AccountError):
                ga.require_account_read('owner', gid, profile, 'member')
        self.access['guild_id'] = 2
        ga.require_account_read('owner', 2, 'test', 'member')
        with patch('services.bot_profiles.guild_profile', return_value='prod'):
            with self.assertRaises(ga.AccountError):
                ga.require_account_read('owner', 2, 'test', 'member')
        with self.assertRaises(ga.AccountError):
            ga.require_account_read('owner', 1, 'test', 'member')

    def test_member_may_open_personal_pages_but_not_officer_actions(self):
        self.access['guild_id'] = self.user['guild_id'] = 2
        app = FastAPI()
        app.add_middleware(SessionMiddleware, secret_key='local-test')
        app.include_router(account_route.router, prefix='/game-account')
        with TestClient(app) as client, patch.object(account_route.ea_connect, 'available', return_value=True), \
                patch.object(ga.database, 'get_user_registrations', return_value=[('123', 'Mine', 1)]), \
                patch.object(accounts, '_connection', return_value=None), \
                patch('web.deps.get_current_user', return_value=self.user):
            linked = client.get('/game-account/link')
            self.assertEqual(linked.status_code, 200)
            self.assertIn('ea_email', linked.text)
            profile = client.get('/game-account', follow_redirects=False)
            self.assertEqual(profile.status_code, 303)
            self.assertEqual(profile.headers['location'], '/player/123#ea-connection')
            for path in ('/modules', '/personal/arena', '/personal/conquest'):
                self.assertEqual(client.get('/game-account' + path).status_code, 200)
                self.assertEqual(client.get('/game-account' + path + '?ally_code=foreign').status_code, 403)
            self.assertEqual(client.post('/game-account/read', data={'csrf': 'x'}).status_code, 403)
            self.assertEqual(client.post('/game-account/disconnect', data={'csrf': 'x'}).status_code, 403)

    async def test_saved_session_is_readable_only_through_explicit_optimizer_read(self):
        cipher = Fernet(Fernet.generate_key())
        credentials = {'expected_id': 'player', 'read_request': 'read'}
        connection = ('target', 'player', cipher.encrypt(json.dumps(credentials).encode()))
        with patch.object(ga, '_cipher', return_value=cipher), \
                patch.object(accounts, '_connection', return_value=connection), \
                patch.object(ga.database, 'get_user_registrations', return_value=[('123', 'Mine', 1)]):
            result = accounts._load('owner', 1, 'test', 'member', '123', optimizer_read=True)
            self.assertEqual(result[1]['expected_id'], 'player')
            with self.assertRaises(ga.AccountError):
                accounts._load('owner', 1, 'test', 'member', 'someone-else', optimizer_read=True)
            with self.assertRaises(ga.AccountError):
                accounts._load('owner', 1, 'test', 'member', '123')
            with patch.object(ga, '_read_payload', AsyncMock()) as transport:
                with self.assertRaises(ga.AccountError):
                    await actions.snapshot(self.user, 'someone-else')
                transport.assert_not_awaited()


if __name__ == '__main__':
    unittest.main()
