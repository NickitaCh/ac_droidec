import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from services import game_account as ga, telegram_web_login as login
from tg_bot import game_account as tg, more, store
from tg_bot.core import COMMANDS, CALLBACKS, menu_commands, get_session, new_session
from tg_bot.api import TelegramError


def ctx(private=True, args=""):
    chat = {"id": 123 if private else -10, "type": "private" if private else "supergroup"}
    return SimpleNamespace(owner_id="tg:123", user={"id": 123}, is_private=private,
        chat=chat, chat_id=chat["id"], thread_id=None, message={"chat": chat, "message_id": 50},
        args=args, access={"guild_id": 1, "tier": "member"},
        app=SimpleNamespace(profile="test", comlink=object(), site_url="https://swgoh-sng.ru", username="test_bot"),
        reply=AsyncMock(), reply_photo=AsyncMock(), action=AsyncMock(),
        callback_query={"id": "q"}, api=SimpleNamespace(answer_callback=AsyncMock(), send_message=AsyncMock()))


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

    async def test_group_shards_uses_own_account_and_posts_report_in_group(self):
        c = ctx(False, "Лорд Вейдер осколки")
        with patch.object(more.search, "guides", return_value=[{"base_id": "VADER"}]), \
                patch.object(tg, "_scope", return_value=(c.owner_id, 1, "test", "member")), \
                patch.object(ga, "select_account", return_value="881789523"), \
                patch.object(tg, "run", new_callable=AsyncMock) as run:
            await more.cmd_ready(c)
        run.assert_awaited_once_with(c, {"mode": "ready", "target": "VADER"}, "881789523")

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

    def test_ga_is_canonical_with_group_access_and_arena_alias(self):
        self.assertIs(COMMANDS['ga'], COMMANDS['arena'])
        self.assertIs(COMMANDS['ga'], COMMANDS['ва'])
        self.assertIn('ga', dict(menu_commands()))
        self.assertNotIn('arena', dict(menu_commands()))
        for name in ('ea', 'ga', 'conquest', 'farm'):
            self.assertEqual(COMMANDS[name].where, 'any')
        for prefix, name in (('eag','ga'),('eac','conquest'),('eaf','farm'),('eas','ready')):
            self.assertEqual(CALLBACKS[prefix].command_name, name)

    async def test_group_connection_link_is_sent_only_to_owner_dm(self):
        for blocked in (False, True):
            c = ctx(False)
            if blocked:
                c.api.send_message.side_effect = TelegramError('sendMessage', 'Forbidden', 403)
            with patch.object(tg, '_scope', return_value=(c.owner_id, 1, 'test', 'member')), \
                    patch.object(ga, 'link_path', return_value='/game-account/link?alt=0'), \
                    patch.object(login, 'create_url', return_value='https://swgoh-sng.ru/login/telegram?token=AUTH_SECRET'):
                await tg._link(c, '881789523')
            self.assertEqual(c.api.send_message.await_args.args[0], c.user['id'])
            self.assertIn('AUTH_SECRET', str(c.api.send_message.await_args))
            self.assertNotIn('AUTH_SECRET', str(c.reply.await_args_list))
            self.assertIn('https://t.me/test_bot', str(c.reply.await_args_list))

    async def test_group_expired_session_confirms_privately_then_returns_to_original_topic(self):
        group = ctx(False)
        group.thread_id = 42
        group.message.update(message_thread_id=42, is_topic_message=True)
        with patch.object(tg, '_scope', return_value=(group.owner_id, 1, 'test', 'member')), \
                patch.object(ga, 'select_account', return_value='881789523'), \
                patch.object(tg.reader, 'read', side_effect=ga.SessionExpired()), \
                patch.object(ga, 'reconnect', new_callable=AsyncMock) as reconnect:
            await tg.run(group, {'mode':'arena'}, '881789523')
            reconnect.assert_not_awaited()
        sent = group.api.send_message.await_args
        payload = sent.kwargs['keyboard'][0][0]['callback_data'].split('|', 1)[1]
        self.assertEqual(sent.args[0], group.user['id'])
        self.assertNotIn('|login', str(group.reply.await_args_list))
        private = ctx()
        with patch.object(tg, 'Ctx', return_value=group) as context, \
                patch.object(tg.command_access, 'denied', return_value=False), \
                patch.object(tg, '_scope', return_value=(group.owner_id, 1, 'test', 'member')), \
                patch.object(tg, 'run', new_callable=AsyncMock) as run:
            await tg.refresh(private, payload)
        self.assertEqual(context.call_args.args[1]['message_thread_id'], 42)
        run.assert_awaited_once_with(group, {'mode':'arena'}, '881789523', reconnect=True)

    async def test_group_cannot_trigger_login_or_use_forwarded_private_button(self):
        group = ctx(False)
        token = tg._session(group, spec={'mode':'arena'}, code='881789523')
        with patch.object(tg.command_access, 'denied', return_value=False), \
                patch.object(tg, '_scope', return_value=(group.owner_id, 1, 'test', 'member')), \
                patch.object(tg, 'run', new_callable=AsyncMock) as run:
            await tg.refresh(group, token+'|login')
            private_token = tg._session(ctx(), spec={'mode':'arena'}, code='881789523')
            await tg.refresh(group, private_token+'|read')
        run.assert_not_awaited()
        self.assertNotIn('login_used', get_session(token))
        group.api.send_message.assert_awaited_once()

    async def test_forwarded_group_buttons_cannot_redirect_report_to_other_chat_or_topic(self):
        group = ctx(False)
        group.thread_id = 42
        group.message.update(message_thread_id=42, is_topic_message=True)
        token = tg._session(group, spec={'mode':'arena'}, code='881789523')
        with patch.object(tg, 'run', new_callable=AsyncMock) as run:
            for chat_id, topic in ((-20,42),(-10,43)):
                forwarded = ctx(False)
                forwarded.chat_id, forwarded.thread_id = chat_id, topic
                await tg.refresh(forwarded, token+'|read')
        run.assert_not_awaited()

    async def test_topic_rules_still_apply_after_private_confirmation(self):
        group, private = ctx(False), ctx()
        token = tg._session(group, spec={'mode':'arena'}, code='881789523')
        with patch.object(tg, 'Ctx', return_value=group), \
                patch.object(tg.command_access, 'denied', return_value=True), \
                patch.object(tg, 'run', new_callable=AsyncMock) as run:
            await tg.refresh(private, token+'|login')
        run.assert_not_awaited()

    async def test_group_does_not_echo_credentials_in_backend_error(self):
        group = ctx(False)
        secret = 'email=private@example.com password=PASSWORD_SECRET token=TOKEN_SECRET'
        with patch.object(tg, '_scope', return_value=(group.owner_id, 1, 'test', 'member')), \
                patch.object(ga, 'select_account', return_value='881789523'), \
                patch.object(tg.reader, 'read', side_effect=ga.AccountError(secret)):
            await tg.run(group, {'mode':'arena'}, '881789523')
        for value in ('private@example.com', 'PASSWORD_SECRET', 'TOKEN_SECRET'):
            self.assertNotIn(value, str(group.reply.await_args_list))

    async def test_group_report_only_renders_gameplay_fields(self):
        group = ctx(False)
        state = dict(difficulty='Сложный', energy={'Сейчас':100}, equipped_disks=2, available_disks=3,
                     sectors=[], stamina=[], email='private@example.com', password='PASSWORD_SECRET', token='TOKEN_SECRET')
        await tg._personal(group, 'conquest', state, '881789523', 'safe-session')
        text = str(group.reply.await_args_list)
        self.assertIn('Энергия: 100', text)
        for value in ('private@example.com', 'PASSWORD_SECRET', 'TOKEN_SECRET'):
            self.assertNotIn(value, text)

    def test_arena_topic_rules_survive_ga_rename(self):
        settings = {'default':True, 'rules':{}, 'blocks':{(42,'arena')}}
        self.assertFalse(tg.command_access.general_allowed(settings, 42, 'ga'))
        settings['rules'][42,'officer','arena'] = True
        self.assertTrue(tg.command_access.officer_extra(settings, 42, 'ga'))
        settings['rules'][42,'all','ga'] = True
        self.assertTrue(tg.command_access.general_allowed(settings, 42, 'ga'))


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
