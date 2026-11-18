import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.middleware.sessions import SessionMiddleware

import database
from services import tb_atlas, tb_personal, tb_squads
from web.deps import require_member_access
from web.routes import tb_map


class PersonalMapTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = patch.object(database, 'DB_NAME', str(Path(self.tmp.name) / 'map.db'))
        self.db.start()
        database.init_db()
        self.user = {'guild_id': 1, 'discord_id': 'alice', 'tier': 'member', 'features': {}}
        self.app = FastAPI()
        self.app.add_middleware(SessionMiddleware, secret_key='test')
        self.app.include_router(tb_map.router, prefix='/tb/map')
        self.app.dependency_overrides[require_member_access] = lambda: self.user
        self.client = TestClient(self.app, follow_redirects=False)
        mission = {'id': 'c1', 'type': 'usual', 'label': 'Бой', 'requirements': ['Реликвия 5+'],
                   'enemies': ['Противники'], 'squads': [
                       {'key': 'base1', 'name': 'Тестовый пак', 'units': ['VADER'], 'omicron': [], 'video': ''}]}
        database.set_bot_state(tb_squads.BASE_KEY, json.dumps({'planets': {
            'Mustafar': {'phase': 1, 'bonus': False, 'missions': [mission]}}}), guild_id=0)

    def tearDown(self):
        self.client.close()
        self.db.stop()
        self.tmp.cleanup()

    def save(self, body, mission='c1', **extra):
        return self.client.post('/tb/map/save', data={'planet': 'Mustafar', 'mission': mission, 'body': body, **extra})

    def test_all_planets_render_without_active_tb_or_roster(self):
        r = self.client.get('/tb/map')
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.text.count('class="tb-world tb-side-'), 20)
        self.assertIn('Тестовый пак', r.text)
        self.assertIn('data-pack=', r.text)
        self.assertIn('/tb/map', r.text)

    def test_notes_private_across_users_and_reusable_across_guilds(self):
        self.assertEqual(self.save('Мой пак').status_code, 303)
        self.user['guild_id'] = 2
        self.assertIn('Мой пак</textarea>', self.client.get('/tb/map').text)
        self.user['discord_id'] = 'bob'
        self.assertNotIn('Мой пак</textarea>', self.client.get('/tb/map').text)
        self.save('Пак Боба', discord_id='alice')
        self.assertEqual(tb_personal.notes('alice')[('Mustafar', 'c1')], 'Мой пак')
        self.assertEqual(tb_personal.notes('bob')[('Mustafar', 'c1')], 'Пак Боба')

    def test_map_uses_catalog_sides_and_keeps_mission_keys(self):
        parsed = tb_squads.parse({'planets': [{'nameEn': 'Mustafar', 'phase': 1, 'missions': [
            {'missionId': 'c1', 'type': 'usual', 'position': 'left'},
            {'missionId': 'c3', 'type': 'usual', 'position': 'right'},
            {'missionId': 'c6', 'type': 'fleet', 'position': 'right'},
        ]}]})
        database.set_bot_state(tb_squads.BASE_KEY, json.dumps({'planets': parsed}), guild_id=0)
        result = self.save('Мой второй бой', mission='c3')
        html = self.client.get(result.headers['location']).text
        left = html.split('class="tb-mission-lane tb-mission-left">')[1].split('</div>')[0]
        right = html.split('class="tb-mission-lane tb-mission-right">')[1].split('</div>')[0]
        self.assertIn('href="#mission-c1"', left)
        self.assertIn('Наземный бой 1', left)
        self.assertIn('href="#mission-c3" class="tb-mission-node tb-node-usual" aria-current="true"', right)
        self.assertIn('Наземный бой 2', right)
        self.assertIn('Бой флота', right)
        self.assertIn('Есть памятка', right)
        self.assertIn('Мой второй бой</textarea>', html)

    def test_all_battle_variants_open_with_their_mission_pins(self):
        for event, (zones, pins) in {
            "rote": (20, 0), "geo-light": (12, 29), "geo-dark": (11, 28),
            "hoth-light": (15, 27), "hoth-dark": (16, 29),
        }.items():
            with self.subTest(event=event):
                response = self.client.get(f'/tb/map?embedded=true&event={event}')
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.text.count('class="tb-world tb-side-'), zones)
                self.assertEqual(response.text.count('class="tb-map-pin '), pins)
                self.assertIn(f'img/tb/{tb_atlas.EVENTS[event]["background"]}.webp', response.text)

    def test_rote_planet_replaces_overview_with_surface_and_pins(self):
        page = self.client.get('/tb/map?planet=Mustafar&embedded=true')
        self.assertEqual(page.status_code, 200)
        self.assertNotIn('id="atlas"', page.text)
        self.assertIn('img/tb/surfaces/mustafar.webp', page.text)
        self.assertIn('class="tb-surface-pin" href="#mission-c1"', page.text)
        self.assertIn('mission-usual.png', page.text)
        self.assertIn('id="tb-mission-dialog"', page.text)
        self.assertIn('href="/tb/map?embedded=true#atlas"', page.text)
        self.assertEqual(set(tb_atlas.ROTE_MISSION_PINS), {
            tb_atlas.ROTE_ART_NAMES.get(key, key.lower()) for key in tb_atlas.ROTE_COORDINATES
        })

    def test_enemy_waves_override_outdated_free_text(self):
        raw = {'planets': [{'nameEn': 'Geonosis', 'missions': [
            {'missionId': 'c2', 'type': 'usual', 'enemies': ['Wave 1: Reek'],
             'enemyWaves': [{'wave': 1, 'units': [{'name': 'Аклай', 'count': 1}]}]},
            {'missionId': 'c3', 'type': 'usual', 'enemies': ['Противник из старого каталога']},
        ]}]}
        missions = tb_squads.parse(raw)['Geonosis']['missions']
        self.assertEqual(missions[0]['enemies'], ['Волна 1: Аклай'])
        self.assertEqual(missions[1]['enemies'], ['Противник из старого каталога'])

    def test_legacy_mission_link_and_note_keep_the_battle_selected(self):
        event = 'geo-dark'
        zones = tb_atlas.legacy_zones(event)
        planet = next(reversed(zones))
        mission = zones[planet]['missions'][0]['id']
        page = self.client.get(f'/tb/map?event={event}&embedded=true')
        self.assertIn(f'&amp;event={event}&amp;mission={mission}&amp;embedded=true#mission-{mission}', page.text)
        fields = {'planet': planet, 'mission': mission, 'body': 'Памятка Джеонозиса', 'embedded': 'true'}
        self.assertEqual(self.client.post('/tb/map/save', data={**fields, 'event': 'hoth-dark'}).status_code, 400)
        saved = self.client.post('/tb/map/save', data={**fields, 'event': event})
        self.assertEqual(saved.status_code, 303)
        self.assertIn(f'event={event}', saved.headers['location'])
        html = self.client.get(saved.headers['location']).text
        self.assertIn('Памятка Джеонозиса</textarea>', html)
        self.assertIn('Есть памятка', html)

    def test_save_escape_delete_and_return_to_mission(self):
        r = self.save('<script>alert(1)</script>')
        self.assertIn('mission=c1', r.headers['location'])
        self.assertTrue(r.headers['location'].endswith('#mission-map'))
        html = self.client.get(r.headers['location']).text
        self.assertIn('&lt;script&gt;', html)
        self.assertNotIn('<script>alert(1)</script>', html)
        self.save('   ')
        self.assertEqual(tb_personal.notes('alice'), {})

    def test_invalid_target_and_oversized_note_do_not_write(self):
        self.assertEqual(self.save('x', mission='unknown').status_code, 400)
        self.assertEqual(self.save('x', planet='unknown').status_code, 400)
        self.assertEqual(self.save('x' * 4001).status_code, 422)
        self.assertEqual(self.client.get('/tb/map?planet=unknown').status_code, 404)
        self.assertEqual(tb_personal.notes('alice'), {})

    def test_removed_mission_note_stays_accessible(self):
        self.save('Не терять')
        database.set_bot_state(tb_squads.BASE_KEY, '', guild_id=0)
        html = self.client.get('/tb/map').text
        self.assertIn('Архивная миссия', html)
        self.assertIn('Не терять</textarea>', html)
        self.assertEqual(self.save('Изменено').status_code, 303)

    def test_guild_hidden_squad_is_not_offered(self):
        tb_squads.set_hidden(1, 'Mustafar', 'c1', 'base1', True, 'officer')
        self.assertNotIn('Тестовый пак', self.client.get('/tb/map').text)

    def test_outsider_cannot_read_or_write(self):
        del self.app.dependency_overrides[require_member_access]
        with patch('web.deps.get_current_user', return_value={'tier': 'none', 'guild_id': None}):
            self.assertEqual(self.client.get('/tb/map').status_code, 403)
            self.assertEqual(self.save('Чужая запись').status_code, 403)
        self.assertEqual(tb_personal.notes('alice'), {})

    def test_embedded_navigation_and_save_keep_profile_layout(self):
        response = self.client.get('/tb/map?embedded=true')
        self.assertEqual(response.status_code, 200)
        self.assertNotIn('class="sidebar"', response.text)
        self.assertIn('name="embedded" value="true"', response.text)
        self.assertIn('&amp;embedded=true#planet', response.text)
        result = self.save('Пак из профиля', embedded='true')
        self.assertIn('embedded=true', result.headers['location'])
        self.assertIn('Пак из профиля</textarea>', self.client.get(result.headers['location']).text)


if __name__ == '__main__':
    unittest.main()
