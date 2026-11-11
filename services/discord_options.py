"""Localized choices for Discord slash-command switches."""

from disnake.ext import commands


def _yes_no(inter, value: int) -> bool:
    return bool(value)


def yes_no_param(**kwargs):
    """Keep Python booleans while Discord displays Да/Нет choices."""
    return commands.Param(choices={"Да": 1, "Нет": 0}, converter=_yes_no, **kwargs)
