import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from tg_bot import outbox

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
