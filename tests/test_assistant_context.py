import json
from contextlib import closing
import sqlite3
import tempfile
import unittest
from datetime import datetime, date
from pathlib import Path
from unittest.mock import AsyncMock, patch

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

    def test_tw_raid_context_and_history_are_distinct_and_bounded(self):
        now = self.now('2026-11-25T19:30:00')
        database.set_bot_state('live_game:tw:someone', json.dumps({'at': now.timestamp(), 'data': {
            'round': 0, 'end': now.timestamp()+3600, 'opponent_guild': {'name': 'Соперник'},
            'own': [{'capacity': 25, 'deployed': 20, 'teams': ['PRIVATE']}], 'opponent': []}}), guild_id=1)
        database.set_bot_state('live_game:raid:someone', json.dumps({'at': now.timestamp(), 'data': {'active': [{
            'id': 'order66', 'end': (now.timestamp()+7200)*1000, 'points': 123456,
            'players': [{'id': 'PRIVATE', 'points': 123}]}]}}), guild_id=1)
        database.save_raid_result(1, {'key': 'old', 'raid_id': 'order66', 'end_time': int(now.timestamp())-86400,
                                     'guild_score': 900000}, [])
        database.set_bot_state('tw_recent_opponents', json.dumps([{'name': 'Старый соперник', 'end': 123,
                                                                  'score': 500, 'opponent_score': 400}]), guild_id=1)
        with patch.object(database, 'get_guild_config', return_value={'name': 'АС'}):
            context = c.build({'guild_id': 1}, now=now)
            foreign = c.build({'guild_id': 2}, now=now)
        self.assertEqual(context['tw']['phase'], 'расстановка защиты')
        self.assertEqual(context['tw']['remaining_seconds'], 3600)
        self.assertEqual(context['tw']['own']['deployed'], 20)
        self.assertEqual(context['raid']['remaining_seconds'], 7200)
        self.assertEqual(context['raid']['points'], 123456)
        self.assertEqual(context['raid']['history'][0]['guild_score'], 900000)
        self.assertNotIn('PRIVATE', json.dumps(context))
        self.assertTrue(foreign['tw']['unavailable'])
        for question, text in [('когда закончится рейд?', '2 ч 0 мин'),
                               ('через сколько закончится расстановка ВГ?', '1 ч 0 мин')]:
            self.assertIn(text, a.local_plan(question, context=context)['text'])
        followup = [{'question': 'когда закончится рейд?', 'answer': 'Через два часа.'}]
        self.assertIn('Рейд', a.local_plan('а сколько осталось?', context=context, history=followup)['text'])

    def test_expired_event_is_not_presented_as_current_or_resurrected_from_history(self):
        now = self.now('2026-11-25T19:30:00')
        for kind, data in [('tw', {'round': 1, 'end': now.timestamp()-1}),
                           ('raid', {'active': [{'id': 'order66', 'end': now.timestamp()-1}]})]:
            database.set_bot_state('live_game:'+kind+':owner', json.dumps({'at': now.timestamp(), 'data': data}), guild_id=1)
        with patch.object(database, 'get_guild_config', return_value={}):
            context = c.build({'guild_id': 1}, now=now)
        for kind in ('tw', 'raid'):
            self.assertNotIn('ends_at', context[kind])
        plan = a.local_plan('Когда закончится рейд?', context=context)
        self.assertIn('нет', plan['text'])
        self.assertNotIn('не идёт', plan['text'])


class RefreshTests(unittest.IsolatedAsyncioTestCase):
    async def test_explicit_event_reads_own_session_only_and_never_logs_in(self):
        from services import game_account_readonly as reader, game_event_live
        access = {'tier': 'officer', 'guild_id': 1, 'ally_code': '123456789'}
        state = {'active': []}
        with patch.object(reader, 'read', new_callable=AsyncMock, return_value=state) as read, patch.object(reader, 'login_for_events', new_callable=AsyncMock) as login, patch.object(game_event_live, 'save') as save:
            self.assertTrue(await c.refresh_event('owner', access, 'test', 'Когда закончится рейд?', {}))
            read.assert_awaited_once_with('owner', 1, 'test', 'officer', '123456789', event='raid')
            self.assertEqual(save.call_args.args[0:3], (1, 'owner', 'raid'))
            login.assert_not_called()
            read.reset_mock()
            self.assertFalse(await c.refresh_event('owner', access, 'test', 'Пошути про рейд', {}))
            read.assert_not_called()

    async def test_missing_connection_keeps_saved_facts_without_login_fallback(self):
        from services import game_account_readonly as reader, game_account
        with patch.object(reader, 'read', new_callable=AsyncMock, side_effect=game_account.ConnectionRequired('missing')), patch.object(reader, 'login_for_events', new_callable=AsyncMock) as login:
            self.assertFalse(await c.refresh_event('owner', {'tier': 'officer', 'guild_id': 1, 'ally_code': '123456789'}, 'test', 'Сколько очков в рейде?', {}))
            login.assert_not_called()


if __name__ == '__main__':
    unittest.main()
