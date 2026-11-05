import asyncio
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, AsyncMock
from types import SimpleNamespace

import database
from cogs.stat_requirements import _char_squad_skip, _missing_plate_squads
from services import task_groups, unit_alignment


class GroupAndSquadTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = patch.object(database, 'DB_NAME', str(Path(self.tmp.name) / 'test.db'))
        self.db.start()
        database.init_db()
        self.gid = database.create_guild(name='Guild', ally_code='123456789', discord_guild_id='123',
                                         member_role_id='', officer_role_id='')
        database.sync_guild_roster(self.gid, [('a','123456789','Alice',3,'Alice'), ('b','223456789','Bob',3,'Bob')])
        database.upsert_player_units('123456789', {'A': {'definitionId':'A:SEVEN_STAR','currentRarity':7,'currentTier':13,'relic':{'currentTier':9}}})
        database.upsert_player_units('223456789', {'A': {'definitionId':'A:SEVEN_STAR','currentRarity':7,'currentTier':13,'relic':{'currentTier':5}}})

    def tearDown(self):
        self.db.stop()
        self.tmp.cleanup()

    def test_named_goal_counts_met_targets_and_reuses_tasks(self):
        reqs = [{'base_id':'A','type':'relic','value':'5'}, {'base_id':'B','type':'stars','value':'7'}]
        _, created = task_groups.create(self.gid,'Raid',reqs,['123456789','223456789'],'2099-01-01','officer')
        self.assertEqual(sum(map(len,created.values())),3)
        self.assertEqual(len(database.get_all_tasks(self.gid)),3)
        group = task_groups.list_groups(self.gid)[0]
        r = task_groups.report(self.gid,group)
        self.assertEqual((r['done'],r['checks'],r['ready']),(1,4,0))
        task_groups.create(self.gid,'Raid 2',reqs,['123456789','223456789'],'2099-01-01','officer')
        self.assertEqual(len(database.get_all_tasks(self.gid)),3)
        task_groups.archive(self.gid+1,group['id'])
        self.assertEqual(len(task_groups.list_groups(self.gid)),2)
        task_groups.archive(self.gid,group['id'])
        self.assertEqual(len(task_groups.list_groups(self.gid)),1)
        self.assertEqual(len(database.get_all_tasks(self.gid)),3)

    def test_group_creation_rolls_back_on_failure(self):
        with patch.object(task_groups,'meets',side_effect=RuntimeError('failure')), self.assertRaises(RuntimeError):
            task_groups.create(self.gid,'Fail',[{'base_id':'A','type':'relic','value':'5'}],['123456789'],'2099-01-01','x')
        self.assertEqual(task_groups.list_groups(self.gid),[])
        self.assertEqual(database.get_all_tasks(self.gid),[])

    def test_earlier_group_deadline_keeps_its_own_reminders(self):
        reqs=[{'base_id':'B','type':'stars','value':'7'}]
        task_groups.create(self.gid,'Later',reqs,['123456789'],'2099-02-01','officer')
        task_groups.create(self.gid,'Earlier',reqs,['123456789'],'2099-01-01','officer')
        self.assertEqual({r[5] for r in database.get_all_tasks(self.gid)}, {'2099-01-01','2099-02-01'})

    def plate(self,name,char):
        database.create_stat_plate(name,'','x',guild_id=self.gid)
        database.add_stat_requirement(name,char,'Speed','>=',300,'required',None,'','x',guild_id=self.gid)

    def test_deadline_changes_are_scoped_and_preserve_shared_goals(self):
        reqs = [{'base_id':'B','type':'stars','value':'7'}]
        gid, _ = task_groups.create(self.gid,'Raid',reqs,['123456789'],'2099-01-01','x')
        self.assertIsNone(task_groups.change_deadline(self.gid+1,'2099-03-01',group_id=gid))
        self.assertEqual(task_groups.change_deadline(self.gid,'2099-02-01',group_id=gid),1)
        self.assertEqual(database.get_all_tasks(self.gid)[0][5],'2099-02-01')
        other, _ = task_groups.create(self.gid,'Other',reqs,['123456789'],'2099-02-01','x')
        self.assertEqual(task_groups.change_deadline(self.gid,'2099-03-01',group_id=gid),0)
        self.assertEqual(database.get_all_tasks(self.gid)[0][5],'2099-02-01')
        self.assertEqual(task_groups.change_deadline(self.gid,'2099-01-15',group_id=other),1)

    def test_batch_deadline_keeps_completed_and_personal_tasks(self):
        ids = [database.add_task('123456789','B','stars','7','2099-01-01','x',guild_id=self.gid,batch_id='batch') for _ in range(3)]
        database.update_task_status(ids[1], 'COMPLETED')
        with sqlite3.connect(database.DB_NAME) as conn:
            conn.execute('UPDATE tasks SET is_personal=1 WHERE task_id=?',(ids[2],))
            conn.execute("UPDATE tasks SET reminder_sent_at='2098-12-31'")
        conn.close()
        self.assertEqual(task_groups.change_deadline(self.gid,'2099-02-01',batch_id='batch'),1)
        self.assertEqual(database.get_task(ids[0])[5],'2099-02-01')
        self.assertEqual(database.get_task(ids[1])[5],'2099-01-01')
        self.assertEqual(database.get_task(ids[2])[5],'2099-01-01')

    def test_character_squad_only_skips_its_character_and_survives_rename(self):
        self.plate('Plate','A')
        database.add_stat_requirement('Plate','B','Speed','>=',300,'required',None,'','x',guild_id=self.gid)
        database.set_stat_character_squad('Plate','A',[{'base_id':'B'}],guild_id=self.gid)
        missing = _missing_plate_squads('123456789',self.gid)
        self.assertTrue(_char_squad_skip('Plate','A',self.gid,missing)[1])
        self.assertEqual(_char_squad_skip('Plate','B',self.gid,missing),(set(),None))
        self.assertEqual(_missing_plate_squads('123456789',self.gid+1),{})
        database.rename_stat_plate('Plate','Renamed',guild_id=self.gid)
        self.assertIn('Renamed\x1fA',database.get_stat_plate_squads(self.gid))
        database.set_stat_character_squad('Renamed','A',[],guild_id=self.gid)
        self.assertEqual(_missing_plate_squads('123456789',self.gid),{})

    def test_alignment_uses_catalog_and_unknown_stays_unclassified(self):
        database.set_bot_state('unit_alignments','{"A":2,"B":3,"C":1}',guild_id=0)
        self.assertEqual([unit_alignment.side(x) for x in ('A','B','C','?')],['light','dark','neutral',None])

    def test_telegram_group_report_is_officer_only(self):
        from tg_bot import more
        ctx = SimpleNamespace(args='группы', access={'guild_id':self.gid,'tier':'member'}, reply=AsyncMock())
        with patch.object(more,'_feature_off',return_value=None), patch.object(task_groups,'list_groups') as listing:
            asyncio.run(more.cmd_tasks(ctx))
        listing.assert_not_called()
        ctx.reply.assert_awaited_once()

    def test_prod_comparison_has_no_personal_rokamora_button(self):
        from cogs.unit_test import CompareSetupView
        from services import bot_profiles
        async def check():
            inter=SimpleNamespace(author=SimpleNamespace(id=1))
            with patch.object(bot_profiles,'current_profile',return_value='prod'):
                view=CompareSetupView(None,inter,self.gid,{})
                self.assertFalse(any('Rokamora' in (getattr(item,'label',None) or '') for item in view.children))
                view.stop()
            with patch.object(bot_profiles,'current_profile',return_value='test'):
                view=CompareSetupView(None,inter,self.gid,{})
                self.assertTrue(any('Rokamora' in (getattr(item,'label',None) or '') for item in view.children))
                view.stop()
        asyncio.run(check())

    def test_group_notifications_follow_assigned_bot_in_shared_web(self):
        from services import bot_profiles
        from web.routes import tasks
        database.update_guild_config(self.gid,tasks_log_channel_id='123',web_both_profiles=1)
        with patch.object(tasks.outbox,'guild_wants',return_value=False), \
             patch.object(tasks.feature_flags,'is_enabled',return_value=True), \
             patch.object(database,'get_discord_id_for_ally',return_value='456'), \
             patch.object(tasks.discord_rest,'send_message',AsyncMock()) as send:
            token=bot_profiles.web_profile.set('prod')
            try:asyncio.run(tasks._mention_new_tasks(self.gid,{'123456789':'Raid'}))
            finally:bot_profiles.web_profile.reset(token)
        self.assertEqual(send.await_args.kwargs['profile'],'test')


if __name__ == '__main__':
    unittest.main()
