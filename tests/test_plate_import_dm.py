import copy
import unittest
from unittest.mock import patch
from services import hotbot_import as hb


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
