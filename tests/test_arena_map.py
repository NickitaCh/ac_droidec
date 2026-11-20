import copy
from io import BytesIO
import unittest
from unittest.mock import patch
from PIL import Image, ImageChops
from services import arena_map_image as image, game_personal_modes as modes, game_account as ga


def msg(tag, *fields):
    return tag, 2, ga.pack(fields)


def num(tag, value):
    return tag, 0, value


class ArenaMapTests(unittest.TestCase):
    def test_real_zone_ids_place_fleet_behind_upper_front_despite_response_order(self):
        zones = [{'id':f'tt4v4_phase0{p}_conflict0{c}'} for p,c in ((2,2),(1,2),(2,1),(1,1))]
        placed = image.positioned_zones(zones)
        self.assertEqual([(p,z['id']) for p,_,z in placed], [
            (0,'tt4v4_phase01_conflict01'), (1,'tt4v4_phase02_conflict01'),
            (2,'tt4v4_phase01_conflict02'), (3,'tt4v4_phase02_conflict02')])
        self.assertEqual(image.zone_label(zones[2],0,'tt4v4_5v5char_1ship'),'ФЛОТ · ТЫЛ')
        self.assertEqual(image.zone_label(zones[2],0,'tt4v4_3v3char_0ship'),'ТЫЛ · ВЕРХ')

    def test_hidden_rear_zones_keep_positions_and_do_not_reorder_squads(self):
        front = {'id':'4zone_phase01_conflict02','teams':[{'units':['VADER']}]}
        placed = image.positioned_zones([front])
        self.assertEqual([p for p,_,_ in placed],[0,1,2,3])
        self.assertIs(placed[2][2],front)
        self.assertIsNone(placed[1][2]['remaining'])

    def test_combat_counter_keeps_defeated_teams_and_counts_survivors(self):
        def squad(state):
            return msg(3, num(6, state), num(8, 1), msg(4, msg(1, (2, 2, b'VADER:SEVEN_STAR'))))
        zone = msg(3, msg(1, (1, 2, b'zone')), num(2, 5), num(4, 2), squad(1), squad(2), squad(3))
        match = msg(2, num(5, 1), num(8, 1), msg(2, zone), msg(3, zone))
        state = modes.arena(ga.pack([msg(4, (1, 2, b'arena'), num(3, 1), match)]))
        current = state['matches'][0]
        zone = current['away'][0]
        self.assertEqual((zone['remaining'], zone['placed'], zone['defeated']), (2, 3, 1))
        self.assertEqual([t['state'] for t in zone['teams']], [1, 2, 3])
        self.assertEqual(current['defense'], (3, 5))

    def test_hidden_combat_zone_keeps_counter_without_inventing_defeats(self):
        zone = msg(3, msg(1, (1, 2, b'zone')), num(2, 5), num(4, 3))
        result = modes._arena_zones([zone], battle=True)[0]
        self.assertEqual(modes.arena_zone_summary(result), {'remaining':3,'defeated':None,'capacity':5})
        self.assertIsNone(result['placed'])

    def test_missing_combat_counter_stays_unknown(self):
        zone = msg(3, msg(1, (1, 2, b'zone')), num(2, 5))
        result = modes._arena_zones([zone], battle=True)[0]
        self.assertIsNone(modes.arena_zone_summary(result)['remaining'])

    def test_unknown_team_is_not_marked_defeated_from_aggregate(self):
        zone = {'placed':2,'capacity':2,'defeated':1,'teams':[{'state':1},{'state':None}]}
        self.assertEqual(modes.arena_zone_summary(zone)['remaining'],1)
        self.assertEqual(modes.arena_team_status(zone['teams'][1])['label'],'Статус неизвестен')

    def test_holds_exclude_current_and_winning_attempts(self):
        for state, raw, expected in ((1,2,2),(2,2,1),(3,2,1),(3,0,0),(2,None,0)):
            with self.subTest(state=state, raw=raw):
                self.assertEqual(modes.arena_team_status({'state':state,'defends':raw})['holds'],expected)

    def test_death_changes_actual_map_and_does_not_mutate_portrait(self):
        portrait = Image.new('RGBA',(64,64),'#c1ba91')
        pristine = portrait.copy()
        zone = {'placed':1,'capacity':1,'defeated':0,'teams':[{'units':['VADER'],'state':1,'defends':0}]}
        match = {'home':[zone], 'away':[], 'round':1}
        with patch.object(image,'_load_icon',return_value=portrait):
            alive = Image.open(image.render(match)).convert('RGB')
            dead_match = copy.deepcopy(match)
            dead_match['home'][0]['teams'][0]['state'] = 3
            dead_match['home'][0]['defeated'] = 1
            dead = Image.open(image.render(dead_match)).convert('RGB')
        self.assertIsNotNone(ImageChops.difference(alive,dead).getbbox())
        self.assertIsNone(ImageChops.difference(pristine,portrait).getbbox())
        # Red crossed portrait at its exact location, independent of text rendering.
        self.assertEqual(dead.getpixel((384,238)), (250,117,129))

    def test_more_than_ten_squads_are_rendered_and_rows_grow(self):
        teams = [{'units':['VADER'],'state':3,'defends':1} for _ in range(13)]
        zone = {'placed':13,'capacity':13,'defeated':13,'teams':teams}
        with patch.object(image,'_load_icon',return_value=None):
            canvas = Image.open(image.render({'home':[zone],'away':[]}))
        self.assertGreater(canvas.height,500)
        self.assertEqual(canvas.getpixel((384,526)), (250,117,129))

    def test_no_board_returns_none(self):
        self.assertIsNone(image.render({'home':[],'away':[]}))
