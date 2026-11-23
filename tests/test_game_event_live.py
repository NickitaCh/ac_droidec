import base64
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import AsyncMock, Mock, patch

from services import game_account as ga, game_account_readonly, game_event_live, game_personal_modes, guild_compare, tw_battle_log


def text(tag, value):
    return tag, 2, value.encode()


def number(tag, value):
    return tag, 0, value


def message(tag, *fields):
    return tag, 2, ga.pack(fields)


class EventReadTests(unittest.TestCase):
    def test_arena_retains_both_boards_and_distinguishes_unrevealed_squads(self):
        def zone(key, *teams):
            return message(3, message(1, text(1, key), number(2, 3)),
                           number(2, 2), number(4, len(teams)), *teams)

        home_team = message(3, message(4, message(1, text(2, "VADER:SEVEN_STAR"))))
        away_team = message(3, message(4, message(1, text(2, "TIEFIGHTERIMPERIAL:SEVEN_STAR"))))
        current = message(2, message(2, zone("home", home_team)), message(3, zone("away", away_team, message(3))),
                          number(8, 1))
        event = message(4, text(1, "arena"), number(3, 1), current)
        state = game_personal_modes.arena(ga.pack([event]))
        with patch.object(game_personal_modes.database, "get_game_unit_names", return_value={"VADER": "Дарт Вейдер"}):
            game_personal_modes.name_arena_units(state)
        match = state["matches"][0]
        self.assertTrue(match["current"])
        self.assertEqual(match["defense"], (1, 2))
        self.assertEqual(match["home"][0]["teams"][0]["names"], ["Дарт Вейдер"])
        self.assertEqual(match["away"][0]["teams"][0]["names"], ["TIEFIGHTERIMPERIAL"])
        self.assertEqual(match["away"][0]["teams"][1]["names"], [])

    def test_tw_retains_zero_and_distinguishes_missing_field(self):
        conflict = message(2, message(1, text(1, "zone"), number(3, 0)),
                           number(2, 25), number(4, 0))
        tw = message(3, text(2, "map"), number(3, 1), number(4, 2_000_000_000),
                     message(6, conflict))
        result = game_event_live.parse_tw(ga.pack([tw]))
        self.assertEqual(result["round"], 1)
        self.assertEqual(result["own"], [
            {"id": "zone", "status": None, "points": 0, "capacity": 25,
             "remaining": 0, "deployed": None, "defeated": None},
        ])
        self.assertIsNone(game_event_live.parse_tw(b""))

    def test_tw_combat_counts_remaining_and_defeated_separately(self):
        def team(status):
            return message(3, number(6, status), message(4, message(1, text(2, "VADER"))))

        def zone(key, remaining, *teams):
            return message(2, message(1, text(1, key)), number(2, 41), number(4, remaining), *teams)

        tw = message(3, number(3, 1), number(4, 2_000_000_000),
                     message(7, zone("open", 2, team(1), team(2), team(3)), zone("hidden", 41)))
        known, hidden = game_event_live.parse_tw(ga.pack([tw]))["opponent"]
        self.assertEqual((known["remaining"], known["deployed"], known["defeated"]), (2, 3, 1))
        self.assertEqual([row["status"] for row in known["teams"]], [1, 2, 3])
        self.assertEqual((hidden["remaining"], hidden["deployed"], hidden["defeated"]), (41, None, None))

    def test_tw_reads_revealed_teams_and_keeps_unrevealed_counts(self):
        squad = message(3, text(1, "player-id"), text(2, "Ник"), message(4, message(1, text(2, "GLREY:SEVEN_STAR")),
                                   message(1, text(2, "BENSOLO:SEVEN_STAR"))), number(8, 2))
        zone = message(2, message(1, text(1, "tw_jakku01_phase01_conflict01")),
                       number(2, 41), number(4, 19), squad)
        hidden = message(2, message(1, text(1, "tw_jakku01_phase01_conflict01")), number(2, 41), number(4, 30))
        war = message(3, number(3, 0), number(4, 2_000_000_000), message(6, zone), message(7, hidden))
        state = game_event_live.parse_tw(ga.pack([war]))
        self.assertEqual(state["own"][0]["teams"], [{"units": ["GLREY", "BENSOLO"], "holds": 2,
                                                      "player_name": "Ник", "player_id": "player-id"}])
        self.assertEqual(state["opponent"][0]["deployed"], 30)
        self.assertNotIn("teams", state["opponent"][0])

    def test_raid_distinguishes_active_from_results(self):
        player = message(23, text(1, "player"), number(2, 123), number(4, 1))
        raid = message(1, text(1, "raid"), number(2, 2_000_000_000_000), number(28, 123), player)
        result = game_event_live.parse_raid(ga.pack([raid, message(2)]))
        self.assertEqual(result["results"], 1)
        self.assertEqual(result["active"][0]["players"], [{"id": "player", "points": 123, "attempts": 1}])
        self.assertEqual(game_event_live.parse_raid(ga.pack([message(2)]))["active"], [])

    def test_tw_opponent_comes_from_away_profile_and_expires_with_round(self):
        war = message(3, text(1, "war-id"), number(3, 1), number(4, 2_000_000_000_000),
                      message(6, message(1, text(1, "own-id"), text(2, "Своя"))),
                      message(7, message(1, text(1, "away-id"), text(2, "Соперник"))))
        result = game_event_live.parse_tw(ga.pack([war]))
        self.assertEqual(result["opponent_guild"], {"id": "away-id", "name": "Соперник"})
        self.assertEqual(result["home_guild"]["id"], "own-id")
        self.assertEqual(result["opponent"], [])
        with patch.object(guild_compare.database, "get_guild_config", return_value={"swgoh_guild_id": "own-id"}), patch.object(
            guild_compare.database, "set_bot_state",
        ) as save, patch.object(guild_compare.time, "time", return_value=1_900_000_000) as clock:
            game_event_live.save(1, "owner", "tw", 1_900_000_000, result)
            key, raw = save.call_args.args
            self.assertEqual(key, guild_compare.STATE_CURRENT_OPPONENT)
            self.assertEqual(save.call_args.kwargs, {"guild_id": 1})
            with patch.object(guild_compare.database, "get_bot_state", return_value=raw):
                self.assertEqual(guild_compare.current_opponent(1)["id"], "away-id")
                clock.return_value = 2_000_000_000
                self.assertIsNone(guild_compare.current_opponent(1))
            save.reset_mock()
            guild_compare.save_current_opponent(1, {"id": "wrong"}, {"id": "away-id"}, "war-id", 2_000_000_010)
            save.assert_not_called()

    def test_tw_preview_flags_both_assignments_with_a_reused_unit(self):
        def assignment(code, zone, units):
            return {"ally_code": code, "zone": zone, "units": units, "squad_name": "Pack"}
        plan = {"excluded": []}
        rows = game_event_live.tw_preview(
            plan, [assignment("1", "x", ["A"]), assignment("1", "y", ["A", "B"]),
                   assignment("2", "x", ["C"])], "1", {"A"},
            {"x": {"label": "X"}, "y": {"label": "Y"}}, lambda _plan, _zone: 1,
        )
        self.assertEqual(len(rows), 2)
        self.assertTrue(all("Юнит повторяется в назначениях." in row["issues"] for row in rows))
        self.assertIn("Число назначений превышает вместимость зоны в плане.", rows[0]["issues"])
        self.assertIn("Часть состава отсутствует в игровом ростере.", rows[1]["issues"])

    def test_tw_preview_keeps_assigned_datacrons_and_flags_reuse_only_for_selected_player(self):
        assignments = [
            {"ally_code": code, "zone": "x", "units": [unit], "squad_name": unit, "datacron_id": cron}
            for code, unit, cron in [("1", "A", "first,second"), ("1", "B", "second"),
                                     ("1", "C", "third"), ("2", "D", "third")]
        ]
        rows = game_event_live.tw_preview(
            {}, assignments, "1", {"A", "B", "C"}, {"x": {"label": "X"}}, lambda *_: 5,
        )
        self.assertEqual([row["datacron_ids"] for row in rows], [["first", "second"], ["second"], ["third"]])
        issue = "Один датакрон назначен несколько раз."
        self.assertEqual([row["issues"] for row in rows], [[issue], [issue], []])

    def test_group_unpacks_nested_game_decoder_results(self):
        rows = [
            ("Энергия · 2", [("Обычная", [("Сейчас", 0)]), ("Флот", [("Сейчас", 10)])]),
            ("Энергия · всего записей", 105),
        ]
        self.assertEqual(game_account_readonly._group(rows, "Энергия"), [{"Сейчас": 0}, {"Сейчас": 10}])


