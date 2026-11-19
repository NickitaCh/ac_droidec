import copy
import json
from pathlib import Path
import re
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.middleware.sessions import SessionMiddleware
import database
from services import personal_plates as plates, mod_optimizer_v2 as engine, mod_optimizer_plans as plans
from services import game_account as ga
from web.routes import personal_plates as route


class PersonalPlateTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = patch.object(database, 'DB_NAME', str(Path(self.temp.name) / 'test.db'))
        self.db.start()
        database.init_db()
        self.user = {'discord_id': 'one', 'guild_id': 1, 'bot_profile': 'test', 'tier': 'member', 'is_super_admin': False}
        self.other = {**self.user, 'discord_id': 'two'}
        self.foreign = {**self.other, 'guild_id': 2}
        self.item = {'kind': 'num', 'char': 'VADER', 'stat': 'Speed', 'op': '>=', 'value': 150, 'priority': 'required'}

    def tearDown(self):
        self.db.stop()
        self.temp.cleanup()

    def test_private_shared_revoked_and_owner_only_writes(self):
        pid = plates.create(self.user, 'Мой плейт', [self.item])
        ref = plates.reference(plates.get(pid, self.user))
        self.assertEqual(plates.list_plates(self.other), [])
        with self.assertRaises(ValueError):
            database.get_stat_requirements(ref, 'VADER', 1)
        for user in (self.other, self.foreign):
            with plates.scope(user), self.assertRaises(ValueError):
                database.get_stat_requirements(ref, 'VADER', user['guild_id'])
            for operation in (lambda: plates.share(user, pid, True), lambda: plates.delete(user, pid),
                              lambda: plates.add(user, pid, self.item)):
                with self.assertRaises(ValueError):
                    operation()
        plates.share(self.user, pid, True)
        self.assertEqual(len(plates.list_plates(self.other)), 1)
        with plates.scope(self.other):
            rows = database.get_stat_requirements(ref, 'VADER', 1)
            self.assertEqual((len(rows[0]), rows[0][2:7]), (15, ('VADER', 'Speed', '>=', 150, 'required')))
            fingerprint = plans.requirements_fingerprint([ref], 1)
        with plates.scope(self.foreign), self.assertRaises(ValueError):
            database.get_stat_requirements(ref, 'VADER', 2)
        plates.add(self.user, pid, {**self.item, 'value': 160})
        with plates.scope(self.user):
            self.assertNotEqual(fingerprint, plans.requirements_fingerprint([ref], 1))
        plates.share(self.user, pid, False)
        with plates.scope(self.other), self.assertRaises(ValueError):
            plans.requirements_fingerprint([ref], 1)
        self.assertNotIn(ref, database.get_all_stat_requirement_plates(1))

    async def test_personal_rows_use_real_statcalc_in_evaluator_and_optimizer(self):
        from swgoh_comlink import StatCalc
        from services import stat_forecast
        calc = StatCalc(json.loads((Path(__file__).parent / 'fixtures/mod_optimizer_statcalc.json').read_text()))
        unit = {'definitionId': 'VADER:SEVEN_STAR', 'currentRarity': 7, 'currentLevel': 85,
                'currentTier': 13, 'relic': {'currentTier': 9}, 'skill': [], 'equipment': [], 'equippedStatMod': []}
        pid = plates.create(self.user, 'Скорость', [{**self.item, 'value': 140}])
        ref = plates.reference(plates.get(pid, self.user))
        with plates.scope(self.user), patch.object(database, 'get_player_unit', return_value=(copy.deepcopy(unit), None)), \
                patch.object(database, 'get_game_unit_name', return_value='Вейдер'), \
                patch.object(database, 'get_unit_types', return_value={'VADER': 'character'}):
            outcome = await stat_forecast.evaluate_character_player(None, calc, ref, 'VADER', '123', False, 'Игрок', guild_id=1)
            self.assertEqual(outcome[2:4], (1, 1))
            result, passed, _, _ = await engine.optimize(None, calc, [ref], '123', [], 1, units_override={'VADER': unit})
            self.assertEqual(passed, [ref])
            self.assertTrue(result[0]['ok'])

    def app(self):
        app = FastAPI()
        app.add_middleware(SessionMiddleware, secret_key='test')
        app.include_router(route.router, prefix='/personal-plates')
        app.dependency_overrides[route._member_access] = lambda: self.user
        return app

    def test_member_import_preview_selective_save_and_guild_untouched(self):
        fixture = json.loads((Path(__file__).parent / 'fixtures/hotbot_compact.json').read_text(encoding='utf-8'))
        names = ['Cassian Andor (Undercover)', 'Luthen Rael', 'Kleya Marki', 'Cinta Kaz', 'Vel Sartha']
        with TestClient(self.app()) as client, patch.object(route.hb, 'fetch_messages', AsyncMock(return_value=fixture)), \
                patch.object(database, 'get_game_unit_en_names', return_value={name: name for name in names}):
            form = client.get('/personal-plates/import')
            csrf = re.search('name="csrf" value="([^"]+)"', form.text).group(1)
            payload = {'csrf': csrf, 'link': 'https://discord.com/channels/1/2/3', 'name': 'Личный импорт'}
            self.assertEqual(client.post('/personal-plates/create', data={'name': 'No csrf'}).status_code, 403)
            preview = client.post('/personal-plates/import', data=payload)
            self.assertEqual(preview.status_code, 200)
            self.assertIn('число омикронов', preview.text)
            self.assertEqual(plates.list_plates(self.user), [])
            digest = re.search('name="digest" value="([^"]+)"', preview.text).group(1)
            changed = client.post('/personal-plates/import', data={**payload, 'action': 'save', 'digest': 'old', 'selected': ['0']})
            self.assertIn('Сообщение изменилось', changed.text)
            saved = client.post('/personal-plates/import', data={**payload, 'action': 'save', 'digest': digest, 'selected': ['0']}, follow_redirects=False)
            self.assertEqual(saved.status_code, 303)
            mine = plates.list_plates(self.user)
            self.assertEqual(len(mine), 1)
            self.assertEqual(len(plates.rows(mine[0])), 1)
            self.assertEqual(database.get_all_stat_requirement_plates(1), [])
            with patch.object(database, 'get_user_registrations', return_value=[]):
                self.assertEqual(client.get(saved.headers['location']).status_code, 200)
            self.user['bot_profile'] = 'prod'
            self.assertEqual(client.get('/personal-plates').status_code, 404)

    def test_test_members_mod_write_gate_preserves_other_game_and_prod_gates(self):
        with patch.object(ga.guild_resolver, 'resolve_access', return_value={'guild_id': 2, 'tier': 'member'}), \
                patch('services.bot_profiles.guild_profile', return_value='test'):
            ga.require_mod_actions('one', 2, 'test', 'member')
            with self.assertRaises(ga.AccountError):
                ga.require_mod_actions('one', 1, 'test', 'member')
            with self.assertRaises(ga.AccountError):
                ga.require_game_officer('one', 2, 'test', 'member')
        with patch.object(ga.guild_resolver, 'resolve_access', return_value={'guild_id': 2, 'tier': 'member'}), \
                patch('services.bot_profiles.guild_profile', return_value='prod'):
            with self.assertRaises(ga.AccountError):
                ga.require_mod_actions('one', 2, 'prod', 'member')

    def test_manual_editor_and_shared_page_reject_another_editor(self):
        with TestClient(self.app()) as client, \
                patch.object(database, 'get_game_unit_name', side_effect=lambda base: 'Вейдер' if base == 'VADER' else None), \
                patch.object(database, 'get_game_unit_names', return_value={'VADER': 'Вейдер'}), \
                patch.object(database, 'get_user_registrations', return_value=[]):
            csrf = re.search('name="csrf" value="([^"]+)"', client.get('/personal-plates').text).group(1)
            created = client.post('/personal-plates/create', data={'csrf': csrf, 'name': 'Мой'}, follow_redirects=False)
            path = created.headers['location']
            pid = path.rsplit('/', 1)[-1]
            payload = {'csrf': csrf, 'base_id': 'VADER', 'stat': 'Speed', 'operator': '>=', 'value': '320', 'priority': 'required'}
            client.post(path+'/add', data=payload)
            plate = plates.get(pid, self.user)
            row_id = plates.rows(plate)[0][0]
            client.post(path+'/edit', data={'csrf': csrf, 'row_id': row_id, 'operator': '>=', 'value': '330', 'priority': 'optional'})
            self.assertEqual(plates.rows(plate)[0][5:7], (330, 'optional'))
            client.post(path+'/share', data={'csrf': csrf, 'shared': '1'})
            self.user['discord_id'] = 'two'
            shared = client.get(path)
            self.assertEqual(shared.status_code, 200)
            self.assertNotIn('action="'+path+'/edit"', shared.text)
            client.post(path+'/remove', data={'csrf': csrf, 'row_id': row_id})
            self.assertEqual(len(plates.rows(plate)), 1)
            self.user['guild_id'] = 2
            self.assertEqual(client.get(path).status_code, 404)

    def test_droidec_dm_import_copies_source_requirements_and_rejects_foreign_plate(self):
        database.create_stat_plate('AC_TEST', '', 'officer', guild_id=1)
        database.add_stat_requirement('AC_TEST', 'VADER', 'Speed', '>=', 320, 'optional', '', None, 'officer', guild_id=1)
        message = {'_droidec_dm': True, 'components': [{'type': 17, 'components': [
            {'type': 10, 'content': '### 📋 AC_TEST (игрок: TEO Nod) — итог'},
            {'type': 10, 'content': 'Только итог, без числовых требований.'}]}]}
        title, items, skipped = plates.parse_source([message], self.user)
        self.assertEqual((title, len(items), skipped), ('AC_TEST', 1, []))
        pid = plates.create(self.user, title, items)
        copied = plates.rows(plates.get(pid, self.user))
        self.assertEqual(copied[0][2:7], ('VADER', 'Speed', '>=', 320, 'optional'))
        with self.assertRaises(ValueError):
            plates.parse_source([message], self.foreign)
        message['components'][0]['components'][0]['content'] = '### Другое сообщение'
        with self.assertRaises(ValueError):
            plates.parse_source([message], self.user)
