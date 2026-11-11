import unittest

import disnake
from disnake.ext import commands

from services.discord_options import yes_no_param


class YesNoTests(unittest.IsolatedAsyncioTestCase):
    async def test_choices_and_boolean_conversion(self):
        param = yes_no_param(default=False, description="Switch")

        @commands.slash_command()
        async def example(inter, enabled: bool = param):
            pass

        option = example.body.to_dict()["options"][0]
        self.assertEqual(option["type"], disnake.OptionType.integer.value)
        self.assertEqual(option["choices"], [
            {"name": "Да", "value": 1}, {"name": "Нет", "value": 0},
        ])
        self.assertFalse(option.get("required", False))
        self.assertIs(param.default, False)
        self.assertIs(await param.convert_argument(None, 1), True)
        self.assertIs(await param.convert_argument(None, 0), False)
