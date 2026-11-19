import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import database
from cogs.guild_units import GuildUnitsCog, ReadinessSessionView
from services import game_account, unit_shards, guild_units_image


def variant(stars, promotion="", base="UNIT", creation="create"):
    return {"id": f"{base}:{stars}", "baseId": base, "rarity": stars,
            "obtainable": True, "obtainableTime": "0",
            "promotionRecipeReference": promotion, "creationRecipeReference": creation}


class ShardCalculationTests(unittest.TestCase):
    def test_catalog_uses_current_rarity_recipe_and_creation_result(self):
        units = [variant(5, "five"), variant(6, "six"), variant(7),
                 dict(variant(6, "wrong"), obtainable=False)]
        recipes = [
            {"id": "create", "ingredients": [{"id": "unitshard_UNIT", "minQuantity": 145}],
             "result": {"id": "UNIT:5"}},
            {"id": "five", "ingredients": [{"id": "UNIT_SHARD_MATERIAL", "minQuantity": 85}]},
            {"id": "six", "ingredients": [{"id": "UNIT_SHARD_MATERIAL", "minQuantity": 100}]},
            {"id": "wrong", "ingredients": [{"id": "UNIT_SHARD_MATERIAL", "minQuantity": 999}]},
        ]
        data = unit_shards.build_catalog(units, recipes)["UNIT"]
        self.assertEqual(data["creation"], {"stars": 5, "count": 145})
        self.assertEqual(data["promotion"]["6"], {"stars": 7, "count": 100})
        self.assertEqual(data["promotion"]["5"]["count"], 85)

    def test_progress_handles_unlocked_locked_max_surplus_and_unknown(self):
        report = {"units": [{"base_id": code} for code in ("UNIT", "LOCKED", "MAX", "READY", "UNKNOWN")]}
        snapshot = {"farm": {"stars": {"UNIT": 6, "MAX": 7, "READY": 5},
                             "stock": {"unitshard_UNIT": 42, "unitshard_READY": 150}}}
        recipes = {"UNIT": {"promotion": {"6": {"stars": 7, "count": 100}}},
                   "LOCKED": {"creation": {"stars": 4, "count": 80}},
                   "READY": {"promotion": {"5": {"stars": 6, "count": 85}}}}
        unit_shards.add_progress(report, snapshot, recipes)
        rows = [u["shards"] for u in report["units"]]
        self.assertEqual((rows[0]["available"], rows[0]["remaining"]), (42, 58))
        self.assertEqual((rows[1]["available"], rows[1]["target"], rows[1]["unlock"]), (0, 4, True))
        self.assertEqual(rows[2], {"max": True})
        self.assertEqual(rows[3]["remaining"], 0)
        self.assertIsNone(rows[4]["needed"])
        with self.assertRaises(game_account.AccountError):
            unit_shards.add_progress(report, {"farm": None}, recipes)

    def test_private_roster_uses_fresh_stars_relics_and_includes_ships_without_gear(self):
        roster = unit_shards.private_roster({"farm": {"stars": {"UNIT": 6, "SHIP": 5},
            "units": {"UNIT": ("UNIT", 13, 5, {})}, "stock": {}}})
        self.assertEqual(roster["UNIT"]["relic"]["currentTier"], 7)
        self.assertEqual(roster["SHIP"]["currentRarity"], 5)


class ShardCommandTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.inter = SimpleNamespace(author=SimpleNamespace(id=123),
            response=SimpleNamespace(defer=AsyncMock(), send_message=AsyncMock()), edit_original_response=AsyncMock())
        self.cog = GuildUnitsCog(SimpleNamespace(comlink=Mock()))
        self.player = {"name": "Player", "allyCode": "123456789"}
        self.context = ("123", 1, "test", "123456789")

    async def call(self, shards=True, member=None, code=None, guild=False):
        await GuildUnitsCog.readiness.callback(self.cog, self.inter, "GUIDE", member, code, False, guild, shards)

    async def test_guild_and_other_discord_user_rejected_before_private_access(self):
        with patch("guild_resolver.resolve_guild_id", return_value=1), \
             patch.object(game_account, "require_account_read") as gate, \
             patch("services.game_account_readonly.read", AsyncMock()) as read:
            await self.call(guild=True)
            await self.call(member=SimpleNamespace(id=999))
        gate.assert_not_called()
        read.assert_not_awaited()
        self.inter.response.defer.assert_awaited_with(ephemeral=False)

    async def test_foreign_ally_code_rejected_before_roster_or_private_read(self):
        with patch("guild_resolver.resolve_guild_id", return_value=1), \
             patch.object(game_account, "require_account_read"), \
             patch("guild_resolver.target_player", return_value=("987654321", None, None)), \
             patch.object(game_account, "select_account", side_effect=game_account.AccountError("Только свой аккаунт")), \
             patch.object(self.cog, "_player_roster", AsyncMock()) as roster, \
             patch("services.game_account_readonly.read", AsyncMock()) as read:
            await self.call(code="987654321")
        roster.assert_not_awaited()
        read.assert_not_awaited()

    async def test_default_has_no_ea_read(self):
        with patch("guild_resolver.resolve_guild_id", return_value=1), \
             patch.object(database, "get_journey_guide_requirements", return_value=[("A", "stars", "7")]), \
             patch.object(self.cog, "_player_roster", AsyncMock(return_value=(self.player, {}, "fresh"))), \
             patch.object(self.cog, "_send_readiness", AsyncMock()) as send, \
             patch.object(game_account, "require_account_read") as gate:
            # Omit the option, including direct callback callers in existing tests.
            await GuildUnitsCog.readiness.callback(self.cog, self.inter, "GUIDE", None, None, False, False)
        gate.assert_not_called()
        self.assertIsNone(send.call_args.args[-1])
        self.inter.response.defer.assert_awaited_with(ephemeral=False)

    async def test_own_alt_keeps_selected_account(self):
        with patch("guild_resolver.resolve_guild_id", return_value=1), \
             patch.object(game_account, "require_account_read"), \
             patch("guild_resolver.target_player", return_value=(None, None, None)), \
             patch("services.public_checks.account", return_value={"ally_code": "222333444"}) as account, \
             patch.object(game_account, "select_account", return_value="222333444") as select, \
             patch.object(database, "get_journey_guide_requirements", return_value=[("A", "stars", "7")]), \
             patch.object(self.cog, "_player_roster", AsyncMock(return_value=(self.player, {}, "fresh"))), \
             patch.object(self.cog, "_send_readiness", AsyncMock()) as send:
            await GuildUnitsCog.readiness.callback(self.cog, self.inter, "GUIDE", None, None, True, False, True)
        account.assert_called_once_with(self.inter.author, alt=True)
        select.assert_called_once_with("123", 1, ally_code="222333444")
        self.assertEqual(send.call_args.args[-1][-1], "222333444")
        self.inter.response.defer.assert_awaited_with(ephemeral=False)

    async def test_connected_owner_reads_and_renders_private_progress(self):
        snapshot = {"read_at": 1795070000, "farm": {"stars": {"UNIT": 6},
            "units": {"UNIT": ("UNIT", 12, 0, {})}, "stock": {"unitshard_UNIT": 42}}}
        report = {"guide": {"name": "Guide"}, "units": [{"base_id": "UNIT", "status": "missing"}]}
        with patch("services.game_account_readonly.read", AsyncMock(return_value=snapshot)) as read, \
             patch.object(unit_shards, "catalog", AsyncMock(return_value={"UNIT": {"promotion": {"6": {"stars": 7, "count": 100}}}})), \
             patch("services.guild_units.player_readiness", return_value=report), \
             patch.object(guild_units_image, "render_player", return_value=b"png"):
            await self.cog._send_readiness(self.inter, 1, "GUIDE", self.player, {}, "old", self.context)
        read.assert_awaited_once_with("123", 1, "test", None, "123456789", member_read=True)
        self.assertEqual(report["units"][0]["shards"]["remaining"], 58)

    async def test_missing_connection_and_expired_session_offer_next_action(self):
        with patch("services.game_account_readonly.read", AsyncMock(side_effect=game_account.ConnectionRequired)), \
             patch.object(game_account, "link_path", return_value="/game-account/link?alt=1"):
            await self.cog._send_readiness(self.inter, 1, "GUIDE", self.player, {}, "", self.context)
        view = self.inter.edit_original_response.call_args.kwargs["view"]
        self.assertIn("alt=1", view.children[0].url)
        with patch("services.game_account_readonly.read", AsyncMock(side_effect=game_account.SessionExpired)), \
             patch.object(game_account, "reconnect", AsyncMock()) as login:
            await self.cog._send_readiness(self.inter, 1, "GUIDE", self.player, {}, "", self.context)
        login.assert_not_awaited()
        self.assertIsInstance(self.inter.edit_original_response.call_args.kwargs["view"], ReadinessSessionView)

    async def test_session_confirmation_rejects_another_user(self):
        view = ReadinessSessionView(self.cog, self.context, "GUIDE", self.player)
        self.inter.author.id = 999
        self.assertFalse(await view.interaction_check(self.inter))
        self.inter.author.id = 123
        self.assertTrue(await view.interaction_check(self.inter))
