import copy
import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.middleware.sessions import SessionMiddleware

import database
import tw_def_engine as engine
from services import tw_workspace as work, tw_def_notify
from web.routes import tw_defense as web


class WorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_patch = patch.object(database, "DB_NAME", str(Path(self.tmp.name) / "tw.db"))
        self.db_patch.start()
        database.init_db()
        self.gid = database.create_guild(name="Тест", ally_code="123456789", discord_guild_id="1", member_role_id="", officer_role_id="")
        database.sync_guild_roster(self.gid, [("d1", "111", "Первый", 2, "p1"), ("d2", "222", "Второй", 2, "p2")])
        self.plan_id = database.create_tw_def_plan(self.gid, "<План>", 2, 1, "test")
        self.squad_id = database.save_tw_def_squad(self.gid, None, "Рей", "character", [{"options": [{"base_id": "A"}]}], None, "test")
        self.other_id = database.save_tw_def_squad(self.gid, None, "Люк", "character", [{"options": [{"base_id": "B"}]}], None, "test")
        self.roster = engine.Roster(["111", "222"], {"111": "Первый", "222": "Второй"},
            {"111": {"A": {"currentRarity": 7}, "B": {"currentRarity": 7}}, "222": {"A": {"currentRarity": 7}}},
            {"A": "Рей", "B": "Люк"}, {"A": "character", "B": "character"}, {})
        self.user = {"tier": "officer", "guild_id": self.gid, "guild_name": "Тест", "discord_id": "1", "bot_profile": "test"}
        self.app = FastAPI()
        self.app.add_middleware(SessionMiddleware, secret_key="test")
        self.app.include_router(web.router, prefix="/tw/def")
        self.app.dependency_overrides[web._require] = lambda: self.user
        self.client = TestClient(self.app)
        self.load_patch = patch.object(web, "_load_roster", AsyncMock(return_value=(self.roster, None)))
        self.warm_patch = patch.object(web, "_warm_stat_calc")
        self.stats_patch = patch.object(web, "_stats_not_ready", return_value=False)
        self.load_patch.start()
        self.warm_patch.start()
        self.stats_patch.start()

    def tearDown(self):
        self.client.close()
        self.stats_patch.stop()
        self.warm_patch.stop()
        self.load_patch.stop()
        self.db_patch.stop()
        self.tmp.cleanup()

    def post(self, action, data=None, plan_id=None):
        pid = plan_id or self.plan_id
        return self.client.post(f"/tw/def/plans/{pid}/{action}",
            data={"revision": work.revision(self.gid, pid), **(data or {})}, follow_redirects=False)

    def test_render_all_views_and_workflow_without_network(self):
        for url in [f"/tw/def/plans/{self.plan_id}", f"/tw/def/plans/{self.plan_id}?view=players",
                    f"/tw/def/plans/{self.plan_id}?view=problems", f"/tw/def/plans/{self.plan_id}/strategy",
                    f"/tw/def/plans/{self.plan_id}/attack", "/tw/def/command", "/tw/def/plans"]:
            response = self.client.get(url)
            self.assertEqual(response.status_code, 200, url)
            self.assertIn("&lt;План&gt;", response.text)
        response = self.client.get(f"/tw/def/plans/{self.plan_id}")
        self.assertEqual(response.text.count('data-tw-zone="'), 10)
        self.assertEqual(response.text.count('data-tw-view-panel="'), 3)

    def test_squad_editor_round_trips_alternative_datacrons_and_soft_skills(self):
        import json
        options = [{'value': 'focused:1', 'label': 'Season A'}, {'value': 'base:2', 'label': 'Season B'}]
        with patch.object(web, '_dc_options', return_value=options), \
             patch.object(database, 'get_unit_types', return_value={'A': 'character'}), \
             patch.object(database, 'get_omicron_capable', return_value=set()):
            data = {'squad_id': str(self.squad_id), 'name': 'Test', 'combat_type': 'character',
                    'slots_json': json.dumps([{'options': [{'base_id': 'A', 'zeta': True,
                                                          'ignore_skills_placement': True}]}]),
                    'datacron': ['focused:1', 'base:2'], 'datacron_required': '1'}
            response = self.client.post('/tw/def/squads/save', data=data, follow_redirects=False)
            self.assertEqual(response.status_code, 303)
            saved = database.get_tw_def_squad(self.gid, self.squad_id)
            self.assertEqual(len(saved['datacron']['alternatives']), 2)
            self.assertTrue(saved['slots'][0]['options'][0]['ignore_skills_placement'])
            response = self.client.get(f'/tw/def/squads/{self.squad_id}/edit')
            self.assertEqual(response.status_code, 200)
            for value in ('focused:1', 'base:2'):
                self.assertIn(f'value="{value}" checked', response.text)
            data.pop('datacron')
            response = self.client.post('/tw/def/squads/save', data=data, follow_redirects=False)
            self.assertEqual(response.status_code, 303)
            self.assertIsNone(database.get_tw_def_squad(self.gid, self.squad_id)['datacron'])

    def test_stale_writes_and_concurrent_writer_rejected(self):
        rev = work.revision(self.gid, self.plan_id)
        self.assertEqual(self.post("strategy", {"zone": "1t", "squad_id": self.squad_id, "count": "1"}).status_code, 303)
        response = self.client.post(f"/tw/def/plans/{self.plan_id}/clear", data={"revision": rev})
        self.assertEqual(response.status_code, 409)
        token = work.acquire(self.gid, self.plan_id)
        try:
            self.assertEqual(self.post("clear").status_code, 409)
        finally:
            work.release(self.gid, self.plan_id, token)
        self.assertEqual(self.post("clear").status_code, 303)

    def test_guild_boundary_and_origin(self):
        self.assertEqual(self.client.post(f"/tw/def/plans/{self.plan_id}/clear", headers={"Origin": "https://foreign.invalid"}).status_code, 403)
        foreign = database.create_tw_def_plan(999, "Чужой", 1, 1, "other")
        self.assertEqual(self.client.get(f"/tw/def/plans/{foreign}").status_code, 404)
        self.assertEqual(self.client.post(f"/tw/def/plans/{foreign}/clear").status_code, 404)
        self.app.dependency_overrides.clear()
        self.assertEqual(self.client.get(f"/tw/def/plans/{self.plan_id}/strategy").status_code, 401)

    def test_reserve_blocks_manual_assignment_and_autopick(self):
        self.assertEqual(self.post("reserve", {"ally_code": "111", "squad_id": self.squad_id}).status_code, 303)
        response = self.post("assign", {"zone": "1t", "squad_id": self.squad_id, "ally_codes": "111"})
        self.assertEqual(response.status_code, 303)
        self.assertEqual(database.get_tw_def_assignments(self.gid, self.plan_id), [])
        self.post("autoadd", {"zone": "1t", "squad_id": self.squad_id, "count": "2"})
        rows = database.get_tw_def_assignments(self.gid, self.plan_id)
        self.assertEqual([r["ally_code"] for r in rows], ["222"])

    def test_preview_does_not_write_and_apply_is_single_use(self):
        self.post("strategy", {"zone": "1t", "squad_id": self.squad_id, "count": "2"})
        response = self.post("preview")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(database.get_tw_def_assignments(self.gid, self.plan_id), [])
        ctx = response.context
        data = {"token": ctx["token"], "revision": ctx["revision"]}
        self.assertEqual(self.post("apply", data).status_code, 303)
        self.assertEqual(len(database.get_tw_def_assignments(self.gid, self.plan_id)), 2)
        self.assertEqual(self.post("apply", data).status_code, 409)

    def test_preview_rejects_changed_roster_and_library(self):
        self.post("strategy", {"zone": "1t", "squad_id": self.squad_id, "count": "1"})
        ctx = self.post("preview").context
        self.roster.units["111"]["A"]["currentRarity"] = 6
        self.assertEqual(self.post("apply", {"token": ctx["token"]}).status_code, 409)
        ctx = self.post("preview").context
        database.save_tw_def_squad(self.gid, self.squad_id, "Изменено", "character", [{"options": [{"base_id": "A", "min_stars": 7}]}], None, "test")
        self.assertEqual(self.post("apply", {"token": ctx["token"]}).status_code, 409)

    def test_history_restores_assignments_and_strategy(self):
        self.post("assign", {"zone": "1t", "squad_id": self.squad_id, "ally_codes": "111"})
        old = work.versions(self.gid, self.plan_id)[0]["id"]
        self.post("strategy", {"zone": "1b", "squad_id": self.other_id, "count": "1"})
        self.post("clear")
        self.assertEqual(self.post("restore", {"version_id": old}).status_code, 303)
        rows = database.get_tw_def_assignments(self.gid, self.plan_id)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["units"], ["A"])
        self.assertEqual(work.workspace(self.gid, self.plan_id)["targets"], [])

    def test_audit_checks_saved_units_and_reserves_not_alternative_match(self):
        plan = database.get_tw_def_plan(self.gid, self.plan_id)
        squads = database.list_tw_def_squads(self.gid)
        ws = work.workspace(self.gid, self.plan_id)
        ws["reserves"] = [{"ally_code": "111", "squad_id": self.squad_id}]
        rows = [{"id": 1, "zone": "1t", "squad_id": self.squad_id, "squad_name": "Рей", "ally_code": "111", "units": ["A"]},
                {"id": 2, "zone": "1b", "squad_id": self.squad_id, "squad_name": "Рей", "ally_code": "111", "units": ["A"]}]
        issues = work.audit(plan, rows, ws, self.roster, squads)
        self.assertEqual(len(issues), 2)
        self.assertIn("один юнит назначен несколько раз", issues[0]["reasons"])
        self.assertIn("использован юнит из резерва атаки", issues[0]["reasons"])
        next(s for s in squads if s["id"] == self.squad_id)["slots"] = [{"options": [{"base_id": "B"}]}]
        issues = work.audit(plan, rows[:1], ws, self.roster, squads)
        self.assertTrue(any("больше не входит" in r for r in issues[0]["reasons"]))

    def test_delivery_changes_are_per_channel_and_withdrawals_have_text(self):
        row = {"ally_code": "111", "entries": [{"zone": "1t"}]}
        work.record_delivery(self.gid, self.plan_id, row, "discord", "sent", "ok")
        same = work.annotate_deliveries(self.gid, self.plan_id, [copy.deepcopy(row)], ["discord"])[0]
        self.assertFalse(same["changed"])
        self.assertTrue(work.annotate_deliveries(self.gid, self.plan_id, [copy.deepcopy(row)], ["tg"])[0]["changed"])
        withdrawn = {"ally_code": "111", "name": "Первый", "entries": []}
        self.assertTrue(work.annotate_deliveries(self.gid, self.plan_id, [withdrawn], ["discord"])[0]["changed"])
        self.assertIn("назначения в защиту по этому плану сняты", "\n".join(tw_def_notify.format_dm("Гильдия", "План", [withdrawn])))

    def test_withdrawn_recipient_is_retained_after_clearing_plan(self):
        work.record_delivery(self.gid, self.plan_id, {"ally_code": "111", "entries": [{"zone": "1t"}]}, "discord", "sent", "ok")
        rows = tw_def_notify.build_player_rows(self.gid, database.get_tw_def_plan(self.gid, self.plan_id), None)
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0]["withdrawn"])
        self.assertEqual(rows[0]["entries"], [])

    def test_notification_records_channel_snapshot_without_live_delivery(self):
        row = {"ally_code": "111", "name": "Первый", "discord_id": "1", "entries": []}
        with patch.object(tw_def_notify, "build_player_rows", return_value=[row]), \
             patch.object(tw_def_notify.tb_platoon_notify, "send_dm", AsyncMock(return_value=(True, "ok"))):
            asyncio.run(tw_def_notify.send_broadcast("", self.gid,
                database.get_tw_def_plan(self.gid, self.plan_id), None, {"111"}, "tg_only"))
        deliveries = work.delivery_rows(self.gid, self.plan_id)
        self.assertEqual(len(deliveries), 1)
        self.assertEqual(deliveries[0]["channel"], "tg")
        self.assertEqual(deliveries[0]["status"], "queued")
        self.assertEqual(deliveries[0]["fingerprint"], work.digest([]))

    def test_template_and_copy_keep_strategy_without_campaign_or_attack_reservations(self):
        self.post("strategy", {"zone": "1t", "squad_id": self.squad_id, "count": "1"})
        self.post("reserve", {"ally_code": "111", "squad_id": self.other_id})
        self.assertEqual(self.post("template-save", {"name": "Шаблон"}).status_code, 303)
        template = work.templates(self.gid)[0]
        fresh = database.create_tw_def_plan(self.gid, "Следующая", 5, 5, "test")
        self.assertEqual(self.post("template-apply", {"template_id": template["id"]}, plan_id=fresh).status_code, 303)
        ws = work.workspace(self.gid, fresh)
        self.assertEqual(len(ws["targets"]), 1)
        self.assertEqual(ws["reserves"], [])
        self.assertEqual(database.get_tw_def_assignments(self.gid, fresh), [])
        self.assertEqual(database.get_tw_def_plan(self.gid, fresh)["squads_per_zone"], 2)
        response = self.post("copy")
        copied_id = int(response.headers["location"].rsplit("/", 1)[-1])
        self.assertEqual(len(work.workspace(self.gid, copied_id)["targets"]), 1)
        self.assertEqual(work.workspace(self.gid, copied_id)["reserves"], [])

    def test_attack_reserve_keeps_required_datacron_out_of_defense(self):
        dc = {"kind": "focused", "req_id": 99, "required": True}
        self.roster.dc_reqs[("focused", 99)] = (99, 22, "", "shin", 9)
        self.roster.datacrons["111"] = [{"id": "owned", "setId": 22, "focused": True,
            "templateId": "datacron_set_22_focused_shin", "affix": [{}] * 9}]
        squads = database.list_tw_def_squads(self.gid)
        next(s for s in squads if s["id"] == self.squad_id)["datacron"] = dc
        other = next(s for s in squads if s["id"] == self.other_id)
        other["datacron"] = dc
        ws = work.workspace(self.gid, self.plan_id)
        ws["reserves"] = [{"ally_code": "111", "squad_id": self.squad_id}]
        reserves = work.reserve_assignments(ws, self.roster, squads)
        self.assertEqual(reserves[0]["datacron_id"], "owned")
        candidates = engine.evaluate_candidates(self.roster, other, reserves)
        self.assertEqual(next(c for c in candidates if c["ally_code"] == "111")["status"], "used")


if __name__ == "__main__":
    unittest.main()
