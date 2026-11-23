import unittest

from services import tw_score


def _war():
    def zones():
        return [{"id": f"tw_jakku01_phase0{column}_conflict0{lane}", "status": 1, "capacity": 2,
                 "points": 68 if column >= 3 and lane == 1 else 60,
                 "remaining": 2, "deployed": 2, "defeated": 0}
                for column in range(1, 5) for lane in range(1, 3 if column < 3 else 4)]
    return {"round": 1, "own": zones(), "opponent": zones()}


class WarScoreTests(unittest.TestCase):
    def test_first_two_failed_attacks_limit_potential_on_the_correct_side(self):
        war = _war()
        war["opponent"][4].update(status=3, teams=[{"status": 2, "holds": 2}, {"status": 1, "holds": 4}])
        home, away = tw_score.analysis(war)
        self.assertEqual((away["current"], away["potential"], away["maximum"]), (616, 6458, 7074))
        self.assertEqual((home["current"], home["potential"], home["maximum"]), (616, 6443, 7059))
        self.assertEqual((home["zones"][4]["holds"], home["zones"][4]["lost"]), (5, 15))

    def test_capture_and_vacancy_do_not_add_to_available_banners(self):
        war = _war()
        war["own"][7]["points"] += 920 + 22
        war["opponent"][7].update(status=4, defeated=1, remaining=0, deployed=1,
                                  teams=[{"status": 3, "holds": 1}])
        home, away = tw_score.analysis(war)
        captured = home["zones"][7]
        self.assertEqual((captured["available"], captured["lost"], captured["first"]), (0, 22, 100))
        self.assertEqual(home["maximum"], 7052)
        self.assertEqual(away["maximum"], 7074)

    def test_unknown_counter_keeps_the_current_score_but_hides_the_ceiling(self):
        war = _war()
        war["opponent"][7].update(remaining=None, defeated=None)
        home, _ = tw_score.analysis(war)
        self.assertEqual(home["current"], 616)
        self.assertIsNone(home["potential"])
        self.assertIsNone(home["maximum"])

    def test_placement_phase_does_not_show_an_attack_forecast(self):
        self.assertEqual(tw_score.analysis({"round": 0}), [])
