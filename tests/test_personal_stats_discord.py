import asyncio
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

import database
from cogs import stat_requirements as stats
from services import personal_plates as plates


class PersonalStatsDiscordTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.stack = ExitStack()
        self.stack.enter_context(patch.object(database, 'DB_NAME', str(Path(self.temp.name) / 'test.db')))
        database.init_db()
        self.user = {'discord_id': '1', 'guild_id': 1}
        self.pid = plates.create(self.user, 'Мой плейт', [{'kind': 'num', 'char': 'VADER', 'stat': 'Speed', 'op': '>=', 'value': 300}])
        self.ref = plates.reference(plates.get(self.pid, self.user))
        self.alias = plates.PREFIX + '#' + self.pid
        self.stack.enter_context(patch.object(stats.bot_profiles, 'current_profile', return_value='test'))
        self.stack.enter_context(patch.object(stats.bot_profiles, 'guild_profile', return_value='test'))
        self.stack.enter_context(patch.object(stats.guild_resolver, 'autocomplete_guild_id', return_value=1))
        self.stack.enter_context(patch.object(stats.guild_resolver, 'require_feature', AsyncMock(return_value=1)))
        self.stack.enter_context(patch.object(stats.guild_resolver, 'target_player', return_value=('123', 'Игрок', None)))
        self.stack.enter_context(patch.object(stats.guild_resolver, 'self_only_error', return_value=None))
        self.stack.enter_context(patch.object(stats.stat_engine, 'wait_stat_calc', AsyncMock(return_value=True)))
        self.stack.enter_context(patch.object(database, 'get_discord_id_for_ally', return_value=None))
        self.stack.enter_context(patch.object(stats, '_stats_scheme_model', return_value=None))
        self.evaluate = self.stack.enter_context(patch.object(stats, '_evaluate_character_player',
            AsyncMock(return_value=('Вейдер', 'Проверка', 1, 1, None, [], 1, None, None))))
        self.stack.enter_context(patch.object(stats.asyncio, 'create_task', side_effect=lambda coroutine: coroutine.close()))
        self.stack.enter_context(patch.dict(stats._stats_pagers, {}, clear=True))
        self.cog = stats.StatRequirementsCog.__new__(stats.StatRequirementsCog)
        self.cog.bot = SimpleNamespace(comlink=None)

    def tearDown(self):
        self.stack.close()
        self.temp.cleanup()

    def inter(self, owner=1, guild=9):
        return SimpleNamespace(id=123, author=SimpleNamespace(id=owner), channel_id=10, guild_id=guild,
            application_command=SimpleNamespace(name='статы'), filled_options={'плейт': self.alias},
            response=SimpleNamespace(defer=AsyncMock(), send_message=AsyncMock()),
            edit_original_response=AsyncMock(), original_response=AsyncMock(return_value=SimpleNamespace(edit=AsyncMock())),
            followup=SimpleNamespace(send=AsyncMock(return_value=SimpleNamespace(jump_url='https://discord.com/channels/9/10/11'))))

    async def command(self, inter, plate=None, guild=False, only_failed=False):
        await stats.StatRequirementsCog.stats_check.callback(self.cog, inter,
            плейт=plate or self.alias, участник=None, аликод=None, персонаж=None, обновить=False, гильдия=guild,
            сценарий=stats.SCENARIO_RAW, вариант=None, с_отрядом=False, только_кресты=only_failed, альт=False)

    async def test_autocomplete_and_character_choice_are_owner_scoped(self):
        mine = await stats.autocomplete_stat_plate(self.inter(), '')
        self.assertEqual(mine[0].value, self.alias)
        self.assertNotIn(self.pid, mine[0].name)
        self.assertEqual(await stats.autocomplete_stat_plate(self.inter(owner=2), ''), [])
        plates.share(self.user, self.pid, True)
        shared = await stats.autocomplete_stat_plate(self.inter(owner=2), '')
        self.assertEqual(shared[0].value, self.alias)
        self.assertTrue(await stats.autocomplete_stat_character(self.inter(owner=2), ''))
        plates.share(self.user, self.pid, False)
        self.assertEqual(await stats.autocomplete_stat_character(self.inter(owner=2), ''), [])

    async def test_personal_is_hidden_including_summary_only_and_forged_foreign_choice(self):
        for only_failed in (False, True):
            inter = self.inter()
            await self.command(inter, only_failed=only_failed)
            inter.response.defer.assert_awaited_once_with(ephemeral=True)
            inter.followup.send.assert_not_awaited()
            state = stats._stats_pagers['123']
            self.assertTrue(state['private_personal'])
            rendered = str([c.to_component_dict() for c in stats._render_stats_page(state)])
            self.assertIn('Показать всем', rendered)
            self.assertNotIn(self.pid, rendered)
        foreign = self.inter(owner=2)
        self.evaluate.reset_mock()
        await self.command(foreign)
        foreign.response.defer.assert_awaited_once_with(ephemeral=True)
        self.evaluate.assert_not_awaited()
        self.assertIn('недоступен', foreign.edit_original_response.call_args.args[0])

    async def test_guild_wide_personal_is_hidden_and_guild_plate_keeps_public_default(self):
        with patch.object(stats.guild_resolver, 'is_officer_for_resolved_guild', return_value=True), \
                patch.object(stats, '_build_guild_report', AsyncMock(return_value={
                    'error': None, 'compliant': [], 'total_players': 0, 'no_data': [], 'skipped': [], 'problem': []})):
            inter = self.inter()
            await self.command(inter, guild=True)
            inter.response.defer.assert_awaited_once_with(ephemeral=True)
            self.assertTrue(stats._stats_pagers['123']['private_personal'])
        database.create_stat_plate('Общий', '', 'officer', guild_id=1)
        database.add_stat_requirement('Общий', 'VADER', 'Speed', '>=', 300, 'required', '', None, 'officer', guild_id=1)
        normal = self.inter()
        await self.command(normal, plate='Общий')
        normal.response.defer.assert_awaited_once_with(ephemeral=False)
        self.assertFalse(stats._stats_pagers['123']['private_personal'])

    async def test_reveal_is_owner_only_same_server_channel_once_and_does_not_share_plate(self):
        inter = self.inter()
        await self.command(inter)
        state = stats._stats_pagers['123']
        for forbidden in (self.inter(owner=2), self.inter(guild=None)):
            await stats._reveal_personal_stats(forbidden, state)
            self.assertTrue(forbidden.followup.send.call_args.kwargs['ephemeral'])
        with patch.object(stats, '_check_stats_pager_access', AsyncMock(return_value=state)):
            inter.component = SimpleNamespace(custom_id='stp|123|reveal')
            await self.cog.on_stats_pager_click(inter)
            await self.cog.on_stats_pager_click(inter)
        inter.followup.send.assert_awaited_once()
        self.assertFalse(inter.followup.send.call_args.kwargs['ephemeral'])
        public = next(value for key, value in stats._stats_pagers.items() if key != '123')
        self.assertFalse(public['private_personal'])
        self.assertFalse(plates.get(self.pid, self.user)['shared'])
        self.assertIn('Открыть публикацию', str([c.to_component_dict() for c in stats._render_stats_page(state)]))

    async def test_revoked_shared_plate_cannot_be_revealed(self):
        plates.share(self.user, self.pid, True)
        inter = self.inter(owner=2)
        await self.command(inter)
        plates.share(self.user, self.pid, False)
        await stats._reveal_personal_stats(inter, stats._stats_pagers['123'])
        self.assertTrue(inter.followup.send.call_args.kwargs['ephemeral'])
        self.assertNotIn('published_url', stats._stats_pagers['123'])

    async def test_personal_share_fits_five_rows_with_three_character_selects(self):
        pages, picks = stats._build_stats_pages('Личный плейт', ['Итог'],
            [(f'Персонаж {i}', '✅', f'Персонаж {i}') for i in range(75)],
            [(f'Персонаж {i}', 'Статы') for i in range(75)])
        inter = self.inter()
        state = await stats._start_stats_pager(inter, pages, picks, 0, '', 1, {1}, personal_ref=self.ref)
        for index in (0, 1):
            state['index'] = index
            components = [c.to_component_dict() for c in stats._render_stats_page(state)]
            self.assertLessEqual(sum(c['type'] == 1 for c in components), 5)
            self.assertIn('Показать всем', str(components))
