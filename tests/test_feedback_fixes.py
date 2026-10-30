"""Focused regressions. Run: python -X utf8 -m unittest discover -s tests -v"""
import asyncio
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import database
from services import mod_analysis, mod_compare, tw_def_library, stat_forecast
from services.steal_build import GuildLookupResult
from tg_bot import command_access, core, handlers, more, checks, notifier, store
from tg_bot.api import TelegramAPI, TelegramError

command_access.install()


class FeedbackTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = patch.object(database, "DB_NAME", str(Path(self.temp.name) / "test.db"))
        self.db.start()
        mod_compare._refs.clear()
        mod_compare._progress.clear()
        mod_analysis._REPORT_CACHE.clear()

    def tearDown(self):
        self.db.stop()
        self.temp.cleanup()

    def test_compact_number_and_defense(self):
        self.assertEqual(mod_compare._fmt(10900, "Health"), "10,9к")
        self.assertEqual(mod_compare._fmt(71000, "Protection"), "71,0к")
        self.assertIn(("Defense", "Оборона"), mod_compare.STAT_LINES)

    async def test_shared_daily_roster_across_characters_and_reports(self):
        lookup = GuildLookupResult(ok=True, swgoh_guild_id="guild", guild_name="Test", members=[("p", "P")])
        comlink = SimpleNamespace(get_player=lambda **_: {"name": "P", "rosterUnit": [{"definitionId": "A:SEVEN_STAR"}]})
        with patch.object(mod_compare, "_fetch", wraps=mod_compare._fetch) as fetch:
            await asyncio.gather(mod_compare.ensure_roster(comlink, lookup), mod_compare.ensure_roster(comlink, lookup))
            self.assertEqual(fetch.call_count, 1)
        with patch.object(comlink, "get_player", side_effect=AssertionError("network refetch")), \
             patch.object(mod_analysis, "_compute_player_deltas"), \
             patch.object(database, "get_game_unit_name", return_value="A"):
            resolved = await mod_compare.resolve(comlink, "guild")
            for base_id in ("A", "B"):
                report = await mod_analysis.build_report_live(comlink, None, base_id, None, resolved)
                self.assertIsNone(report["error"])
            self.assertEqual((await mod_analysis.build_report_live(comlink, None, "A", None, resolved))["total_open"], 1)

    async def test_failed_fetch_keeps_previous_payload(self):
        lookup = GuildLookupResult(ok=True, swgoh_guild_id="g", guild_name="G", members=[("p", "P")])
        comlink = SimpleNamespace(get_player=lambda **_: {"name": "P", "rosterUnit": []})
        await mod_compare.ensure_roster(comlink, lookup)
        before = mod_compare.roster_info("g")["fetched_at"]
        with patch.object(comlink, "get_player", side_effect=RuntimeError("offline")):
            mod_compare.start_fetch(comlink, lookup)
            await mod_compare._progress["g"]["task"]
        self.assertEqual(mod_compare.roster_info("g")["fetched_at"], before)

    def test_comparison_does_not_retain_rosters_in_result_cache(self):
        guild = {"own": False, "id": "g", "name": "G", "roster": []}
        with patch.object(database, "get_game_unit_names", return_value={}):
            row = mod_compare.compare(None, ["A"], [guild], None)[0]["rows"][0]
        self.assertNotIn("roster", row["guild"])
        self.assertIn("character=A&relic=&guild_ref=g", row["analysis_url"])

    async def test_unit_single_photo_with_compare_buttons(self):
        ctx = SimpleNamespace(action=AsyncMock(), reply=AsyncMock(), reply_photo=AsyncMock())
        state = {"levels": {}, "guild_id": 1, "refresh": False}
        with patch.object(more, "_stat_calc", AsyncMock(return_value=object())), \
             patch.object(database, "get_unit_types", return_value={"A": "character"}), \
             patch.object(more, "_card_players", AsyncMock(return_value=([{"rosterUnit": [1]}], [0]))), \
             patch.object(more.toon, "build", return_value={}), \
             patch.object(more.toon_image, "render", return_value=b"png"), \
             patch.object(more, "_unit_compare_keyboard", return_value=[[{"text": "compare"}]]):
            await more._unit_card(ctx, state, "A", ["123"])
        ctx.reply.assert_not_called()
        self.assertEqual(ctx.reply_photo.call_count, 1)
        self.assertTrue(ctx.reply_photo.call_args.kwargs["keyboard"])

    async def test_photo_fallback_preserves_keyboard_and_topic(self):
        api = SimpleNamespace(send_photo=AsyncMock(side_effect=TelegramError("sendPhoto", "PHOTO_INVALID_DIMENSIONS")),
                              send_document=AsyncMock(return_value={}))
        ctx = object.__new__(core.Ctx)
        ctx.api, ctx.chat_id, ctx.thread_id, ctx.callback_query = api, -10, 42, None
        ctx.message = {"message_id": 3}
        keyboard = [[{"text": "compare", "callback_data": "ua|x|g"}]]
        await ctx.reply_photo(b"x", "x.png", keyboard=keyboard)
        self.assertEqual(api.send_document.call_args.kwargs["keyboard"], keyboard)
        self.assertEqual(api.send_document.call_args.kwargs["thread_id"], 42)

    async def test_keyboard_is_sent_in_photo_and_document_api_payloads(self):
        api = object.__new__(TelegramAPI)
        api.call = AsyncMock(return_value={})
        keyboard = [[{"text": "compare", "callback_data": "ua|x|g"}]]
        await api.send_photo(-10, b"x", "x.png", keyboard=keyboard, thread_id=42)
        self.assertEqual(api.call.call_args.kwargs["reply_markup"]["inline_keyboard"], keyboard)
        self.assertEqual(api.call.call_args.kwargs["message_thread_id"], 42)
        await api.send_document(-10, b"x", "x.png", "image/png", keyboard=keyboard)
        self.assertEqual(api.call.call_args.kwargs["reply_markup"]["inline_keyboard"], keyboard)

    async def test_unit_compare_replaces_photo_instead_of_sending_another(self):
        api = SimpleNamespace(call=AsyncMock(return_value={}), send_photo=AsyncMock())
        ctx = object.__new__(core.Ctx)
        ctx.api, ctx.chat_id, ctx.callback_query = api, -10, {"id": "q"}
        ctx.message = {"message_id": 3, "photo": [{"file_id": "old"}]}
        await ctx.reply_photo(b"x", "x.png", caption="Updated", replace=True)
        api.send_photo.assert_not_called()
        self.assertEqual(api.call.call_args.args[0], "editMessageMedia")
        self.assertEqual(api.call.call_args.kwargs["message_id"], 3)

    async def test_foreign_guild_officer_cannot_configure_chat(self):
        store.bind_chat(-10, 1, "G", "supergroup", "officer")
        ctx = SimpleNamespace(chat_id=-10, access={"guild_id": 2, "is_super_admin": False}, reply=AsyncMock())
        await core.COMMANDS["command_access"].handler(ctx)
        self.assertIn("гильдии", ctx.reply.call_args.args[0])
        self.assertEqual(command_access.blocked(-10, None), set())

    async def test_topic_block_covers_alias_and_callback_only_in_that_topic(self):
        store.bind_chat(-10, 1, "G", "supergroup", "officer")
        command_access.toggle(1, -10, 42, "unit")
        api = SimpleNamespace(send_message=AsyncMock(), answer_callback=AsyncMock())
        app = SimpleNamespace(api=api, username="bot")
        msg = {"chat": {"id": -10, "type": "supergroup"}, "from": {"id": 20}, "message_id": 2,
               "message_thread_id": 42, "is_topic_message": True, "text": "/юнит Рей"}
        with patch.object(core, "denied_text", return_value=None), \
             patch.object(core.COMMANDS["unit"], "handler", AsyncMock()) as handler:
            await core._handle_message(app, msg)
            handler.assert_not_called()
        with patch.object(core.CALLBACKS["ua"], "handler", AsyncMock()) as handler:
            await core._handle_callback(app, {"id": "q", "from": {"id": 20}, "message": msg, "data": "ua|x|g"})
            handler.assert_not_called()
        self.assertEqual(command_access.blocked(-10, 43), set())
        self.assertEqual(command_access.blocked(-10, None), set())
        command_access.toggle(1, -10, 42, "unit")
        self.assertEqual(command_access.blocked(-10, 42), set())

    def test_all_feature_callbacks_have_command_gate(self):
        self.assertTrue(core.CALLBACKS)
        for cb in core.CALLBACKS.values():
            self.assertIn(cb.command_name, core.COMMANDS)

    def test_library_import_is_scoped_idempotent_and_preserves_requirements(self):
        slots = [{"options": [{"base_id": "A", "min_relic": 7, "zeta": True,
                                "stats": [{"stat": "Speed", "min": 300}]}]}]
        source_id = database.save_tw_def_squad(1, None, "Example", "character", slots, "note", "source", {"id": 7})
        raw = json.dumps(tw_def_library.export_library(1))
        self.assertNotIn("datacron", raw)
        with patch.object(database, "get_unit_types", return_value={"A": "character"}), \
             patch.object(database, "get_game_unit_names", return_value={"A": "Test Unit"}), \
             patch.object(database, "get_omicron_capable", return_value=set()):
            self.assertFalse(tw_def_library.preview(raw, 2)[0]["exists"])
            self.assertEqual(tw_def_library.import_selected(raw, 2, {0}, "officer"), 1)
            self.assertEqual(tw_def_library.import_selected(raw, 2, {0}, "officer"), 0)
        imported = database.list_tw_def_squads(2)[0]
        self.assertEqual(imported["slots"], slots)
        self.assertIsNone(imported["datacron"])
        self.assertIsNotNone(database.get_tw_def_squad(1, source_id))
        self.assertIsNone(database.get_tw_def_squad(3, imported["id"]))

    def test_omicron_phrases_are_isolated_with_guild_local_fallback(self):
        database.set_omicron_phrase("A", "AC default", guild_id=1)
        database.set_omicron_phrase("A", "AC skill", skill_id="skill", guild_id=1)
        self.assertIsNone(database.get_omicron_phrase("A", "skill", guild_id=2))
        self.assertEqual(database.get_all_omicron_phrases(2), [])
        database.set_omicron_phrase("A", "Other default", guild_id=2)
        self.assertEqual(database.get_omicron_phrase("A", "skill", guild_id=2), "Other default")
        self.assertEqual(database.get_omicron_phrase("A", "skill", guild_id=1), "AC skill")
        database.delete_omicron_phrase("A", guild_id=2)
        self.assertEqual(database.get_omicron_phrase("A", guild_id=1), "AC default")

    def test_global_omicron_migration_keeps_legacy_rows_only_in_ac(self):
        with sqlite3.connect(database.DB_NAME) as conn:
            conn.execute("""CREATE TABLE omicron_phrases (id INTEGER PRIMARY KEY, character_key TEXT,
                skill_id TEXT NOT NULL DEFAULT '', phrase TEXT, updated_by TEXT, updated_at TEXT,
                UNIQUE(character_key, skill_id))""")
            conn.execute("INSERT INTO omicron_phrases VALUES (7, 'A', '', 'Legacy AC', 'officer', '2026-10-29')")
            conn.execute("INSERT INTO omicron_phrases VALUES (8, 'A', 'skill', 'Legacy skill', 'officer', '2026-10-29')")
        conn.close()
        self.assertEqual(database.get_all_omicron_phrases(2), [])  # read triggers and commits migration
        self.assertEqual(database.get_all_omicron_phrases(1)[0][0], 7)
        self.assertEqual(database.get_omicron_phrase("A", "skill", guild_id=1), "Legacy skill")
        database.set_omicron_phrase("A", "Other", skill_id="skill", guild_id=2)
        self.assertEqual(database.get_omicron_phrase("A", "skill", guild_id=1), "Legacy skill")

    def test_oldest_omicron_schema_migrates_without_skill_id(self):
        with sqlite3.connect(database.DB_NAME) as conn:
            conn.execute("""CREATE TABLE omicron_phrases (id INTEGER PRIMARY KEY, character_key TEXT UNIQUE,
                phrase TEXT, updated_by TEXT, updated_at TEXT)""")
            conn.execute("INSERT INTO omicron_phrases VALUES (3, 'A', 'Oldest AC', NULL, '2026-08-20')")
        conn.close()
        self.assertIsNone(database.get_omicron_phrase("A", guild_id=2))
        self.assertEqual(database.get_omicron_phrase("A", "skill", guild_id=1), "Oldest AC")

    def test_omicron_standard_announcement_stays_shared_only_suffix_is_scoped(self):
        from cogs.stat_requirements import StatRequirementsCog
        database.set_omicron_phrase("A", "AC suffix", guild_id=1)
        with patch("cogs.stat_requirements._unit_display_name", return_value="Unit"):
            ac = StatRequirementsCog._omicron_line("Player", "A", "skill", {}, guild_id=1)
            other = StatRequirementsCog._omicron_line("Player", "A", "skill", {}, guild_id=2)
        self.assertEqual(ac, other + " AC suffix")

    async def test_statcalc_simultaneous_requests_share_one_build(self):
        async def build(_):
            await asyncio.sleep(0)
            return object()
        with patch.object(stat_forecast, "_stat_calc", None), \
             patch.object(stat_forecast, "_cached_at", 0), \
             patch.object(stat_forecast, "_refresh_task", None), \
             patch.object(stat_forecast.stat_engine, "build_stat_calc", AsyncMock(side_effect=build)) as builder:
            results = await asyncio.gather(*(stat_forecast.get_stat_calc(None) for _ in range(5)))
            self.assertEqual(builder.await_count, 1)
            self.assertTrue(all(r is results[0] for r in results))

    async def test_cancelled_request_does_not_cancel_shared_statcalc_build(self):
        started, release = asyncio.Event(), asyncio.Event()
        result = object()
        async def build(_):
            started.set()
            await release.wait()
            return result
        with patch.object(stat_forecast, "_stat_calc", None), \
             patch.object(stat_forecast, "_cached_at", 0), \
             patch.object(stat_forecast, "_refresh_task", None), \
             patch.object(stat_forecast.stat_engine, "build_stat_calc", AsyncMock(side_effect=build)) as builder:
            first = asyncio.create_task(stat_forecast.get_stat_calc(None))
            await started.wait()
            second = asyncio.create_task(stat_forecast.get_stat_calc(None))
            first.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await first
            release.set()
            self.assertIs(await second, result)
            self.assertEqual(builder.await_count, 1)


if __name__ == "__main__":
    unittest.main()
