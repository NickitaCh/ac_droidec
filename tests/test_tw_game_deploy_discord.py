import copy
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import disnake

from cogs.tw_game_deploy import TWGameDeployCog, build_panel
from services import game_account as ga, tw_game_deploy as deploy
import test_tw_game_deploy as base
state = base.state


class DiscordEditTests(base.ExecutionTests):
    async def test_edit_invalidates_confirmation_and_rejects_stale_panel(self):
        with patch.object(deploy, 'snapshot', AsyncMock(return_value=state())):
            await deploy.prepare(self.user, self.ticket, {})
            old = deploy.load(self.user, self.ticket)
            rev = deploy.revision(old['data'])
            await deploy.edit_datacron(self.user, self.ticket, 0, '', rev)
            changed = deploy.load(self.user, self.ticket)
            self.assertEqual(changed['state'], 'draft')
            self.assertNotIn('nonce', changed['data'])
            self.assertEqual(changed['data']['assignments'][0]['datacron'], '')
            with self.assertRaises(ga.AccountError):
                await deploy.edit_datacron(self.user, self.ticket, 0, 'dc', rev)

    async def test_used_datacron_and_manual_pack_cannot_be_edited(self):
        live = state()
        live['used_dc'].add('dc')
        record = deploy.load(self.user, self.ticket)
        with patch.object(deploy, 'snapshot', AsyncMock(return_value=live)):
            with self.assertRaises(ga.AccountError):
                await deploy.edit_datacron(self.user, self.ticket, 0, 'dc', deploy.revision(record['data']))
        live['teams'] = [{'zone': 'elsewhere', 'units': ['A', 'B'], 'datacron': 'dc', 'types': [2, 1]}]
        with patch.object(deploy, 'snapshot', AsyncMock(return_value=live)):
            with self.assertRaises(ga.AccountError):
                await deploy.edit_datacron(self.user, self.ticket, 0, '', deploy.revision(record['data']))

    async def test_ready_prepare_cannot_overwrite_concurrent_dc_edit(self):
        original = deploy.load(self.user, self.ticket)
        async def concurrent(*args):
            changed = copy.deepcopy(original['data'])
            changed['assignments'][0]['datacron'] = ''
            deploy._save(self.ticket, changed, 'draft')
            return state()
        with patch.object(deploy, 'snapshot', AsyncMock(side_effect=concurrent)):
            with self.assertRaises(ga.AccountError):
                await deploy.prepare(self.user, self.ticket, {})
        self.assertEqual(deploy.load(self.user, self.ticket)['data']['assignments'][0]['datacron'], '')


class PanelTests(unittest.IsolatedAsyncioTestCase):
    async def test_pagination_hides_used_dc_and_has_no_implicit_apply(self):
        live = state()
        dcs = [{'id': f'dc{i}', 'setId': 35, 'affix': []} for i in range(32)]
        live['datacrons'].update({dc['id']: b'' for dc in dcs})
        live['used_dc'].add('dc0')
        record = {'state': 'draft', 'event': 'war', 'data': {'assignments': [
            {'zone': '1b', 'name': 'Пак', 'units': ['A', 'B'], 'datacron': ''}], 'results': []}}
        embed, view = build_panel('ticket', record, live, {}, {}, dcs)
        selects = [i for i in view.children if isinstance(i, disnake.ui.StringSelect)]
        dc_select = selects[1]
        self.assertEqual(len(dc_select.options), 25)
        self.assertNotIn('dc0', [o.value for o in dc_select.options])
        self.assertFalse(any(':apply:' in (i.custom_id or '') for i in view.children))
        self.assertTrue(any(':page:' in (i.custom_id or '') for i in view.children))
        self.assertTrue(all(len(i.custom_id or '') <= 100 for i in view.children))
        self.assertLess(len(embed), 6000)

    async def test_dm_open_and_select_do_not_apply(self):
        bot = SimpleNamespace(user=SimpleNamespace(id=123))
        cog = TWGameDeployCog(bot)
        user = {'discord_id': 'owner', 'guild_id': 1}
        inter = SimpleNamespace(component=SimpleNamespace(custom_id='twd:ticket:open'),
            response=SimpleNamespace(defer=AsyncMock()), edit_original_response=AsyncMock())
        record = {'data': {'assignments': [{'zone': '1b', 'units': ['A']} ]}}
        with patch.object(cog, '_user', AsyncMock(return_value=user)), patch.object(deploy, 'load', return_value=record), patch.object(cog, '_render', AsyncMock()) as render, patch.object(deploy, 'apply', AsyncMock()) as apply:
            await cog._dispatch(inter)
            inter.response.defer.assert_awaited_once_with(with_message=True, ephemeral=True)
            render.assert_awaited_once()
            apply.assert_not_awaited()

    async def test_foreign_owner_denied_before_snapshot_or_write(self):
        cog = TWGameDeployCog(SimpleNamespace(user=SimpleNamespace(id=123)))
        inter = SimpleNamespace(component=SimpleNamespace(custom_id='twd:ticket:open'),
            response=SimpleNamespace(defer=AsyncMock()), edit_original_response=AsyncMock())
        with patch.object(cog, '_user', AsyncMock(return_value={'discord_id': 'intruder'})), patch.object(deploy, 'load', side_effect=ga.AccountError('Другой игрок')), patch.object(deploy, 'snapshot', AsyncMock()) as snapshot, patch.object(deploy, 'apply', AsyncMock()) as apply:
            await cog._dispatch(inter)
            snapshot.assert_not_awaited()
            apply.assert_not_awaited()
            self.assertIn('Другой игрок', inter.edit_original_response.await_args.kwargs['content'])
