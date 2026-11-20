import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from services import event_panels as panels
from tg_bot import event_panels as tg
from tg_bot.core import COMMANDS
from cogs import event_panels as discord_panels
from cogs.event_panels import EventPanelCog


class EventPanelTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        db = patch.object(panels.database, "DB_NAME", str(Path(self.tmp.name) / "test.db"))
        db.start()
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(db.stop)

    def test_new_panel_invalidates_old_callback_and_preserves_other_platform(self):
        def make(platform, msg):
            revision, token = panels.reserve(platform, 1, "tb")
            active, previous = panels.activate(platform, 1, "tb", revision, token, 88, msg)
            self.assertTrue(active)
            return token, previous

        first, _ = make("discord", 100)
        tg_token, _ = make("telegram", 200)
        new, previous = make("discord", 300)
        self.assertEqual(previous["message"], "100")
        self.assertFalse(panels.handoff("discord", 1, "tb", first, 88, 100, "one", "watch"))
        self.assertFalse(panels.handoff("discord", 1, "tb", new, 88, 100, "one", "watch"))
        self.assertFalse(panels.handoff("discord", 1, "tb", new, 99, 300, "one", "watch"))
        self.assertTrue(panels.handoff("discord", 1, "tb", new, 88, 300, "one", "watch"))
        self.assertTrue(panels.handoff("telegram", 1, "tb", tg_token, 88, 200, "one", "watch"))

    def test_older_command_response_cannot_take_over_if_it_finishes_late(self):
        old, old_token = panels.reserve("discord", 1, "tw")
        new, new_token = panels.reserve("discord", 1, "tw")
        self.assertTrue(panels.activate("discord", 1, "tw", new, new_token, 88, 200)[0])
        self.assertFalse(panels.activate("discord", 1, "tw", old, old_token, 77, 100)[0])
        self.assertEqual(panels.current("discord", 1, "tw")["message"], "200")

    def test_only_the_clicking_officer_can_redeem_a_link(self):
        rev, token = panels.reserve("discord", 1, "tb")
        panels.activate("discord", 1, "tb", rev, token, 88, 100)
        def ticket():
            url = panels.handoff("discord", 1, "tb", token, 88, 100, "clicker", "watch")
            return url.rsplit("/", 1)[1]

        valid = ticket()
        self.assertIsNone(panels.panel_link(valid, 1, "publisher"))
        self.assertIsNone(panels.panel_link(valid, 2, "clicker"))
        self.assertTrue(panels.panel_link(valid, 1, "clicker").endswith("/tb/live#tb-watch-controls"))
        self.assertIsNone(panels.panel_link(valid, 1, "clicker"))

        stale = ticket()
        newer, replacement = panels.reserve("discord", 1, "tb")
        panels.activate("discord", 1, "tb", newer, replacement, 88, 200)
        self.assertIsNone(panels.panel_link(stale, 1, "clicker"))

    async def test_capture_edits_only_registered_messages(self):
        for platform, message in (("discord", 100), ("telegram", 200)):
            serial, token = panels.reserve(platform, 1, "raid")
            panels.activate(platform, 1, "raid", serial, token, 77, message)
        with patch.object(panels, "allowed", return_value=True), patch.object(panels, "edit", AsyncMock()) as edit:
            await panels.publish(1, "raid", {"active": []}, 1750000000)
        self.assertIn("не вернула активное событие", panels.latest(1, "raid"))
        self.assertEqual({call.args[0]["message"] for call in edit.await_args_list}, {"100", "200"})
        for call in edit.await_args_list:
            self.assertIn("Снимок", call.args[1])

    async def test_tw_map_updates_the_registered_discord_message(self):
        serial, token = panels.reserve("discord", 1, "tw")
        panels.activate("discord", 1, "tw", serial, token, 77, 100)
        zone = {"deployed": 198, "capacity": 410, "defeated": None}
        state = {"round": 0, "end": 2_000_000_000, "own": [zone], "opponent": [{**zone, "deployed": 305}]}
        with patch.object(panels, "allowed", return_value=True), \
                patch.object(panels, "tw_board", return_value=b"png"), \
                patch.object(panels, "edit", AsyncMock()) as edit:
            await panels.publish(1, "tw", state, 1_900_000_000)
        panel, content = edit.await_args.args
        self.assertEqual(panel["message"], "100")
        self.assertIn("слоты защиты: 198 из 410", content)
        self.assertIn("слоты защиты: 305 из 410", content)
        self.assertEqual(edit.await_args.kwargs, {"tw_board": b"png", "replace_board": True})

    async def test_discord_refresh_uses_saved_session_and_updates_without_handoff(self):
        serial, token = panels.reserve("discord", 1, "tw")
        panels.activate("discord", 1, "tw", serial, token, 77, 100)
        inter = SimpleNamespace(
            author=SimpleNamespace(id=123), channel_id=77, edit_original_response=AsyncMock(),
            component=SimpleNamespace(custom_id=f"glp:tw:{token}:refresh"),
            message=SimpleNamespace(id=100), response=SimpleNamespace(defer=AsyncMock()),
        )
        with patch.object(panels, "allowed", return_value=True), \
                patch.object(discord_panels.guild_resolver, "resolve_access", return_value={"guild_id": 1, "tier": "officer"}), \
                patch.object(discord_panels.game_account, "select_account", return_value="456"), \
                patch.object(discord_panels.game_watch, "refresh_saved", AsyncMock(return_value={"own": []})) as refresh, \
                patch.object(panels, "set_refreshing", AsyncMock()) as progress, \
                patch.object(discord_panels.game_account, "reconnect", AsyncMock()) as login:
            cog = EventPanelCog()
            await cog._action(inter, "tw", "stale", "refresh", 100)
            refresh.assert_not_awaited()
            await cog.event_panel_button(inter)
        inter.response.defer.assert_awaited_once_with(with_message=True, ephemeral=True)
        self.assertEqual(refresh.await_args.args[-2:], ("456", "tw"))
        self.assertTrue(refresh.await_args.kwargs["reconnect_if_expired"])
        login.assert_not_awaited()
        self.assertIsNone(inter.edit_original_response.await_args.kwargs["view"])
        self.assertEqual(inter.edit_original_response.await_args.kwargs["content"], "Сводка обновлена.")
        self.assertEqual([call.args[1] for call in progress.await_args_list], [True, False])

    async def test_zone_button_expands_then_collapses_the_same_message(self):
        serial, token = panels.reserve("discord", 1, "tw")
        panels.activate("discord", 1, "tw", serial, token, 77, 100)
        message = SimpleNamespace(id=100, embeds=[], edit=AsyncMock())
        inter = SimpleNamespace(
            author=SimpleNamespace(id=123, display_name="Officer"), channel_id=77,
            channel=SimpleNamespace(fetch_message=AsyncMock(return_value=message)), message=message,
            component=SimpleNamespace(custom_id=f"glp:tw:{token}:details"),
            response=SimpleNamespace(defer=AsyncMock()), followup=SimpleNamespace(send=AsyncMock()),
        )
        with patch.object(panels, "allowed", return_value=True), \
                patch.object(discord_panels.guild_resolver, "resolve_access", return_value={"guild_id": 1, "tier": "officer"}), \
                patch.object(discord_panels.game_account, "select_account", return_value="456"), \
                patch.object(panels, "discord_details", return_value=["Слоты защиты 19 из 41"]) as details:
            cog = EventPanelCog()
            await cog.event_panel_button(inter)
            message.embeds = message.edit.await_args.kwargs["embeds"]
            self.assertEqual(message.embeds[0].description, "Слоты защиты 19 из 41")
            self.assertIn("Скрыть зоны", [item.label for item in message.edit.await_args.kwargs["view"].children])
            await cog.event_panel_button(inter)
        self.assertEqual(message.edit.await_args.kwargs["embeds"], [])
        self.assertEqual(details.call_count, 1)
        inter.followup.send.assert_not_awaited()

    async def test_my_defense_is_ephemeral_and_does_not_edit_public_panel(self):
        serial, token = panels.reserve("discord", 1, "tw")
        panels.activate("discord", 1, "tw", serial, token, 77, 100)
        message = SimpleNamespace(id=100, edit=AsyncMock())
        inter = SimpleNamespace(author=SimpleNamespace(id=123), channel_id=77, message=message,
            component=SimpleNamespace(custom_id=f"glp:tw:{token}:mine"),
            response=SimpleNamespace(defer=AsyncMock()), edit_original_response=AsyncMock(),
            followup=SimpleNamespace(send=AsyncMock()))
        with patch.object(panels, "allowed", return_value=True), \
                patch.object(discord_panels.guild_resolver, "resolve_access", return_value={"guild_id": 1, "tier": "officer"}), \
                patch.object(discord_panels.game_account, "select_account", return_value="456"), \
                patch.object(panels, "discord_details", return_value=["Рей · Бен Соло"]) as details:
            await EventPanelCog().event_panel_button(inter)
        inter.response.defer.assert_awaited_once_with(with_message=True, ephemeral=True)
        self.assertIn("Рей · Бен Соло", inter.edit_original_response.await_args.kwargs["content"])
        self.assertEqual(details.call_args.args, (1, "123", "456", "tw", "mine"))
        message.edit.assert_not_awaited()

    def test_commands_registered_and_callbacks_fit_telegram_limit(self):
        discord = {c.name: set(c.children) for c in EventPanelCog.__cog_app_commands__ if getattr(c, "children", None)}
        self.assertEqual(discord, {"вг": {"старт"}})
        from cogs.tb_readiness import TBReadinessCog
        from cogs.raids import RaidsCog
        for cog, name in ((TBReadinessCog, "тб"), (RaidsCog, "рейд")):
            group = next(c for c in cog.__cog_app_commands__ if c.name == name)
            self.assertIn("старт", group.children)
        for name in ("tb", "tw", "raid"):
            self.assertEqual(COMMANDS[name].where, "group")
            self.assertTrue(all(len(b["callback_data"].encode()) <= 64
                                for row in panels.telegram_keyboard(name, "test-token") for b in row))

    def test_tw_zones_only_open_opponent_and_group_same_composition(self):
        from services import game_event_live, tb_live
        zone = {"id": "tw_jakku01_phase01_conflict01", "status": tb_live.OPEN_ZONE,
                "teams": [{"units": ["A", "B", "C"], "player_name": "Nick1"},
                          {"units": ["A", "C", "B"], "player_name": "Nick2"}]}
        state = {"own": [zone], "opponent": [zone, {**zone, "status": 1}]}
        with patch.object(game_event_live, "snapshot", return_value={"data": state}), \
                patch.object(panels.database, "get_game_unit_names", return_value={"A": "Лидер", "B": "Б", "C": "В"}):
            lines = panels.discord_details(1, "owner", "code", "tw", "details")
        self.assertEqual(len(lines), 2)
        self.assertIn("Nick1, Nick2 — Лидер · Б · В", lines[1])

    async def test_repeated_refresh_is_rejected_and_error_restores_buttons(self):
        serial, token = panels.reserve("discord", 1, "tw")
        panels.activate("discord", 1, "tw", serial, token, 77, 100)
        def interaction():
            return SimpleNamespace(author=SimpleNamespace(id=123), channel_id=77,
                                   edit_original_response=AsyncMock())
        first, second = interaction(), interaction()
        started, finish = asyncio.Event(), asyncio.Event()
        async def refresh(*args, **kwargs):
            started.set()
            await finish.wait()
            raise discord_panels.game_account.AccountError("Игра недоступна")
        with patch.object(panels, "allowed", return_value=True), \
                patch.object(discord_panels.guild_resolver, "resolve_access", return_value={"guild_id": 1, "tier": "officer"}), \
                patch.object(discord_panels.game_account, "select_account", return_value="456"), \
                patch.object(panels, "set_refreshing", AsyncMock()) as progress, \
                patch.object(discord_panels.game_watch, "refresh_saved", side_effect=refresh) as read:
            cog = EventPanelCog()
            task = asyncio.create_task(cog._action(first, "tw", token, "refresh", 100))
            try:
                await asyncio.wait_for(started.wait(), 2)
                await cog._action(second, "tw", token, "refresh", 100)
                self.assertIn("уже идёт", second.edit_original_response.await_args.kwargs["content"])
                self.assertEqual(read.call_count, 1)
            finally:
                finish.set()
                await task
        self.assertEqual([call.args[1] for call in progress.await_args_list], [True, False])
        self.assertFalse(cog._busy)
        self.assertEqual(first.edit_original_response.await_args.kwargs["content"], "Игра недоступна")

    def test_defense_status_uses_own_player_and_actual_zone(self):
        rows = [{"units": ["A", "B"], "zone": "t1", "name": "Поставленный"},
                {"units": ["C"], "zone": "t1", "name": "Остался"}]
        zone = {"id": "territorywar01_conflict01", "deployed": 2,
                "teams": [{"units": ["A", "B"], "player_id": "mine"},
                          {"units": ["C"], "player_id": "someone_else"}]}
        snapshot = {"at": 100, "data": {"end": 300, "own": [zone]}}
        with patch.object(panels.database, "get_roster_by_player_id", return_value={"mine": ("123", "Me")}), \
                patch.object(panels.database, "get_game_unit_names", return_value={}), \
                patch.object(panels.time, "time", return_value=200):
            lines = panels.personal_tw_lines(1, "123", rows, snapshot)
            text = "\n".join(lines)
            self.assertIn("Уже выставлено · 1", text)
            self.assertIn("Ещё поставить · 1", text)
            self.assertLess(text.index("Поставленный"), text.index("Ещё поставить"))
            self.assertGreater(text.index("Остался"), text.index("Ещё поставить"))
            zone["deployed"] = 3  # An incomplete response cannot prove absence.
            self.assertIn("Статус не подтверждён", "\n".join(panels.personal_tw_lines(1, "123", rows, snapshot)))
            snapshot["data"]["end"] = 199
            text = "\n".join(panels.personal_tw_lines(1, "123", rows, snapshot))
            self.assertNotIn("Уже выставлено", text)
            self.assertNotIn("Ещё поставить", text)

    async def test_progress_disables_refresh_but_preserves_details_and_personal_button(self):
        import httpx
        requests = []
        panel = {"platform": "discord", "kind": "tw", "token": "token", "channel": "77", "message": "100"}
        components = [{"type": 1, "components": [
            {"type": 2, "custom_id": "glp:tw:token:" + action, "label": label}
            for action, label in [("refresh", "Обновить"), ("watch", "Наблюдение"),
                                  ("details", "Скрыть зоны"), ("mine", "Моя защита")]]}]
        def handler(request):
            if request.method == "GET":
                return httpx.Response(200, json={"components": components})
            import json
            requests.append(json.loads(request.content))
            return httpx.Response(200, json={})
        original = httpx.AsyncClient
        with patch.object(panels.discord_rest, "_headers", return_value={}), \
                patch.object(panels.httpx, "AsyncClient", side_effect=lambda **kwargs: original(transport=httpx.MockTransport(handler), **kwargs)):
            await panels.set_refreshing(panel, True)
            await panels.set_refreshing(panel, False)
        busy, ready = [r["components"][0]["components"] for r in requests]
        self.assertEqual(busy[0]["label"], "⏳ Обновляю…")
        self.assertTrue(busy[0]["disabled"] and busy[1]["disabled"])
        self.assertEqual(busy[2]["label"], "Скрыть зоны")
        self.assertNotIn("disabled", busy[3])
        self.assertEqual(ready[0]["label"], "Обновить")
        self.assertFalse(ready[0]["disabled"] or ready[1]["disabled"])

    def test_personal_preview_is_bound_to_owner_panel_event_and_expiry(self):
        import json
        from services import game_event_live
        preview = {"owner": "owner", "panel_token": "panel", "event_id": "war",
                   "expires": 200, "lines": ["Разовые назначения"]}
        with patch.object(panels.database, "get_bot_state", side_effect=lambda key, **kw:
                          json.dumps(preview) if key.startswith("tw_panel_preview:") else ""), \
                patch.object(panels.database, "get_tw_def_plan", return_value=None), \
                patch.object(panels, "current", return_value={"token": "panel"}) as current, \
                patch.object(game_event_live, "snapshot", return_value={"data": {"event_id": "war"}}) as snapshot, \
                patch.object(panels.time, "time", return_value=100):
            read = lambda owner="owner": panels.discord_details(1, owner, "code", "tw", "mine")
            self.assertEqual(read()[1:], preview["lines"])
            self.assertIn("не подтверждён", read()[0])
            self.assertNotIn("Разовые назначения", read("other"))
            current.return_value = {"token": "new"}
            self.assertNotIn("Разовые назначения", read())
            current.return_value = {"token": "panel"}
            snapshot.return_value = {"data": {"event_id": "new"}}
            self.assertNotIn("Разовые назначения", read())
            snapshot.return_value = {"data": {"event_id": "war"}}
            preview["expires"] = 99
            self.assertNotIn("Разовые назначения", read())


if __name__ == "__main__":
    unittest.main()
