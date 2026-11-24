import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from cogs import personal_game_modes as cog
import disnake


class ArenaPresentationTests(unittest.TestCase):
    def test_live_report_uses_compact_aligned_score_instead_of_inline_field_grid(self):
        # The live comparison is match 3 in attack phase 2, not "round 2".
        state = {"joined": True, "rating": 3067, "league": "Кайбер", "division": 10,
                 "match_end": "25.11 01:00 МСК", "matches": [{
                     "current": True, "number": 3, "round": 2, "round_end": "25.11 01:00 МСК",
                     "opponent": "tsuner", "defense": (18, 18), "home": [], "away": [],
                     "score": {"home": 0, "away": 0},
                     "maximum": {"home": {"full": 2085, "undersized": 2133},
                                 "away": {"full": 2085, "undersized": 2133}},
                 }]}
        embed = disnake.Embed(title="Великая арена · 881789523")
        cog._arena(embed, state)
        self.assertLessEqual(len(embed.fields), 3)
        self.assertTrue(all(not field.inline for field in embed.fields))
        text = str(embed.to_dict())
        self.assertIn("2085", text)
        self.assertIn("2133", text)
        self.assertNotIn("Зона 1", text)


class PersonalGameModeVisibilityTests(unittest.IsolatedAsyncioTestCase):
    async def test_arena_response_is_public_and_conquest_stays_private(self):
        for mode, private in (("arena", False), ("conquest", True)):
            with self.subTest(mode=mode):
                inter = SimpleNamespace(
                    author=SimpleNamespace(id=123),
                    response=SimpleNamespace(is_done=Mock(return_value=False), defer=AsyncMock()),
                    edit_original_response=AsyncMock(),
                )
                with patch.object(cog, "_scope", return_value=("123", 1, "test", "member")), \
                     patch.object(cog.game_account, "select_account", return_value="123456789"), \
                     patch.object(cog.game_account_readonly, "read", new=AsyncMock(return_value=None)):
                    await cog.PersonalGameModesCog()._report(inter, mode, None)
                inter.response.defer.assert_awaited_once_with(ephemeral=private)

    async def test_public_controls_are_still_owner_only(self):
        view = cog.PersonalReportView(Mock(), 123, "arena", "123456789")
        self.assertTrue(await view.interaction_check(SimpleNamespace(author=SimpleNamespace(id=123))))
        self.assertFalse(await view.interaction_check(SimpleNamespace(author=SimpleNamespace(id=456))))


if __name__ == "__main__":
    unittest.main()
