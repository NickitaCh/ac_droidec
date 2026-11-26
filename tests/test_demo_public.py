import os
import random
import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from jinja2 import Environment, FileSystemLoader

import database
from services import demo_data, demo_mode
from services import raid_planner


class PublicDemoTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = str(Path(self.temp.name) / "demo.db")
        self.db_patch = patch.object(database, "DB_NAME", self.db)
        self.db_patch.start()
        database.init_db()
        database.get_guild_config(900)
        database.set_user_registration(demo_mode.DEMO_DISCORD_ID, "123456789", "Демо-лидер", guild_id=900)

    def tearDown(self):
        self.db_patch.stop()
        self.temp.cleanup()

    def test_demo_uses_public_profile_and_its_registered_account(self):
        with patch.dict(os.environ, {"WEB_PUBLIC_PROFILE": "test"}):
            user = demo_mode.demo_user()
        self.assertEqual(user["bot_profile"], "prod")
        self.assertFalse(user["is_super_admin"])
        self.assertEqual(user["active_ally_code"], "123456789")
        self.assertEqual(user["game_accounts"]["123456789"], ("Демо-лидер", True))

    def test_demo_menu_keeps_public_workflows_without_account_only_links(self):
        env = Environment(loader=FileSystemLoader(Path(__file__).resolve().parents[1] / "web/templates"))
        template = env.from_string('{% extends "base.html" %}{% block content %}Demo{% endblock %}')
        request = SimpleNamespace(url=SimpleNamespace(path="/", hostname="localhost"),
                                  scope={}, query_params={}, app=SimpleNamespace(state=SimpleNamespace()))
        user = demo_mode.demo_user()
        page = template.render(request=request, user=user)
        for path in ('href="/tb/live"', 'href="/game-account/events/tw"',
                     'href="/game-account/personal/arena"', 'href="/game-account/modules"'):
            self.assertNotIn(path, page)
        for path in ('href="/tw/def/plans"', 'href="/raids"', 'href="/tb/plan"', 'href="/conquest"'):
            self.assertIn(path, page)
        user["is_demo"] = False
        normal_page = template.render(request=request, user=user)
        self.assertIn('href="/tb/live"', normal_page)
        self.assertIn('href="/game-account/events/tw"', normal_page)

    def test_current_sections_seed_events_with_demo_identities(self):
        with closing(sqlite3.connect(self.db)) as conn, closing(sqlite3.connect(":memory:")) as source:
            database._ensure_raid_results_tables(source.cursor())
            source.execute("INSERT INTO raid_results VALUES (1,'source','raid-catalog-id',0,1,100)")
            units = ["EMPERORPALPATINE", "MARAJADE", "VADER", "GRANDADMIRALTHRAWN", "ROYALGUARD"]
            members = [{"discord_id": demo_mode.DEMO_DISCORD_ID, "pid": "invented-player",
                        "code": "123456789", "name": "Демо-лидер", "strength": .8}]
            roster = {unit: {"currentRarity": 7, "currentTier": 13, "relic": {"currentTier": 9}} for unit in units}
            demo_data._current_sections(source, conn, random.Random(1), datetime.now(timezone.utc),
                                        members, {"123456789": roster}, {unit: [] for unit in units})
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM raid_results WHERE guild_id=900").fetchone()[0], 8)
            self.assertEqual(conn.execute("SELECT DISTINCT player_id FROM raid_member_scores").fetchall(), [("invented-player",)])
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM tw_def_assignments WHERE guild_id=900").fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM tb_order_phases").fetchone()[0], 6)
            self.assertEqual(conn.execute("SELECT stars FROM tb_calc_plans WHERE guild_id=900").fetchone()[0], 36)

    def test_raid_turn_order_accepts_catalogue_variants_for_the_current_lineup(self):
        run = {"id": "isb", "tier": 5, "units": ["DEDRA", "PARTAGAZ", "MAUL"]}
        variants = [{"tiers": [7, 7], "units": ["PARTAGAZ", "DEDRA"]},
                    {"tiers": [0, 6], "only": ["MAUL"], "units": ["DEDRA", "PARTAGAZ"]}]
        self.assertEqual(raid_planner.turn_order(run, {"turn_order": {"isb": variants}}), ["DEDRA", "PARTAGAZ"])
        self.assertEqual(raid_planner.turn_order({**run, "units": ["DEDRA"]}, {"turn_order": {"isb": variants}}), [])
        self.assertEqual(raid_planner.turn_order(run, {"turn_order": {"isb": {"units": ["PARTAGAZ", "DEDRA"]}}}), ["PARTAGAZ", "DEDRA"])


if __name__ == "__main__":
    unittest.main()
