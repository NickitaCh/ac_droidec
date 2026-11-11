import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.middleware.sessions import SessionMiddleware
from PIL import Image

import database
from services import feature_flags, mod_scheme_image
from services.mod_recommendations import SECONDARIES
from web.routes import stat_plates
from web.deps import require_officer_access


class RecommendationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = patch.object(database, "DB_NAME", str(Path(self.tmp.name) / "test.db"))
        self.db.start()
        database.init_db()
        database.create_stat_plate("Plate", "", "x", guild_id=1)
        database.add_stat_requirement("Plate", "HUX", "Speed", ">=", 300, "required", None, "", "x", guild_id=1)
        self.rows = database.get_stat_requirements("Plate", "HUX", 1)
        self.app = FastAPI()
        self.app.add_middleware(SessionMiddleware, secret_key="test-only")
        self.app.include_router(stat_plates.router, prefix="/plates")
        self.app.dependency_overrides[require_officer_access] = lambda: {"guild_id": 1, "tier": "officer", "discord_id": "1", "features": {}}
        self.client = TestClient(self.app, follow_redirects=False)

    def tearDown(self):
        self.client.close()
        self.db.stop()
        self.tmp.cleanup()

    def test_add_delete_deduplicate_and_leave_requirements_unchanged(self):
        url = "/plates/Plate/characters/HUX/secondary"
        for _ in range(2):
            self.assertEqual(self.client.post(url, data={"stat_id": 41}).status_code, 303)
        self.assertEqual(database.get_mod_secondary_recommendations("Plate", "HUX", 1), [("Plate", 0, 41)])
        self.assertEqual(database.get_stat_requirements("Plate", "HUX", 1), self.rows)
        self.assertEqual(database.count_stat_requirements_by_plate("Plate", 1), 1)
        self.client.post(url, data={"stat_id": 41, "action": "delete"})
        self.assertEqual(database.get_mod_secondary_recommendations("Plate", "HUX", 1), [])

    def test_reject_non_secondary_and_cross_guild_character(self):
        url = "/plates/Plate/characters/HUX/secondary"
        for payload in ({"stat_id": 16}, {"stat_id": 48}, {"stat_id": 5, "scheme_num": 8}, {"stat_id": 5, "scheme_num": 1}):
            self.assertIn("error=", self.client.post(url, data=payload).headers["location"])
        self.client.post("/plates/Missing/characters/HUX/secondary", data={"stat_id": 5})
        self.assertEqual(database.get_mod_secondary_recommendations("Missing", "HUX", 1), [])
        self.assertEqual(database.get_mod_secondary_recommendations("Plate", "HUX", 1), [])
        self.assertEqual(len(SECONDARIES), 8)
        self.assertFalse(any("%" in label for label in SECONDARIES.values()))

    def test_render_editor_shows_recommendations_separately(self):
        database.add_mod_secondary_recommendation("Plate", "HUX", 5, guild_id=1)
        response = self.client.get("/plates/Plate")
        self.assertEqual(response.status_code, 200)
        self.assertIn("Рекомендуемые доп. статы", response.text)
        self.assertIn('name="stat_id"', response.text)
        self.assertIn("На результат проверки не влияет", response.text)

    def test_rename_delete_and_guild_isolation(self):
        database.add_mod_secondary_recommendation("Plate", "HUX", 5, guild_id=1)
        database.add_mod_secondary_recommendation("Plate", "HUX", 41, guild_id=2)
        database.rename_stat_plate("Plate", "New", guild_id=1)
        self.assertEqual(database.get_mod_secondary_recommendations("New", "HUX", 1), [("New", 0, 5)])
        database.delete_stat_requirements_by_character("New", "HUX", 1)
        self.assertEqual(database.get_mod_secondary_recommendations("New", "HUX", 1), [])
        self.assertEqual(database.get_mod_secondary_recommendations("Plate", "HUX", 2), [("Plate", 0, 41)])
        database.delete_stat_plate("Plate", 2)
        self.assertEqual(database.get_mod_secondary_recommendations("Plate", "HUX", 2), [])

    def test_picture_only_adds_block_when_recommendations_exist(self):
        model = dict(name="Хакс", base_id="HUX", plate="Plate", slots={}, requirements=[("Скорость", ">= 300")])
        with patch.object(mod_scheme_image, "_portrait", return_value=None):
            first = Image.open(io.BytesIO(mod_scheme_image.render(model)))
            model["secondaries"] = list(SECONDARIES.values())
            second = Image.open(io.BytesIO(mod_scheme_image.render(model)))
        self.assertGreater(second.height, first.height)

    def test_delete_imported_plate_with_slash_keeps_other_guild(self):
        name = 'AC_CASSIAN · 5/19 · 14 failed'
        for gid in (1, 2):
            database.create_stat_plate(name, "", "x", guild_id=gid)
            database.add_stat_requirement(name, "HUX", "Speed", ">=", 300, "required", None, "", "x", guild_id=gid)
        page = self.client.get('/plates')
        self.assertEqual(page.status_code, 200)
        self.assertIn('action="/plates/delete"', page.text)
        self.assertIn('name="plate_name" value="' + name + '"', page.text)
        response = self.client.post('/plates/delete', data={'plate_name': name})
        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers['location'], '/plates')
        self.assertIsNone(database.get_stat_plate(name, 1))
        self.assertEqual(database.count_stat_requirements_by_plate(name, 1), 0)
        self.assertIsNotNone(database.get_stat_plate(name, 2))
        self.assertEqual(database.count_stat_requirements_by_plate(name, 2), 1)


if __name__ == "__main__":
    unittest.main()