class SuppliesDecodeTests(unittest.IsolatedAsyncioTestCase):
    async def test_player_and_store_get_an_independent_decode_budget(self):
        raw_response = ga.pack([text(1, "player"), message(5), message(2), message(18)])
        report, supplies = [], []
        decoder = Mock(side_effect=[[("inventory", [])], [("supplies", [])]])
        with patch.object(ga, "read_request", return_value=b"request"), patch.object(
            ga, "_read_payload", AsyncMock(return_value=raw_response),
        ), patch("services.game_read_decode.decode", decoder):
            await ga.read_initial(b"session", "player", report=report, supplies=supplies)
        self.assertEqual(report, [("inventory", [])])
        self.assertEqual(supplies, [("supplies", [])])
        self.assertEqual([tag for tag, _, _ in ga.fields(decoder.call_args_list[1].args[0])], [2, 18])


class SavedSessionTests(unittest.IsolatedAsyncioTestCase):
    def test_tw_pairs_a_retry_without_using_a_different_players_start(self):
        target = {"guild_id": "home", "zone": "north", "squad_id": "squad", "defender": "Defender"}
        events = [
            {**target, "id": "1", "at": 1, "attacker": "First", "outcome": "start"},
            {**target, "id": "2", "at": 2, "attacker": "Second", "outcome": "start"},
            {**target, "id": "3", "at": 3, "attacker": "Second", "outcome": "hold"},
            {**target, "id": "4", "at": 4, "attacker": "Second", "outcome": "start"},
            {**target, "id": "5", "at": 5, "attacker": "Second", "outcome": "win", "banners": 20},
        ]
        rows = tw_battle_log._attempts(events)
        self.assertEqual([(row["id"], row["outcome"], row.get("banners")) for row in rows],
                         [("4", "win", 20), ("2", "hold", None), ("1", "start", None)])

    async def test_tw_reads_the_activity_channel_and_deduplicates_observations(self):
        zone_id = "tw_jakku01_phase01_conflict01"
        war = ga.pack([message(3, text(1, "war"), number(3, 1), number(4, 2_000_000_000),
                               text(5, "channel"), message(6, message(1, text(1, "home"))),
                               message(7, message(1, text(1, "away")),
                                       message(2, message(1, text(1, zone_id), text(4, "channel")))))])

        def event(identifier, status, *, owner="defender", channel="channel"):
            key = "TERRITORY_CHANNEL_ACTIVITY_CONFLICT_FLEET_WIN" if status == 3 else "EMPTY"
            params = [message(2, text(3, "22"), number(4, 2))] if status == 3 else []
            zone = message(1, text(1, zone_id), message(4, text(1, key), *params),
                           text(7, "war"), text(12, "home"))
            squad = message(2, text(1, owner), text(2, "Defender"), number(6, status),
                            message(4, message(1, text(2, "VADER:SEVEN_STAR"))),
                            text(5, "different-squad" if identifier == "hold" else "squad"),
                            number(8, 1) if identifier == "hold" else number(8, 0))
            return message(1, text(1, identifier), text(3, channel), number(6, 1_800_000_000_000),
                           text(7, "attacker"), text(8, "Attacker"),
                           message(10, text(5, "TERRITORY_WAR_CONFLICT_ACTIVITY"),
                                   message(6, zone, squad)))

        log = ga.pack([event("win", 3), event("start", 2), event("hold", 1), event("deploy", 1, owner="attacker"),
                       event("foreign", 3, channel="other"), event("win", 3)])
        request = ga.pack([text(4, "PlayerRpc"), text(5, "GetInitialData"), text(6, ""),
                           text(7, "player"), text(8, "session")])
        session = ga.pack([text(1, "channel-player"), text(2, "channel-token")])
        with patch.object(ga, "_read_payload", AsyncMock(side_effect=[war, session, log])) as read:
            result = await game_account_readonly._read_event(request, "player", "tw", lambda: True)
        args = read.call_args.args
        self.assertEqual(args[1:], ("ChannelRpc", "GetEvents"))
        self.assertEqual(read.await_args.kwargs["endpoint"], tw_battle_log.ENDPOINT)
        self.assertEqual(ga._one(ga.fields(args[0]), 8, 2), b"channel-token")
        payload = ga.fields(ga._one(ga.fields(args[0]), 6, 2))
        channel = ga.fields(ga._one(payload, 4, 2))
        self.assertEqual(channel, [text(1, "channel"), number(5, 100)])
        self.assertEqual([(row["id"], row["outcome"]) for row in result["battles"]],
                         [("win", "win"), ("start", "start"), ("hold", "hold")])
        self.assertEqual((result["battles"][0]["attacker"], result["battles"][0]["defender"],
                          result["battles"][0]["leader"]), ("Attacker", "Defender", "VADER"))
        with TemporaryDirectory() as temp, patch.object(tw_battle_log.database, "DB_NAME", str(Path(temp) / "db.sqlite")):
            tw_battle_log.record(1, result, 100)
            tw_battle_log.record(1, {**result, "battles": [result["battles"][0]]}, 101)
            self.assertEqual(len(tw_battle_log.latest(1)["rows"]), 3)
            self.assertFalse(tw_battle_log.latest(2))
            with patch.object(tw_battle_log.database, "get_game_unit_names", return_value={"VADER": "Vader"}):
                log = tw_battle_log.latest(1)
                zone = next(key for key in tw_battle_log.zone_options(log)
                            if tw_battle_log.display_rows(log, zone=key))
                battles = tw_battle_log.display_rows(log, zone=zone)
                self.assertEqual(len(battles), 2)
                self.assertEqual({row["result"] for row in battles}, {"Победа · +22 стяг.", "Защита устояла"})
                victory = next(row for row in battles if row["outcome"] == "win")
                self.assertEqual((victory["id"], victory["banners"]), ("start", 22))

    async def test_expired_session_uses_only_one_allowlisted_read(self):
        request = ga.pack([text(4, "PlayerRpc"), text(5, "GetInitialData"), text(6, ""),
                           text(7, "player"), text(8, "session")])
        cipher = Mock()
        cipher.decrypt.return_value = json.dumps({
            "expected_id": "player", "read_request": base64.b64encode(request).decode(),
        }).encode()
        expired = AsyncMock(side_effect=ga.SessionExpired("Expired"))
        with patch.object(ga, "require_pilot"), patch.object(ga, "select_account", return_value="123"), patch.object(
            game_account_readonly, "_connection", return_value=("target", "player", b"encrypted"),
        ), patch.object(ga, "_cipher", return_value=cipher), patch.object(ga, "_read_payload", expired):
            with self.assertRaises(ga.SessionExpired):
                await game_account_readonly.read("owner", 1, "test", "officer", "123", event="tw")
        expired.assert_awaited_once()
        body, service, method = expired.await_args.args
        self.assertEqual((service, method), ("TerritoryRpc", "GetTerritoryMapStatuses"))
        self.assertEqual(ga.fields(ga._one(ga.fields(body), 6, 2)), [(1, 0, 8)])


if __name__ == "__main__":
    unittest.main()
