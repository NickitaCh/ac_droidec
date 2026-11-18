import unittest
from unittest.mock import AsyncMock, Mock, patch
from starlette.requests import Request
from starlette.datastructures import FormData

from cogs import datacron_requirements as datacrons
from services import shared_datacron_catalog
from web.routes.tw_game_deploy import datacron_option
from web.routes import tw_game_deploy as web_deploy
from services import game_account as ga


class DeploymentActionTests(unittest.IsolatedAsyncioTestCase):
    async def run_action(self, action):
        request = Request({'type': 'http', 'session': {'tw_deploy_csrf': 'csrf'}})
        request._form = FormData({'dc_0': 'chosen'})
        user = {'discord_id': 'owner', 'guild_id': 1, 'bot_profile': 'test', 'tier': 'officer'}
        record = {'code': '123', 'data': {'assignments': [{}]}}
        with (patch.object(web_deploy.deploy, 'load', return_value=record),
              patch.object(web_deploy.deploy, 'prepare', AsyncMock(side_effect=[ga.SessionExpired('expired'), None])) as prepare,
              patch.object(ga, 'reconnect', AsyncMock()) as reconnect,
              patch.object(web_deploy, 'display', AsyncMock()) as display):
            await web_deploy.action(request, 'ticket', csrf='csrf', action=action,
                                    nonce='', confirm_login=False, confirm_deploy=False, user=user)
        return prepare, reconnect, display

    async def test_expired_session_requires_explicit_reconnect(self):
        prepare, reconnect, display = await self.run_action('prepare')
        reconnect.assert_not_awaited()
        self.assertEqual(prepare.await_count, 1)
        self.assertTrue(display.call_args.args[-1])
        self.assertEqual(display.call_args.args[-2], {'0': 'chosen'})

    async def test_reconnect_button_retries_check_with_selected_datacron(self):
        prepare, reconnect, _ = await self.run_action('reconnect')
        reconnect.assert_awaited_once()
        self.assertEqual(prepare.await_count, 2)
        self.assertEqual(prepare.call_args.args[-1], {'0': 'chosen'})


class DatacronOptionTests(unittest.IsolatedAsyncioTestCase):
    async def test_focused_bonus_names_its_own_target(self):
        template_id = 'datacron_set_36_focused_lukestarkiller'
        ability = 'datacron_character_generic_038'
        veers_ability = 'datacron_character_generic_007'
        game_data = {
            'datacronSet': [{'id': 36, 'expirationTimeMs': '9999999999999'}],
            'datacronTemplate': [{
                'id': template_id, 'setId': 36, 'focused': True,
                'tier': [
                    {'id': 3, 'affixTemplateSetId': ['focused_tier3']},
                    {'id': 6, 'affixTemplateSetId': ['focused_tier6']},
                ],
            }],
            'datacronAffixTemplateSet': [{
                'id': 'focused_tier3',
                'affix': [{'abilityId': ability, 'targetRule': 'target_datacron_hansolo'}],
            }, {
                'id': 'focused_tier6',
                'affix': [{'abilityId': veers_ability, 'targetRule': 'target_datacron_generalveers'}],
            }],
        }
        comlink = Mock()
        comlink.get_game_data.return_value = game_data
        comlink.get_localization.return_value = {'Loc_RUS_RU.txt': '\n'.join([
            'UNIT_HANSOLO_NAME|Хан Соло',
            'UNIT_VEERS_GENERAL_NAME|Генерал Вирс',
            f'{ability.upper()}_DESC|Когда {{0}} оглушает врага, он получает бонус.',
            f'{veers_ability.upper()}_DESC|Когда {{0}} делает свой ход, он получает бонус.',
        ])}
        owned = {
            'setId': 36, 'focused': True, 'templateId': template_id,
            'affix': [{}, {}, {'abilityId': ability}, {}, {}, {'abilityId': veers_ability}],
        }
        with (patch.object(shared_datacron_catalog, 'load', return_value=None),
              patch.object(shared_datacron_catalog, 'save'),
              patch.object(datacrons.database, 'get_datacron_ability_shorts', return_value={}),
              patch.object(datacrons.database, 'get_datacron_shorts_version', return_value='')):
            catalog = await datacrons._refresh_datacron_cache.__wrapped__(comlink)
        option = datacron_option('owned', owned, catalog, 1)
        self.assertEqual(option['details'], '3: Когда Хан Соло оглушает врага, он получает бонус.\n'
                                            '6: Когда Генерал Вирс делает свой ход, он получает бонус.')
        self.assertNotIn(ability, option['name'])
        self.assertEqual(catalog['seasons'][36]['level3'], [])
