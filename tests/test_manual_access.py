import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import database
import guild_resolver
from web.deps import _refresh_access


class ManualAccessTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_patch = patch.object(database, "DB_NAME", str(Path(self.tmp.name) / "access.db"))
        self.db_patch.start()
        database.init_db()
        self.guild = self.make_guild("Home")
        self.other = self.make_guild("Other")

    def tearDown(self):
        self.db_patch.stop()
        self.tmp.cleanup()

    def make_guild(self, name):
        return database.create_guild(name=name, ally_code="123456789", discord_guild_id=name,
                                     member_role_id="", officer_role_id="")

    def register(self, level, guild=None, code="123456789"):
        guild = guild or self.guild
        database.sync_guild_roster(guild, [(code, code, "Player", level, "player-id")])
        database.set_user_registration("player", code, "Player", guild_id=guild)

    def grant(self, tier="officer", guild=None, code="123456789"):
        database.add_manual_grant("player", code, guild or self.guild, tier)

    def test_officer_grant_promotes_registered_member_in_bot_and_web(self):
        self.register(2)
        self.grant()
        self.assertTrue(guild_resolver.is_officer_for_resolved_guild("player"))
        user = _refresh_access({"discord_id": "player", "tier": "member"})
        self.assertEqual((user["tier"], user["guild_id"]), ("officer", self.guild))
        self.assertEqual(database.get_member_level(self.guild, "123456789"), 2)

    def test_revoking_grant_restores_member_even_with_officer_session(self):
        self.register(2)
        self.grant()
        database.remove_manual_grant("player")
        self.assertEqual(_refresh_access({"discord_id": "player", "tier": "officer"})["tier"], "member")

    def test_member_grant_does_not_demote_game_officer(self):
        self.register(3)
        self.grant("member")
        self.assertEqual(guild_resolver.resolve_tier("player"), "officer")

    def test_external_player_can_still_use_grant(self):
        self.grant()
        self.assertEqual(guild_resolver.resolve_access("player"), {
            "tier": "officer", "guild_id": self.guild,
            "ally_code": "123456789", "is_super_admin": False,
        })

    def test_higher_grant_keeps_its_own_guild_and_account(self):
        self.register(2)
        self.grant(guild=self.other, code="223456789")
        access = guild_resolver.resolve_access("player")
        self.assertEqual((access["tier"], access["guild_id"], access["ally_code"]),
                         ("officer", self.other, "223456789"))

    def test_equal_grant_preserves_existing_officer_guild(self):
        self.register(3)
        self.grant(guild=self.other, code="223456789")
        access = guild_resolver.resolve_access("player")
        self.assertEqual((access["guild_id"], access["ally_code"]), (self.guild, "123456789"))

    def test_member_grant_does_not_demote_super_admin(self):
        self.register(2)
        self.grant("member")
        database.add_bot_admin("player")
        access = guild_resolver.resolve_access("player")
        self.assertTrue(access["is_super_admin"])
        self.assertEqual(access["tier"], "officer")
