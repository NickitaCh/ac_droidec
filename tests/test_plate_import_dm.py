import copy
import json
import re
import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch
from services import hotbot_import as hb
import database


class Reply:
    status = 200

    def __init__(self, data):
        self.data = data

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    async def json(self):
        return copy.deepcopy(self.data)

    def raise_for_status(self):
        pass


class DMImportTests(unittest.IsolatedAsyncioTestCase):
    def session(self, recipients, channel_type=1):
        self.calls = []
        test = self

        class Session:
            def __init__(self, **kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                pass

            def get(self, url, **kwargs):
                path = url.removeprefix(hb.DISCORD_API)
                test.calls.append(path)
                return Reply({
                    '/channels/22': {'type': channel_type, 'recipients': [{'id': str(owner)} for owner in recipients]},
                    '/users/@me': {'id': 'bot'},
                    '/channels/22/messages/33': {'id': '33', 'author': {'id': 'bot'}, 'components': []},
                    '/channels/22/messages': [],
                }[path])
        return Session

    async def test_own_dm_and_disguised_foreign_dm(self):
        with patch.object(hb.aiohttp, 'ClientSession', self.session(['owner'])):
            messages = await hb.fetch_messages('https://discord.com/channels/@me/22/33', token='fake', owner='owner')
            self.assertTrue(messages[0]['_droidec_dm'])
        for link in ('https://discord.com/channels/@me/22/33', 'https://discord.com/channels/11/22/33'):
            with patch.object(hb.aiohttp, 'ClientSession', self.session(['someone-else'])):
                with self.assertRaises(hb.HotbotImportError):
                    await hb.fetch_messages(link, token='fake', owner='owner')
                self.assertEqual(self.calls, ['/channels/22'])

    async def test_group_dm_and_missing_identity_are_rejected(self):
        with patch.object(hb.aiohttp, 'ClientSession', self.session(['owner'], 3)):
            with self.assertRaises(hb.HotbotImportError):
                await hb.fetch_messages('https://discord.com/channels/@me/22/33', token='fake', owner='owner')
            self.assertEqual(self.calls, ['/channels/22'])
        with self.assertRaises(hb.HotbotImportError):
            await hb.fetch_messages('https://discord.com/channels/@me/22/33', token='fake')

    def range_session(self, messages):
        test = self
        self.pages = []

        class Session:
            def __init__(self, **kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                pass

            def get(self, url, **kwargs):
                path = url.removeprefix(hb.DISCORD_API)
                if path == '/channels/22':
                    return Reply({'type': 1, 'recipients': [{'id': 'owner'}]})
                if path == '/users/@me':
                    return Reply({'id': 'bot'})
                if path == '/channels/22/messages':
                    params = kwargs['params']
                    test.pages.append(params)
                    page = sorted([m for m in messages if int(m['id']) < int(params['before'])],
                                  key=lambda m: int(m['id']), reverse=True)[:params['limit']]
                    return Reply(page)
                return Reply(next(m for m in messages if path.endswith('/' + m['id'])))
        return Session

    def forwarded(self, mid, original=None):
        return {'id': str(mid), 'author': {'id': 'owner'},
                'message_reference': {'type': 1, 'channel_id': 'source', 'message_id': str(original or mid)},
                'message_snapshots': [{'message': {'embeds': [{'fields': [
                    {'name': '✅ Раздел...', 'value': f'строка {mid}'}]}]}}]}

    async def test_large_forward_range_paginates_orders_and_excludes_neighbours(self):
        messages = [self.forwarded(mid) for mid in range(1, 208)]
        with patch.object(hb.aiohttp, 'ClientSession', self.range_session(messages)):
            found = await hb.fetch_messages('https://discord.com/channels/@me/22/2', token='fake',
                                           owner='owner', end_link='https://discord.com/channels/@me/22/206')
        self.assertEqual([m['id'] for m in found], [str(i) for i in range(2, 207)])
        self.assertEqual(len(self.pages), 3)
        parsed = hb.parse_messages(found)
        self.assertEqual(len(parsed['sections']), 1)
        self.assertEqual(parsed['sections'][0]['lines'], [f'строка {i}' for i in range(2, 207)])

    async def test_duplicate_forwards_do_not_duplicate_source(self):
        messages = [self.forwarded(1), self.forwarded(2, original=1), self.forwarded(3)]
        with patch.object(hb.aiohttp, 'ClientSession', self.range_session(messages)):
            found = await hb.fetch_messages('https://discord.com/channels/@me/22/1', token='fake',
                                           owner='owner', end_link='https://discord.com/channels/@me/22/3')
        self.assertEqual([m['id'] for m in found], ['1', '3'])

    async def test_invalid_range_and_missing_end_rejected(self):
        link = 'https://discord.com/channels/@me/22/2'
        for end in ('https://discord.com/channels/@me/23/3', 'https://discord.com/channels/@me/22/1'):
            with self.assertRaises(hb.HotbotImportError):
                await hb.fetch_messages(link, token='fake', owner='owner', end_link=end)
        for middle in ({'id': '3', 'author': {'id': 'owner'}},
                       {**self.forwarded(3), 'author': {'id': 'other'}}):
            messages = [self.forwarded(2), middle, self.forwarded(4)]
            with patch.object(hb.aiohttp, 'ClientSession', self.range_session(messages)), self.assertRaises(hb.HotbotImportError):
                await hb.fetch_messages(link, token='fake', owner='owner', end_link='https://discord.com/channels/@me/22/4')
        with patch.object(hb.aiohttp, 'ClientSession', self.range_session([self.forwarded(2)])), self.assertRaises(hb.HotbotImportError):
            await hb.fetch_messages(link, token='fake', owner='owner', end_link='https://discord.com/channels/@me/22/4')

    async def test_range_limit_does_not_silently_truncate(self):
        with patch.object(hb.aiohttp, 'ClientSession', self.range_session([self.forwarded(i) for i in range(1, 503)])), self.assertRaises(hb.HotbotImportError):
            await hb.fetch_messages('https://discord.com/channels/@me/22/1', token='fake', owner='owner',
                                    end_link='https://discord.com/channels/@me/22/502')

    def test_forwarded_hotbot_fixture_matches_original(self):
        fixture = json.loads((Path(__file__).parent / 'fixtures/hotbot_compact.json').read_text(encoding='utf-8'))
        forwarded = [{'message_snapshots': [{'message': m}]} for m in fixture]
        self.assertEqual(hb.parse_messages(forwarded), hb.parse_messages(fixture))


class GuildRangeImportTests(unittest.TestCase):
    def test_range_preview_then_apply_builds_one_modular_template(self):
        from unittest.mock import AsyncMock
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from starlette.middleware.sessions import SessionMiddleware
        from web.deps import require_officer_access
        from web.routes import stat_plates as route

        fixture = json.loads((Path(__file__).parent / 'fixtures/hotbot_compact.json').read_text(encoding='utf-8'))
        messages = [{'message_snapshots': [{'message': message}]} for message in fixture]
        names = ['Cassian Andor (Undercover)', 'Luthen Rael', 'Kleya Marki', 'Cinta Kaz', 'Vel Sartha']
        app = FastAPI()
        app.add_middleware(SessionMiddleware, secret_key='test')
        app.include_router(route.router, prefix='/plates')
        user = {'guild_id': 1, 'tier': 'officer', 'discord_id': 'owner', 'features': {}, 'bot_profile': 'prod'}
        app.dependency_overrides[require_officer_access] = lambda: user
        fetch = AsyncMock(return_value=messages)
        with tempfile.TemporaryDirectory() as tmp, patch.object(database, 'DB_NAME', str(Path(tmp) / 'test.db')), \
                patch.object(hb, 'fetch_messages', fetch), \
                patch.object(database, 'get_game_unit_en_names', return_value={name: name for name in names}), TestClient(app) as client:
            database.init_db()
            links = {'link': 'https://discord.com/channels/@me/22/33',
                     'end_link': 'https://discord.com/channels/@me/22/55'}
            preview = client.get('/plates/import', params=links)
            self.assertEqual(preview.status_code, 200)
            self.assertIn('name="end_link" value="' + links['end_link'] + '"', preview.text)
            self.assertEqual(database.get_all_stat_requirement_plates(1), [])
            selected = re.findall('name="sel" value="([^"]+)"', preview.text)
            self.assertGreater(len(selected), 40)
            applied = client.post('/plates/import', data={**links, 'action': 'apply', 'sel': selected, 'build_parent': '1'})
            self.assertEqual(applied.status_code, 200)
            self.assertIn('Модульный плейт: AC_CASSIAN', applied.text)
            fetch.assert_awaited_with(links['link'], owner='owner', profile='prod', end_link=links['end_link'])
            self.assertEqual(len(database.get_stat_plate_components('AC_CASSIAN', 1)), 5)
            self.assertEqual(len(database.get_all_stat_requirements(1)), len(selected))
            self.assertEqual(database.get_all_stat_requirement_plates(2), [])
