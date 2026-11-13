import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.middleware.sessions import SessionMiddleware

import database
from web.deps import require_officer_access
from web.routes import stat_plates


class PlateVisibilityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = patch.object(database, "DB_NAME", str(Path(self.tmp.name) / "test.db"))
        self.db.start()
        database.init_db()
        self.name = "ЛВ / персонаж"
        for gid in (1, 2):
            database.create_stat_plate(self.name, "", "x", guild_id=gid)
            database.add_stat_requirement(self.name, "HUX", "Speed", ">=", 300,
                                          "required", None, "", "x", guild_id=gid)
        database.create_stat_plate("Общий", "", "x", guild_id=1, modular=True)
        database.set_stat_plate_components("Общий", [(self.name, "")], "x", guild_id=1)
        app = FastAPI()
        app.add_middleware(SessionMiddleware, secret_key="test-only")
        app.include_router(stat_plates.router, prefix="/plates")
        app.dependency_overrides[require_officer_access] = lambda: {
            "guild_id": 1, "tier": "officer", "discord_id": "1", "features": {},
        }
        self.client = TestClient(app, follow_redirects=False)

    def tearDown(self):
        self.client.close()
        self.db.stop()
        self.tmp.cleanup()

    def test_switch_preserves_sources_and_other_guild(self):
        before = database.get_stat_requirements("Общий", "HUX", guild_id=1)
        self.assertTrue(before)
        self.assertIn(self.name, database.get_all_stat_requirement_plates(1, bot_visible_only=True))
        response = self.client.post("/plates/bot-visibility", data={"plate_name": self.name})
        self.assertEqual(response.status_code, 303)
        self.assertNotIn(self.name, database.get_all_stat_requirement_plates(1, bot_visible_only=True))
        self.assertIn(self.name, database.get_all_stat_requirement_plates(1))
        self.assertIn(self.name, database.get_all_stat_requirement_plates(2, bot_visible_only=True))
        self.assertEqual(database.get_stat_requirements("Общий", "HUX", guild_id=1), before)
        self.assertIn(self.name, [row[0] for row in database.get_all_stat_plates_detailed(1)])
        page = self.client.get("/plates")
        self.assertEqual(page.status_code, 200)
        self.assertIn('action="/plates/bot-visibility"', page.text)
        self.assertIn("Показывать в боте", page.text)
        response = self.client.post("/plates/bot-visibility", data={"plate_name": self.name, "visible": "true"})
        self.assertEqual(response.status_code, 303)
        self.assertIn(self.name, database.get_all_stat_requirement_plates(1, bot_visible_only=True))

    def test_setting_survives_rename(self):
        database.set_stat_plate_bot_visibility(self.name, False, guild_id=1)
        self.assertTrue(database.rename_stat_plate(self.name, "Новое имя", guild_id=1))
        self.assertNotIn("Новое имя", database.get_all_stat_requirement_plates(1, bot_visible_only=True))


if __name__ == "__main__":
    unittest.main()
