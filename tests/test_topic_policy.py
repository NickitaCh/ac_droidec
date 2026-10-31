"""Topic allowlists must not grant officer permissions or leak between guilds."""
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch, AsyncMock

import database
from tg_bot import command_access as access, topics, store, outbox
import tb_platoon_notify


class TopicPolicyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = patch.object(database, 'DB_NAME', str(Path(self.temp.name) / 'test.db'))
        self.db.start()
        store.bind_chat(-100123, 7, 'Group', 'supergroup', 'owner')
        store.bind_chat(-100456, 8, 'Other', 'supergroup', 'owner')

    def tearDown(self):
        self.db.stop()
        self.temp.cleanup()

    def ctx(self, tid, tier='member', chat=-100123):
        return SimpleNamespace(is_private=False, chat_id=chat, thread_id=tid,
                               access={'tier': tier, 'guild_id': 7, 'is_super_admin': False})

    def test_default_deny_whitelist_and_future_topics(self):
        access.reset_policy(7, -100123, False)
        access.set_rule(7, -100123, 20, 'all', '*', True)
        for tid in (None, 1, 21, 999):
            self.assertTrue(access.denied(self.ctx(tid), 'unit'))
        self.assertFalse(access.denied(self.ctx(20), 'unit'))
        self.assertFalse(access.denied(self.ctx(20), 'warns'))  # normal tier gate still follows
        self.assertFalse(access.denied(self.ctx(20, chat=-100456), 'unit'))

    def test_officer_exceptions_are_additive_and_require_real_role(self):
        access.reset_policy(7, -100123, False)
        access.set_rule(7, -100123, 30, 'officer', 'warns', True)
        self.assertTrue(access.denied(self.ctx(30), 'warns'))
        self.assertFalse(access.denied(self.ctx(30, 'officer'), 'warns'))
        foreign = self.ctx(30, 'officer'); foreign.access['guild_id'] = 8
        self.assertTrue(access.denied(foreign, 'warns'))
        self.assertTrue(access.denied(self.ctx(31, 'officer'), 'warns'))
        self.assertTrue(access.denied(self.ctx(30, 'officer'), 'unit'))
        access.set_rule(7, -100123, 30, 'all', '*', True)
        access.set_rule(7, -100123, 30, 'officer', 'unit', False)
        self.assertFalse(access.denied(self.ctx(30, 'officer'), 'unit'))

    def test_legacy_blocks_reset_and_management_escape(self):
        access.toggle(7, -100123, 20, 'unit')
        self.assertTrue(access.denied(self.ctx(20), 'unit'))
        access.reset_policy(7, -100123, False)
        for name in access.MANAGEMENT:
            self.assertFalse(access.denied(self.ctx(20), name))
        ctx = self.ctx(20); ctx.is_private = True
        self.assertFalse(access.denied(ctx, 'unit'))
        access.reset_policy(7, -100123, True)
        self.assertFalse(access.denied(self.ctx(20), 'unit'))

    def test_catalogue_observes_names_and_preserves_them(self):
        message = {'chat': {'id': -100123, 'type': 'supergroup'}, 'is_topic_message': True,
                   'message_thread_id': 20, 'forum_topic_created': {'name': 'Дроидец'}}
        topics.observe(message)
        topics.observe({**message, 'forum_topic_created': {}, 'text': 'hello'})
        self.assertEqual(next(t for t in topics.known(-100123) if t['id'] == 20)['title'], 'Дроидец')
        topics.observe({**message, 'forum_topic_created': {}, 'forum_topic_edited': {'name': 'Флудилка'}})
        self.assertIn('Флудилка', str(outbox.destinations(7)))
        self.assertNotIn('Флудилка', str(outbox.destinations(8)))
        topics.add_link(-100123, 'https://t.me/c/123/25/26', 'ВГ')
        self.assertIn(25, {t['id'] for t in topics.known(-100123)})
        with self.assertRaises(ValueError):
            topics.add_link(-100123, 'https://t.me/c/456/25', 'Foreign')


class DeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_strict_telegram_needs_no_discord_token(self):
        with patch.object(tb_platoon_notify, 'channels', return_value={'discord': True, 'tg': True}), \
             patch.object(outbox, 'enqueue_dm', return_value=True) as enqueue, \
             patch.object(tb_platoon_notify, '_discord_dm', AsyncMock()) as discord:
            ok, detail = await tb_platoon_notify.send_dm(None, '', '123', 'text', 'tg_only')
            self.assertTrue(ok)
            enqueue.assert_called_once()
            discord.assert_not_called()
        self.assertIn('tg_only', tb_platoon_notify.MODES)
        self.assertIn('discord_only', tb_platoon_notify.MODES)


    async def test_both_reports_partial_and_attempts_both(self):
        rows = [{"ally_code": "123", "name": "Player", "discord_id": "owner"}]
        with patch.object(database, "get_guild_config", return_value={"name": "Guild"}), \
             patch.object(tb_platoon_notify, "build_player_rows", AsyncMock(return_value=rows)), \
             patch.object(tb_platoon_notify, "format_dm_text", return_value="text"), \
             patch.object(tb_platoon_notify, "send_dm", AsyncMock(side_effect=[(True, "ok"), (False, "start needed")])) as send:
            report = await tb_platoon_notify.send_broadcast("token", 7, {"name": "Plan"}, 1, {"123"}, "both")
        self.assertEqual(report[0]["status"], "partial")
        self.assertEqual([c.args[-1] for c in send.await_args_list], ["discord_only", "tg_only"])

    async def test_configuration_lists_topics_and_limits_callback_size(self):
        from tg_bot import core, handlers, more, checks, notifier
        if "command_access" not in core.COMMANDS:
            access.install()
        with tempfile.TemporaryDirectory() as tmp, patch.object(database, "DB_NAME", str(Path(tmp) / "ui.db")):
            store.bind_chat(-100123, 7, "Guild", "supergroup", "owner")
            for tid in range(20, 39):
                topics.remember(-100123, tid, f"Тема {tid}")
            ctx = SimpleNamespace(chat_id=-100123, args="", message={"chat": {"id": -100123, "type": "supergroup"}},
                                  access={"tier": "officer", "guild_id": 7, "is_super_admin": False},
                                  callback_query={"id": "test"}, reply=AsyncMock(), edit=AsyncMock(),
                                  api=SimpleNamespace(answer_callback=AsyncMock()))
            await core.COMMANDS["command_access"].handler(ctx)
            callback = core.CALLBACKS["ca"].handler
            for payload in ("reset|0", "topics|all|0", "topics|officer|1", "commands|officer|20|0",
                            "cmd|officer|20|warns|0", "flip|all|21|0"):
                await callback(ctx, payload)
                for row in ctx.edit.call_args.kwargs["keyboard"]:
                    for button in row:
                        self.assertLessEqual(len(button["callback_data"].encode()), 64)
            settings = access.policy(-100123)
            self.assertFalse(settings["default"])
            self.assertTrue(access.officer_extra(settings, 20, "warns"))
            self.assertTrue(access.general_allowed(settings, 21, "unit"))
            ctx.access["guild_id"] = 8
            await callback(ctx, "reset|1")
            self.assertFalse(access.policy(-100123)["default"])
