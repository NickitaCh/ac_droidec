import json
from contextlib import closing
import sqlite3
import tempfile
import unittest
from datetime import datetime, date
from pathlib import Path
from unittest.mock import patch

import database
from services import assistant_context as c, mention_assistant as a


class ContextTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = patch.object(database, 'DB_NAME', str(Path(self.tmp.name) / 'test.db'))
        self.db.start()
        self.anchor = patch.object(c.tb_schedule, 'get_week_anchor_date', return_value=date(2026, 11, 23))
        self.anchor.start()
        self.offset = patch.object(c.tb_schedule, 'effective_dst_offset_minutes', return_value=60)
        self.offset.start()

    def tearDown(self):
        self.offset.stop()
        self.anchor.stop()
        self.db.stop()
        self.tmp.cleanup()

    def now(self, text):
        return datetime.fromisoformat(text + '+03:00')

    def test_calendar_before_after_phase_boundary_and_inactive_week(self):
        for stamp, phase, remaining in [('2026-11-25T19:30:00', 2, 5400),
                                         ('2026-11-25T21:00:00', 3, 86400),
                                         ('2026-11-29T20:59:00', 6, 60)]:
            with self.subTest(stamp=stamp):
                state = c.tb_calendar(self.now(stamp))
                self.assertEqual((state['phase'], state['remaining_seconds']), (phase, remaining))
                self.assertFalse(state['confirmed_live'])
        for stamp in ['2026-11-23T20:59:00', '2026-11-29T21:00:00', '2026-12-02T22:00:00']:
            self.assertFalse(c.tb_calendar(self.now(stamp))['active'])

    def test_fresh_game_deadline_wins_but_stale_other_guild_and_expired_do_not(self):
        now = self.now('2026-11-25T19:30:00')
        with closing(sqlite3.connect(database.DB_NAME)) as db, db:
            db.execute('CREATE TABLE tb_live_snapshots (guild_id INTEGER,id INTEGER PRIMARY KEY,captured INTEGER,state TEXT)')
            state = {'phase': 3, 'end_ms': (now.timestamp() + 1800) * 1000,
                     'players': {'PRIVATE_PLAYER': 123}, 'zones': {'PRIVATE_ZONE': {}}}
            db.execute('INSERT INTO tb_live_snapshots VALUES(1,1,?,?)', (now.timestamp() - 30, json.dumps(state)))
        result = c.tb_context(1, now)
        self.assertTrue(result['confirmed_live'])
        self.assertEqual(result['remaining_seconds'], 1800)
        self.assertNotIn('PRIVATE', json.dumps(result))
        self.assertFalse(c.tb_context(2, now)['confirmed_live'])
        with closing(sqlite3.connect(database.DB_NAME)) as db, db:
            db.execute('UPDATE tb_live_snapshots SET captured=?', (now.timestamp() - 901,))
        self.assertFalse(c.tb_context(1, now)['confirmed_live'])
        with closing(sqlite3.connect(database.DB_NAME)) as db, db:
            state['end_ms'] = (now.timestamp() - 1) * 1000
            db.execute('UPDATE tb_live_snapshots SET captured=?,state=?', (now.timestamp(), json.dumps(state)))
        self.assertFalse(c.tb_context(1, now)['confirmed_live'])

    def test_context_uses_only_own_guild_and_allowlisted_aggregates(self):
        with closing(sqlite3.connect(database.DB_NAME)) as db, db:
            db.execute('CREATE TABLE guild_daily_snapshots(guild_id INTEGER,day TEXT,metrics_json TEXT,updated_at TEXT)')
            db.execute('INSERT INTO guild_daily_snapshots VALUES(1,?,?,?)',
                       ('2026-11-25', json.dumps({'gp': 123, 'secret': 'PRIVATE'}), '2026-11-25T14:00:00+00:00'))
            db.execute('INSERT INTO guild_daily_snapshots VALUES(2,?,?,?)',
                       ('2026-11-25', json.dumps({'gp': 999}), 'today'))
        with patch.object(database, 'get_guild_config', return_value={'name': 'АС', 'token': 'SECRET'}):
            result = c.build({'guild_id': 1}, now=self.now('2026-11-25T19:30:00'))
        self.assertEqual(result['guild_snapshot']['metrics'], {'gp': 123})
        self.assertNotIn('SECRET', json.dumps(result))
        self.assertNotIn('PRIVATE', json.dumps(result))

    def test_dialogue_isolated_bounded_and_expires(self):
        dialogue = c.Dialogue()
        key = (1, 20, 30)
        for n in range(5):
            dialogue.remember(key, str(n), 'answer', now=100 + n)
        self.assertEqual([t['question'] for t in dialogue.get(key, now=105)], ['2', '3', '4'])
        for other in [(2, 20, 30), (1, 21, 30), (1, 20, 31)]:
            self.assertEqual(dialogue.get(other, now=105), [])
        self.assertEqual(dialogue.get(key, now=1304), [])

    def test_tb_followup_is_recalculated_instead_of_repeating_old_countdown(self):
        history = [{'question': 'Когда этап ТБ закончится?', 'answer': 'Через 2 часа.'}]
        context = {'tb': c.tb_calendar(self.now('2026-11-25T19:30:00'))}
        plan = a.local_plan('а сколько осталось?', context=context, history=history)
        self.assertIn('1 ч 30 мин', plan['text'])
        self.assertIsNone(a.local_plan('А на каком ты этапе эволюции?))', context=context, history=history))


if __name__ == '__main__':
    unittest.main()
