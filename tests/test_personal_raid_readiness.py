import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import database
import guild_resolver
from cogs.raids import RaidsCog, autocomplete_raid_teams
from services import raids, public_checks
from tg_bot import core, more


def unit(relic=7, stars=7, gear=13):
    return {"definitionId": "A:SEVEN_STAR", "currentRarity": stars, "currentTier": gear,
            "relic": {"currentTier": relic + 2}}


class PersonalRaidReadinessTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = patch.object(database, "DB_NAME", str(Path(self.tmp.name) / "test.db"))
        self.db.start()
        database.init_db()
        database.set_bot_state(raids.RAID_UNITS_STATE, json.dumps({
            "order66": {"units": ["A", "B", "C"], "min_stars": 5}}), guild_id=0)
        self.teams = patch.dict(raids.RAID_TEAMS, {"order66": [("Первый", ["A", "B"]), ("Второй", ["C"])]})
        self.teams.start()
        self.author = SimpleNamespace(id=123)
        self.inter = SimpleNamespace(author=self.author, response=SimpleNamespace(defer=AsyncMock()),
                                     edit_original_response=AsyncMock(), delete_original_response=AsyncMock(),
                                     followup=SimpleNamespace(send=AsyncMock()))
        database.set_user_registration("123", "123456789", "Player", guild_id=0)
        self.cards = SimpleNamespace(_card_players=AsyncMock(return_value=([
            {"name": "Player", "rosterUnit": [unit()]}], [None])))
        self.cog = object.__new__(RaidsCog)
        self.cog.bot = SimpleNamespace(get_cog=lambda _: self.cards)

    def tearDown(self):
        self.teams.stop()
        self.db.stop()
        self.tmp.cleanup()

    async def run_command(self, relic=7, raid="order66", team=None, alt=False, guild=False):
        await RaidsCog.raid_readiness.callback(self.cog, self.inter, relic, raid, team, alt, guild)

    def test_personal_statuses_respect_stars_relic_offset_and_missing_units(self):
        plan = raids.readiness_requirements("order66", 7)
        for roster, expected in (({"A": unit()}, "met"), ({"A": unit(6)}, "short"),
                                 ({"A": unit(stars=6)}, "missing"), ({}, "missing")):
            report = raids.personal_readiness(plan, roster)
            self.assertEqual(report["units"][0]["status"], expected)
            self.assertEqual([u["base_id"] for u in report["units"]], ["A", "B", "C"])
            self.assertIsNone(report["guide_level"])

    def test_g13_mode_and_team_filter_use_same_requirements(self):
        plan = raids.readiness_requirements("order66", 0, "Первый")
        self.assertEqual(list(plan["requirements"]), ["A", "B"])
        self.assertIn(("gear", "13"), plan["requirements"]["A"])
        self.assertEqual(raids.personal_readiness(plan, {"A": unit(0)})["units"][0]["status"], "met")
        self.assertEqual(raids.personal_readiness(plan, {"A": unit(0, gear=12)})["units"][0]["status"], "short")

    def test_public_defaults_do_not_read_any_guild_history(self):
        with patch.object(database, "get_raid_results") as history:
            plan = raids.readiness_requirements()
        history.assert_not_called()
        self.assertEqual(plan["raid_id"], "order66")

    def test_unknown_raid_team_and_relic_are_rejected(self):
        for kwargs in ({"raid": "nonexistent"}, {"raid": "order66", "team": "неизвестный"},
                       {"raid": "order66", "relic": 11}, {"raid": "order66", "team": "р"}):
            with self.assertRaises(ValueError):
                raids.readiness_requirements(**kwargs)

    def test_preview_names_order_and_notice_are_kept(self):
        plan = raids.readiness_requirements("droiddestruction")
        report = raids.personal_readiness(plan, {})
        self.assertEqual([u["base_id"] for u in report["units"]], raids.PREVIEW_RAIDS["droiddestruction"]["units"])
        self.assertTrue(plan["entry"]["preview"])
        self.assertEqual(next(u["name"] for u in report["units"] if u["base_id"] == "GLMANDO"), "Мандалорец и Грогу")

    async def test_discord_personal_renders_without_guild_feature_or_rosters(self):
        with patch("services.guild_units_image.render_player", return_value=b"png") as render, \
             patch.object(guild_resolver, "require_feature", AsyncMock()) as feature, \
             patch("services.guild_units.readiness_grid") as grid:
            await self.run_command()
        feature.assert_not_awaited()
        grid.assert_not_called()
        self.cards._card_players.assert_awaited_once_with(["123456789"], None, False)
        self.assertIn("подготовлено 1 из 3", self.inter.edit_original_response.call_args.kwargs["content"])
        self.assertEqual(render.call_args.args[0]["units"][0]["status"], "met")

    async def test_discord_alt_uses_second_registered_account(self):
        database.set_user_registration("123", "987654321", "Alt", is_main=False, guild_id=0)
        with patch("services.guild_units_image.render_player", return_value=b"png"):
            await self.run_command(alt=True)
        self.assertEqual(self.cards._card_players.call_args.args[0], ["987654321"])

    async def test_discord_guild_mode_is_rejected_before_reading_rosters(self):
        with patch("services.guild_units.readiness_grid") as grid, \
             patch.object(guild_resolver, "require_feature", AsyncMock()) as feature:
            await self.run_command(guild=True)
        grid.assert_not_called()
        feature.assert_not_awaited()
        self.assertIn("офицерам", self.inter.followup.send.call_args.args[0])

    async def test_discord_officer_guild_mode_preserves_feature_gate_and_grid(self):
        with patch.object(guild_resolver, "is_officer_for_resolved_guild", return_value=True), \
             patch.object(guild_resolver, "require_feature", AsyncMock(return_value=5)) as feature, \
             patch("services.guild_units.readiness_grid", return_value={"units": [], "players": [{"ready": True}]}) as grid, \
             patch("services.guild_units_image.render", return_value=b"png"):
            await self.run_command(guild=True)
        feature.assert_awaited_once_with(self.inter, "raids")
        self.assertEqual(grid.call_args.args[0], 5)
        self.cards._card_players.assert_not_awaited()

    async def test_discord_guild_mode_still_respects_disabled_feature(self):
        with patch.object(guild_resolver, "is_officer_for_resolved_guild", return_value=True), \
             patch.object(guild_resolver, "require_feature", AsyncMock(return_value=None)), \
             patch("services.guild_units.readiness_grid") as grid:
            await self.run_command(guild=True)
        grid.assert_not_called()

    async def test_discord_own_alt_is_not_a_guild_option(self):
        with patch("services.guild_units.readiness_grid") as grid:
            await self.run_command(alt=True, guild=True)
        grid.assert_not_called()
        self.assertIn("личная", self.inter.followup.send.call_args.args[0])

    async def test_autocomplete_teams_works_without_connected_guild(self):
        inter = SimpleNamespace(author=self.author, filled_options={})
        options = await autocomplete_raid_teams(inter, "")
        self.assertEqual([o.value for o in options], ["Первый", "Второй"])

    async def test_telegram_command_renders_shared_personal_report_and_blocks_guild_mode(self):
        ctx = SimpleNamespace(owner_id="123", access={"tier": None, "guild_id": None},
                              args="Приказ 66 r0 | Первый", action=AsyncMock(), reply=AsyncMock(), reply_photo=AsyncMock())
        self.assertEqual(core.COMMANDS["raidready"].tier, core.TIER_REGISTERED)
        self.assertIsNone(core.denied_text(core.TIER_REGISTERED, ctx))
        with patch.object(more, "_player_roster", AsyncMock(return_value=({"name": "Player"}, {"A": unit(0)}, "fresh"))), \
             patch("services.guild_units_image.render_player", return_value=b"png"):
            await more.cmd_raidready(ctx)
        self.assertIn("G13", ctx.reply_photo.call_args.kwargs["caption"])
        self.assertIn("подготовлено 1 из 2", ctx.reply_photo.call_args.kwargs["caption"])
        ctx.args = "Приказ 66 гильдия"
        with patch.object(more, "_player_roster", AsyncMock()) as roster:
            await more.cmd_raidready(ctx)
        roster.assert_not_awaited()

    async def test_telegram_invalid_relic_and_missing_alt_do_not_fetch_player(self):
        ctx = SimpleNamespace(owner_id="123", access={"tier": None, "guild_id": None},
                              action=AsyncMock(), reply=AsyncMock())
        for args in ("Приказ 66 r11", "Приказ 66 r0 r7", "Приказ 66 альт"):
            ctx.args = args
            with patch.object(more, "_player_roster", AsyncMock()) as roster:
                await more.cmd_raidready(ctx)
            roster.assert_not_awaited()
