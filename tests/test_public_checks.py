import ast
import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from disnake.ext import commands

import database
import guild_resolver
from cogs.guild_units import GuildUnitsCog
from cogs.unit_test import UnitTestCog
from cogs.profile import ProfileCog
from services import public_checks, registration
from tg_bot import core, handlers, more


class PublicChecksTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = patch.object(database, "DB_NAME", str(Path(self.tmp.name) / "test.db"))
        self.db.start()
        database.init_db()
        self.comlink = Mock(get_player=Mock(return_value={"name": "Player", "guildId": "foreign"}))
        self.author = SimpleNamespace(id=123)

    def tearDown(self):
        self.db.stop()
        self.tmp.cleanup()

    async def register(self, code="123456789", **kwargs):
        return await registration.register_player(self.comlink, "123", code, **kwargs)

    async def test_registration_without_connected_guild_never_grants_member_access(self):
        for guild in ("foreign", None):
            self.comlink.get_player.return_value = {"name": "Player", "guildId": guild}
            result = await self.register()
            self.assertTrue(result.ok, result.error)
            self.assertEqual(result.guild_id, 0)
            self.assertTrue(public_checks.registered(self.author))
            self.assertIsNone(guild_resolver.resolve_tier(self.author))
            self.assertIsNone(guild_resolver.resolve_guild_id(self.author))

    async def test_main_alt_and_unregistration_work_without_guild_access(self):
        await self.register()
        await self.register("987654321", is_alt=True)
        cog = UnitTestCog(SimpleNamespace())
        inter = SimpleNamespace(author=self.author)
        self.assertEqual(cog.own_code(inter, None, False), ("123456789", None))
        self.assertEqual(cog.own_code(inter, None, True), ("987654321", None))
        result = await registration.unregister_player("123", 0, "123456789")
        self.assertTrue(result.ok)
        self.assertEqual(public_checks.account(self.author)["ally_code"], "987654321")

    async def test_code_cannot_be_claimed_from_another_owner_or_scope(self):
        database.set_user_registration("456", "123456789", guild_id=5)
        result = await self.register()
        self.assertFalse(result.ok)
        self.assertEqual(database.get_ally_code_registrations("123456789"), [(5, "456")])

    async def test_officer_cannot_register_external_player_for_someone(self):
        result = await self.register(officer_guild_id=5)
        self.assertFalse(result.ok)
        self.assertFalse(public_checks.registered(self.author))

    async def test_public_registration_moves_into_connected_guild_on_roster_sync(self):
        await self.register()
        database.move_registrations_to_guild(5, {"123456789"})
        self.assertEqual(database.get_ally_code_registrations("123456789"), [(5, "123")])

    async def test_reregistration_moves_own_code_without_leaving_public_duplicate(self):
        await self.register()
        with patch.object(database, "get_guild_config_by_swgoh_id", return_value={"id": 5}):
            result = await self.register()
        self.assertTrue(result.ok)
        self.assertEqual(database.get_ally_code_registrations("123456789"), [(5, "123")])

    async def test_discord_gate_opens_only_the_explicit_personal_commands(self):
        # Exercise the real global gate without constructing/running a Discord bot.
        tree = ast.parse(Path("main.py").read_text(encoding="utf-8"))
        node = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "_check_access")
        constants = {n.targets[0].id: ast.literal_eval(n.value) for n in tree.body
                     if isinstance(n, ast.Assign) and isinstance(n.targets[0], ast.Name)
                     and n.targets[0].id in ("ALWAYS_ALLOWED_COMMANDS", "MEMBER_ACCESSIBLE_COMMANDS")}
        env = {**constants, "bot": SimpleNamespace(allowed_user_ids=[]), "guild_resolver": guild_resolver,
               "public_checks": public_checks, "commands": commands}
        exec(compile(ast.Module(body=[node], type_ignores=[]), "main.py", "exec"), env)
        check = env["_check_access"]
        with self.assertRaises(commands.CheckFailure):
            check(self.author, "юнит")
        await self.register()
        for cmd in ("юнит", "юнит_гильдия", "готовность", "фракция", "профиль", "рейд готовность", "регистрация_отмена"):
            self.assertTrue(check(self.author, cmd))
        for cmd in ("статы", "нарушения", "тб_отчет", "тб оз_готовность", "рейд итоги", "рейд игрок"):
            with self.assertRaises(commands.CheckFailure, msg=cmd):
                check(self.author, cmd)

    async def test_discord_readiness_rejects_guild_mode_before_reading_rosters(self):
        await self.register()
        inter = SimpleNamespace(author=self.author, response=SimpleNamespace(defer=AsyncMock()),
                                edit_original_response=AsyncMock())
        with patch.object(database, "get_journey_guide_requirements") as reqs:
            await GuildUnitsCog.readiness.callback(GuildUnitsCog(None), inter, "GUIDE",
                                                   None, None, False, True)
        reqs.assert_not_called()
        self.assertIn("подключённых", inter.edit_original_response.call_args.args[0])

    async def test_public_guildunit_skips_failed_players_and_caches_report(self):
        from services import guild_units, steal_build
        await self.register()
        guild_units._public_reports.clear()
        guild_units._public_lock = asyncio.Lock()
        comlink = SimpleNamespace(get_player=Mock(
            side_effect=[{"name": "Loaded", "rosterUnit": [{"definitionId": "UNIT:SEVEN_STAR"}]},
                         RuntimeError("offline")]))
        lookup = SimpleNamespace(ok=True, swgoh_guild_id="external", guild_name="External",
                                 members=[("p1", "Loaded"), ("p2", "Failed")])
        with patch.object(steal_build, "resolve_guild", AsyncMock(return_value=lookup)), \
             patch.object(guild_units, "unit_report", return_value={"members": 1}) as build:
            report = await guild_units.public_unit_report(comlink, self.author, "UNIT", None)
            again = await guild_units.public_unit_report(comlink, self.author, "UNIT", None)
        self.assertEqual(report, again)
        self.assertEqual(comlink.get_player.call_count, 2)
        self.assertEqual(build.call_args.kwargs["members"], [("p1", "Loaded")])
        self.assertIn("не загружено: 1", report["note"])
        self.assertEqual(report["guild_name"], "External")
        guild_units._public_reports.clear()

    async def test_public_guildunit_without_registration_does_not_fetch(self):
        from services import guild_units, steal_build
        with patch.object(steal_build, "resolve_guild", AsyncMock()) as fetch:
            with self.assertRaises(ValueError):
                await guild_units.public_unit_report(None, self.author, "UNIT", None)
        fetch.assert_not_called()

    async def test_discord_personal_roster_resolves_public_main_and_alt(self):
        await self.register()
        await self.register("987654321", is_alt=True)
        cards = SimpleNamespace(_card_players=AsyncMock(return_value=([
            {"name": "Player", "rosterUnit": [{"definitionId": "UNIT:SEVEN_STAR"}]}], [None])))
        cog = GuildUnitsCog(SimpleNamespace(get_cog=lambda _: cards))
        inter = SimpleNamespace(author=self.author, edit_original_response=AsyncMock())
        for alt, code in ((False, "123456789"), (True, "987654321")):
            result = await cog._player_roster(inter, None, None, None, alt)
            self.assertIsNotNone(result)
            self.assertEqual(cards._card_players.call_args.args[0], [code])

    async def test_connected_default_and_same_guild_alt_keep_their_priority(self):
        database.set_user_registration("123", "123456789", guild_id=5)
        database.set_user_registration("123", "222333444", is_main=False, guild_id=5)
        database.set_user_registration("123", "987654321", guild_id=6)
        with patch.object(guild_resolver, "resolve_access", return_value={"guild_id": 5, "ally_code": "123456789"}):
            self.assertEqual(public_checks.account(self.author)["ally_code"], "123456789")
            self.assertEqual(public_checks.account(self.author, alt=True)["ally_code"], "222333444")

    async def test_public_readiness_renders_using_global_requirements(self):
        await self.register()
        inter = SimpleNamespace(author=self.author, response=SimpleNamespace(defer=AsyncMock()),
                                edit_original_response=AsyncMock())
        cog = GuildUnitsCog(None)
        player = {"name": "Player", "allyCode": "123456789"}
        with patch.object(database, "get_journey_guide_requirements", return_value=[("UNIT", "relic", "5")]) as reqs, \
             patch.object(cog, "_player_roster", AsyncMock(return_value=(player, {}, "fresh"))), \
             patch("services.guild_units.player_readiness", return_value={"guide": {"name": "Guide"}, "units": []}) as build, \
             patch("services.guild_units_image.render_player", return_value=b"png"):
            await GuildUnitsCog.readiness.callback(cog, inter, "GUIDE", None, None, False, False)
        reqs.assert_called_once_with("GUIDE", None)
        build.assert_called_once_with(None, "GUIDE", {})
        self.assertIn("выполнено", inter.edit_original_response.call_args.kwargs["content"])

    async def test_telegram_gate_and_self_target_without_guild(self):
        ctx = SimpleNamespace(owner_id="123", access={"tier": None, "guild_id": None},
                              reply=AsyncMock(), reply_user=None)
        self.assertIsNotNone(core.denied_text(core.TIER_REGISTERED, ctx))
        await self.register()
        self.assertIsNone(core.denied_text(core.TIER_REGISTERED, ctx))
        self.assertIsNotNone(core.denied_text(core.TIER_MEMBER, ctx))
        self.assertEqual(await handlers._resolve_target(ctx, None, "", "test"), "123456789")
        for name in ("unit", "ready", "faction", "profile", "raidready", "guildunit"):
            self.assertEqual(core.COMMANDS[name].tier, core.TIER_REGISTERED)
        for prefix in ("un", "uw", "ua", "rd", "rw", "fc", "fw", "pf", "gu"):
            self.assertEqual(core.CALLBACKS[prefix].tier, core.TIER_REGISTERED)

    async def test_discord_public_faction_renders_without_a_guild(self):
        await self.register()
        inter = SimpleNamespace(author=self.author, response=SimpleNamespace(defer=AsyncMock()),
                                edit_original_response=AsyncMock())
        cog = GuildUnitsCog(None)
        with patch.object(cog, "_player_roster", AsyncMock(return_value=({"name": "Player"}, {}, "fresh"))) as roster, \
             patch("services.faction.resolve", return_value="empire"), \
             patch("services.faction.build", return_value={"name": "Империя", "owned": 1, "total": 2}), \
             patch("services.faction_image.render", return_value=b"png"):
            await GuildUnitsCog.faction.callback(cog, inter, "empire", None, None, False)
        self.assertIsNone(roster.call_args.args[1])
        self.assertIn("открыто 1 из 2", inter.edit_original_response.call_args.kwargs["content"])

    async def test_discord_public_profile_renders_own_and_explicit_player(self):
        await self.register()
        self.comlink.get_player.return_value = {"name": "Player", "rosterUnit": [{"definitionId": "UNIT"}]}
        cog = ProfileCog(SimpleNamespace(comlink=self.comlink, stat_calc=object()))
        inter = SimpleNamespace(author=self.author, response=SimpleNamespace(defer=AsyncMock()),
                                edit_original_response=AsyncMock())
        with patch("stat_engine.wait_stat_calc", AsyncMock(return_value=True)), \
             patch("services.player_profile.load_context", return_value={}), \
             patch("services.player_profile.player_meta", return_value={}), \
             patch("services.player_profile.summarize", return_value={}), \
             patch("services.profile_image.render_player", return_value=b"png"):
            for code, expected in ((None, "123456789"), ("987654321", "987654321")):
                await ProfileCog.profile.callback(cog, inter, None, code, False)
                self.assertEqual(self.comlink.get_player.call_args.kwargs, {"allycode": expected})
                self.assertEqual(inter.edit_original_response.call_args.kwargs["file"].filename, f"profile_{expected}.png")

    async def test_discord_profile_guild_mode_denied_before_statcalc(self):
        await self.register()
        inter = SimpleNamespace(author=self.author, response=SimpleNamespace(defer=AsyncMock()),
                                edit_original_response=AsyncMock())
        with patch("stat_engine.wait_stat_calc", AsyncMock()) as calc:
            await ProfileCog.profile.callback(ProfileCog(None), inter, None, None, True)
        calc.assert_not_awaited()
        self.assertIn("подключённых", inter.edit_original_response.call_args.args[0])

    async def test_connected_guild_profile_keeps_existing_access(self):
        cog = ProfileCog(SimpleNamespace())
        inter = SimpleNamespace(author=self.author, response=SimpleNamespace(defer=AsyncMock()),
                                edit_original_response=AsyncMock())
        with patch.object(guild_resolver, "resolve_guild_id", return_value=5), \
             patch("stat_engine.wait_stat_calc", AsyncMock(return_value=True)), \
             patch("services.player_profile.load_context", return_value={}), \
             patch.object(cog, "_guild_profile", AsyncMock()) as guild:
            await ProfileCog.profile.callback(cog, inter, None, None, True)
        guild.assert_awaited_once_with(inter, 5, {})

    async def test_telegram_public_profile_own_target_and_guild_rejection(self):
        await self.register()
        ctx = SimpleNamespace(owner_id="123", access={"tier": None, "guild_id": None}, args="", reply_user=None,
                              reply=AsyncMock())
        with patch.object(handlers, "_player_profile", AsyncMock()) as player, \
             patch.object(handlers, "_guild_profile", AsyncMock()) as guild:
            await handlers.cmd_profile(ctx)
            player.assert_awaited_once_with(ctx, "123456789")
            for arg in ("гильдия", "guild", "г"):
                ctx.args = arg
                await handlers.cmd_profile(ctx)
            guild.assert_not_awaited()
        self.assertIn("подключённых", ctx.reply.call_args.args[0])

    async def test_telegram_guild_readiness_and_guild_picker_are_closed(self):
        await self.register()
        ctx = SimpleNamespace(owner_id="123", access={"tier": None, "guild_id": None},
                              args="Рей гильдия", reply=AsyncMock())
        with patch.object(more.search, "guides") as search:
            await more.cmd_ready(ctx)
        search.assert_not_called()
        ctx.user = {"id": 123}
        with patch.object(database, "get_unit_types", return_value={"UNIT": "char"}):
            keyboard = more._unit_compare_keyboard(ctx, {"guild_id": None}, "UNIT", ["987654321"])
        self.assertFalse(any("Игрок гильдии" in b["text"] for row in keyboard or [] for b in row))
