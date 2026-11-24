import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from cogs import personal_game_modes as cog


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
