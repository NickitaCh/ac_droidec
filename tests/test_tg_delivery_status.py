import asyncio
import sqlite3
import time
from contextlib import closing
from types import SimpleNamespace
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch
import httpx

from tg_bot import notifier, outbox
from tg_bot.api import TelegramAPI, TelegramError

class DeliveryStatusTests(unittest.TestCase):
    def test_closed_topic_has_remedy(self):
        message = outbox.delivery_error('-100123: Bad Request: TOPIC_CLOSED')
        self.assertIn('закрыта', message)
        self.assertIn('повторно', message)

    def test_latest_delivery_is_scoped_and_tracks_completion(self):
        with tempfile.TemporaryDirectory() as folder, patch('database.DB_NAME', str(Path(folder) / 'test.db')):
            conn = outbox._connect()
            conn.execute("INSERT INTO tg_outbox(guild_id,kind,text,created_at) VALUES(5,'tb_order','test',1)")
            item = conn.execute('SELECT last_insert_rowid()').fetchone()[0]
            conn.execute("INSERT INTO tg_outbox(guild_id,kind,text,created_at) VALUES(6,'tb_order','other',2)")
            conn.commit(); conn.close()
            self.assertEqual(outbox.latest_delivery(5,'tb_order')['status'], 'pending')
            self.assertIsNone(outbox.latest_delivery(7,'tb_order'))
            # Use current timestamps: mark_sent prunes old delivery history.
            outbox.mark_sent(item, 'Bad Request: TOPIC_CLOSED')
            self.assertEqual(outbox.latest_delivery(5,'tb_order')['status'], 'error')
            outbox.mark_sent(item)
            self.assertEqual(outbox.latest_delivery(5,'tb_order')['status'], 'sent')


