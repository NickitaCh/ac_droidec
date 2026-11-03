import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.middleware.sessions import SessionMiddleware

import database
from services import alliances
from web.deps import require_officer_access
from web.routes.alliances import router


class AllianceWebTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = patch.object(database, "DB_NAME", str(Path(self.tmp.name) / "test.db"))
        self.db.start()
        database.init_db()
        self.gid = database.create_guild(name="Наша гильдия", ally_code="123456789", discord_guild_id="1", member_role_id="", officer_role_id="")
        self.user = {"tier": "officer", "guild_id": self.gid, "discord_id": "1", "username": "Officer", "features": {}}
        self.app = FastAPI()
        self.app.add_middleware(SessionMiddleware, secret_key="alliance-test-only")
        self.app.include_router(router, prefix="/alliances")
        self.app.dependency_overrides[require_officer_access] = lambda: self.user
        self.client = TestClient(self.app)

    def tearDown(self):
        self.client.close()
        self.db.stop()
        self.tmp.cleanup()

    def test_create_and_render_do_not_need_comlink(self):
        response = self.client.get("/alliances")
        self.assertEqual(response.status_code, 200)
        self.assertIn("Создать альянс", response.text)
        response = self.client.post("/alliances/create", data={"name": "<Наш альянс>"}, follow_redirects=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn("&lt;Наш альянс&gt;", response.text)
        self.assertIn("Доступно нашей гильдии", response.text)
        self.assertIn("Всем гильдиям альянса, включая будущие", response.text)

    def test_mutations_require_officer_and_trusted_origin(self):
        self.assertEqual(self.client.post("/alliances/create", data={"name": "X"}, headers={"Origin": "https://foreign.example"}).status_code, 403)
        self.app.dependency_overrides.clear()
        self.assertEqual(self.client.get("/alliances").status_code, 401)
        self.assertEqual(self.client.post("/alliances/create", data={"name": "X"}).status_code, 401)
        with patch("web.deps.get_current_user", return_value={"tier": "member", "guild_id": self.gid}):
            self.assertEqual(self.client.get("/alliances").status_code, 403)
            self.assertEqual(self.client.post("/alliances/create", data={"name": "X"}).status_code, 403)

    def test_share_preview_and_post_errors_are_escaped(self):
        alliances.create(self.gid, "Test", "1")
        other = database.create_guild(name="Другие", ally_code="234567891", discord_guild_id="2", member_role_id="", officer_role_id="")
        alliances.invite(self.gid, other, "1")
        alliances.respond(other, alliances.page(other)["incoming"][0]["id"], True)
        database.create_stat_plate("Тестовый", "<script>bad</script>", "1", self.gid)
        sid = alliances.share(self.gid, "plate", "Тестовый", [other], True, True)
        response = self.client.get("/alliances/shared/%s" % sid)
        self.assertEqual(response.status_code, 200)
        self.assertIn("&lt;script&gt;bad&lt;/script&gt;", response.text)
        self.assertNotIn("<script>bad</script>", response.text)
        self.assertEqual(self.client.post("/alliances/consume", data={"share_id": "wrong"}, follow_redirects=True).status_code, 200)


if __name__ == "__main__":
    unittest.main()
