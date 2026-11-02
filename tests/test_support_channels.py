import asyncio
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import database
from services import donation_progress, feedback, payments, discord_rest


class SupportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = patch.object(database, "DB_NAME", str(Path(self.tmp.name) / "test.db"))
        self.db.start()
        database.init_db()

    def tearDown(self):
        self.db.stop()
        self.tmp.cleanup()

    def test_only_confirmed_donations_count_and_webhooks_do_not_double_count(self):
        for i in range(502):
            database.record_donation(str(i), 10, None, None, None, None)
            if i < 501:
                database.mark_donation_succeeded(str(i))
        self.assertEqual(database.get_confirmed_donation_total(), 5010)
        self.assertIsNone(database.mark_donation_succeeded("0"))
        self.assertEqual(donation_progress.current(), {"total": 5010, "goal": 5000, "value": 5000})

    def test_payment_notifies_only_nod_once(self):
        database.record_donation("p", 100, None, "Donor", None, None)
        with patch.object(discord_rest, "send_dm", new_callable=AsyncMock) as dm, \
                patch.object(discord_rest, "notify_super_admins", new_callable=AsyncMock) as all_admins:
            asyncio.run(payments._handle_donation("p"))
            asyncio.run(payments._handle_donation("p"))
        dm.assert_awaited_once()
        self.assertEqual(dm.call_args.args[0], "276671902733434880")
        all_admins.assert_not_awaited()

    def test_feedback_saved_and_rate_limited(self):
        for i in range(10):
            report_id = feedback.create("tg:42", "Author", None, "Example", user_agent="Telegram")
        self.assertEqual(database.get_bug_report(report_id)["message"], "Example")
        self.assertEqual(database.get_bug_report_blob(report_id)["user_agent"], "Telegram")
        with self.assertRaises(ValueError):
            feedback.create("tg:42", "Author", None, "One more")

    def test_telegram_reply_is_saved_without_guild_and_owner_is_notified(self):
        from tg_bot import core
        from tg_bot.feedback import handle_reply
        from types import SimpleNamespace
        app = SimpleNamespace(api=SimpleNamespace(send_message=AsyncMock()), bot_id=99, username="bot")
        message = {"chat": {"id": 42, "type": "private"}, "from": {"id": 42, "first_name": "Author"},
                   "message_id": 2, "text": "An idea", "reply_to_message": {
                       "from": {"id": 99}, "text": "Ответьте на это сообщение: опишите ошибку, предложение или вопрос"}}
        with patch.object(core.guild_resolver, "resolve_access", return_value={"guild_id": None, "tier": None}), \
                patch.object(discord_rest, "send_dm", new_callable=AsyncMock) as notify:
            self.assertTrue(asyncio.run(handle_reply(app, message)))
        self.assertEqual(database.get_bug_reports()[0]["discord_id"], "tg:42")
        notify.assert_awaited_once()

    def test_admin_reply_to_linked_telegram_user_stays_in_telegram(self):
        from tg_bot import store, outbox
        from web.routes import support
        store.touch_user({"id": 42, "first_name": "Author"}, dm_open=True)
        with sqlite3.connect(database.DB_NAME) as conn:
            conn.execute("UPDATE tg_users SET owner_id='123' WHERE tg_user_id=42")
        conn.close()
        report_id = feedback.create("123", "Author", None, "Example", user_agent="Telegram")
        with patch.object(discord_rest, "send_dm", new_callable=AsyncMock) as discord:
            asyncio.run(support.admin_feedback_update(report_id, status="done", reply="Thank you",
                                                       back="open", user={"discord_id": "admin"}))
        discord.assert_not_awaited()
        self.assertEqual(database.get_bug_report(report_id)["reply"], "Thank you")
        item = outbox.pending()[0]
        self.assertEqual(item["owner_id"], "123")
        self.assertIn("Thank you", item["text"])


if __name__ == "__main__":
    unittest.main()