class DeliveryRetryTests(unittest.IsolatedAsyncioTestCase):
    async def test_rate_limit_retries_only_the_undelivered_chunk(self):
        with tempfile.TemporaryDirectory() as folder, patch('database.DB_NAME', str(Path(folder) / 'test.db')):
            with closing(outbox._connect()) as conn, conn:
                conn.execute("INSERT INTO tg_outbox(guild_id,kind,text,created_at) VALUES(5,'tb_order','test',?)",
                             (time.time(),))
            error = TelegramError('sendMessage', 'Too Many Requests', 429, retry_after=120)
            send = AsyncMock(side_effect=[None, error, None])
            app = SimpleNamespace(profile='test', api=SimpleNamespace(send_message=send))
            with patch('services.bot_profiles.guild_profile', return_value='test'), \
                    patch.object(outbox, 'chats_for', return_value=[(123, None)]), \
                    patch.object(notifier, 'render', return_value=['first', 'second']), \
                    patch.object(notifier.asyncio, 'sleep', side_effect=asyncio.CancelledError):
                with self.assertRaises(asyncio.CancelledError):
                    await notifier.run_notifier(app)
                self.assertEqual(outbox.latest_delivery(5, 'tb_order')['status'], 'pending')
                self.assertEqual(outbox.pending(profile='test'), [])
                with closing(outbox._connect()) as conn, conn:
                    due = conn.execute('SELECT next_attempt_at FROM tg_outbox').fetchone()[0]
                    self.assertGreater(due - time.time(), 110)
                    conn.execute('UPDATE tg_outbox SET next_attempt_at=0')
                with self.assertRaises(asyncio.CancelledError):
                    await notifier.run_notifier(app)
                self.assertEqual(outbox.latest_delivery(5, 'tb_order')['status'], 'sent')
            self.assertEqual([call.args[1] for call in send.await_args_list], ['first', 'second', 'second'])

    async def test_attachment_read_timeout_retries_without_resending_text(self):
        send = AsyncMock()
        upload = AsyncMock()
        app = SimpleNamespace(api=SimpleNamespace(send_message=send, send_document=upload))
        url = 'https://example.test/file'
        item = {'id': 1, 'text': 'test', 'attachments': [{'url': url}], 'delivery_progress': {}}
        reply = httpx.Response(200, content=b'data', request=httpx.Request('GET', url))
        with patch.object(httpx.AsyncClient, 'get', AsyncMock(side_effect=[httpx.ReadTimeout('CDN'), reply])), \
                patch.object(outbox, 'record_delivery_progress'):
            with self.assertRaises(notifier.RetryDelivery):
                await notifier._send_to(app, 123, None, item)
            upload.assert_not_awaited()
            self.assertIsNone(await notifier._send_to(app, 123, None, item))
        send.assert_awaited_once()
        upload.assert_awaited_once()

    async def test_busy_checkpoint_is_retried_before_returning_to_queue(self):
        send = AsyncMock()
        app = SimpleNamespace(api=SimpleNamespace(send_message=send))
        with tempfile.TemporaryDirectory() as folder, patch('database.DB_NAME', str(Path(folder) / 'test.db')):
            with closing(outbox._connect()) as conn, conn:
                conn.execute("INSERT INTO tg_outbox(id,kind,text,created_at) VALUES(1,'dm','test',?)", (time.time(),))
            item = outbox.pending(profile='test')[0]
            real_checkpoint = outbox.record_delivery_progress
            with patch.object(outbox, 'record_delivery_progress', side_effect=[
                sqlite3.OperationalError('database is locked'), unittest.mock.DEFAULT,
            ], wraps=real_checkpoint) as checkpoint, patch.object(notifier.asyncio, 'sleep', AsyncMock()):
                self.assertIsNone(await notifier._send_to(app, 123, None, item))
                self.assertEqual(checkpoint.call_count, 2)
            self.assertIsNone(await notifier._send_to(app, 123, None, outbox.pending(profile='test')[0]))
        send.assert_awaited_once()

    async def test_busy_retry_schedule_preserves_http_retry_after(self):
        request = httpx.Request('POST', 'https://example.test/send')
        response = httpx.Response(429, request=request, headers={'Retry-After': '120'})
        send = AsyncMock(side_effect=httpx.HTTPStatusError('rate limited', request=request, response=response))
        app = SimpleNamespace(profile='test', api=SimpleNamespace(send_message=send))
        with tempfile.TemporaryDirectory() as folder, patch('database.DB_NAME', str(Path(folder) / 'test.db')):
            with closing(outbox._connect()) as conn, conn:
                conn.execute("INSERT INTO tg_outbox(guild_id,kind,text,created_at) VALUES(5,'tb_order','test',?)",
                             (time.time(),))
            with patch('services.bot_profiles.guild_profile', return_value='test'), \
                    patch.object(outbox, 'chats_for', return_value=[(123, None)]), \
                    patch.object(outbox, 'retry_delivery', wraps=outbox.retry_delivery, side_effect=[
                        sqlite3.OperationalError('database is locked'), unittest.mock.DEFAULT,
                    ]) as save, patch.object(notifier.asyncio, 'sleep', side_effect=[None, asyncio.CancelledError]):
                with self.assertRaises(asyncio.CancelledError):
                    await notifier.run_notifier(app)
                self.assertEqual(save.call_count, 2)
            self.assertEqual(outbox.pending(profile='test'), [])
            with closing(outbox._connect()) as conn:
                due = conn.execute('SELECT next_attempt_at FROM tg_outbox').fetchone()[0]
                self.assertGreater(due - time.time(), 110)
        send.assert_awaited_once()

    async def test_shutdown_drains_a_checkpoint_after_sqlite_unlocks(self):
        send = AsyncMock()
        app = SimpleNamespace(api=SimpleNamespace(send_message=send))
        retry_started = asyncio.Event()
        unlock = asyncio.Event()

        async def wait_for_db(delay):
            retry_started.set()
            await unlock.wait()

        with tempfile.TemporaryDirectory() as folder, patch('database.DB_NAME', str(Path(folder) / 'test.db')):
            with closing(outbox._connect()) as conn, conn:
                conn.execute("INSERT INTO tg_outbox(id,kind,text,created_at) VALUES(1,'dm','test',?)", (time.time(),))
            with patch.object(outbox, 'record_delivery_progress', wraps=outbox.record_delivery_progress, side_effect=[
                sqlite3.OperationalError('database is locked'), unittest.mock.DEFAULT,
            ]), patch.object(notifier.asyncio, 'sleep', side_effect=wait_for_db):
                worker = asyncio.create_task(notifier._send_to(app, 123, None, outbox.pending(profile='test')[0]))
                try:
                    await asyncio.wait_for(retry_started.wait(), 5)
                    worker.cancel()
                finally:
                    unlock.set()
                with self.assertRaises(asyncio.CancelledError):
                    await worker
            self.assertIsNone(await notifier._send_to(app, 123, None, outbox.pending(profile='test')[0]))
        send.assert_awaited_once()

    async def test_json_rate_limit_uses_header_if_response_has_no_delay(self):
        api = TelegramAPI('synthetic')
        response = httpx.Response(429, json={'ok': False, 'error_code': 429}, headers={'Retry-After': '120'})
        try:
            with patch.object(api._client, 'post', AsyncMock(return_value=response)):
                with self.assertRaises(TelegramError) as error:
                    await api.call('sendMessage')
            self.assertEqual(error.exception.retry_after, 120)
        finally:
            await api.close()

    async def test_non_json_telegram_error_does_not_expose_request_credentials(self):
        api = TelegramAPI('synthetic-bot-token')
        response = httpx.Response(429, content=b'Proxy rate limit', headers={'Retry-After': '120'},
                                  request=httpx.Request('POST', 'https://example.test/botsynthetic-bot-token/getUpdates'))
        try:
            with patch.object(api._client, 'post', AsyncMock(return_value=response)):
                with self.assertRaises(TelegramError) as error:
                    await api.get_updates(None)
            self.assertNotIn('synthetic-bot-token', repr(error.exception))
            self.assertEqual((error.exception.error_code, error.exception.retry_after), (429, 120))
        finally:
            await api.close()
