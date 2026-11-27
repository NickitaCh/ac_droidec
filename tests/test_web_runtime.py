import os
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from fastapi import Depends, FastAPI, Request
from fastapi.testclient import TestClient
from starlette.middleware.sessions import SessionMiddleware

from web import audit
from web.session_settings import session_settings


class SessionSettingsTests(unittest.TestCase):
    def test_production_requires_a_configured_secret(self):
        for values in ({}, {"WEB_ENV": "production"}, {"DISCORD_CLIENT_ID": "configured"},
                       {"WEB_ENV": "production", "WEB_SESSION_SECRET": "dev-insecure-secret-change-me"}):
            with self.subTest(values=values), patch.dict(os.environ, values, clear=True):
                with self.assertRaisesRegex(RuntimeError, "WEB_SESSION_SECRET"):
                    session_settings()

    def test_production_session_cookie_is_secure(self):
        with patch.dict(os.environ, {"WEB_SESSION_SECRET": "synthetic-value"}, clear=True):
            settings = session_settings()
        app = FastAPI()
        app.add_middleware(SessionMiddleware, **settings)

        @app.get("/")
        def set_session(request: Request):
            request.session["test"] = True
            return {}

        with TestClient(app, base_url="https://example.test") as client:
            cookie = client.get("/").headers["set-cookie"].lower()
        self.assertIn("; secure", cookie)
        self.assertIn("; httponly", cookie)

    def test_development_generates_a_private_fallback(self):
        with patch.dict(os.environ, {"WEB_ENV": "development", "DISCORD_CLIENT_ID": "configured"}, clear=True):
            first = session_settings()
            second = session_settings()
        self.assertFalse(first["https_only"])
        self.assertNotEqual(first["secret_key"], second["secret_key"])


class AuditResponsivenessTests(unittest.IsolatedAsyncioTestCase):
    async def test_access_and_audit_write_run_off_the_event_loop(self):
        event_loop_thread = threading.get_ident()
        worker_threads = []

        def resolve_user(request):
            worker_threads.append(threading.get_ident())
            return {"tier": "officer", "guild_id": 1, "discord_id": "1"}

        def write_audit(*args, **kwargs):
            worker_threads.append(threading.get_ident())

        scope = {"type": "http", "method": "POST"}
        request = SimpleNamespace(scope=scope, method="POST", url=SimpleNamespace(path="/tasks/add", query=""),
                                  session={"user": {"discord_id": "1"}}, headers={})

        async def app(scope, receive, send):
            await audit.capture_request(request)

        with patch("web.deps.get_current_user", side_effect=resolve_user), \
                patch("database.log_audit", side_effect=write_audit):
            await audit.AuditMiddleware(app)(scope, None, None)
        self.assertEqual(len(worker_threads), 2)
        self.assertNotIn(event_loop_thread, worker_threads)

    def test_audit_preserves_route_form_limits_and_logs_valid_fields(self):
        app = FastAPI(dependencies=[Depends(audit.capture_request)])
        app.add_middleware(audit.AuditMiddleware)
        app.add_middleware(SessionMiddleware, secret_key="test-session")

        @app.get("/session")
        def set_session(request: Request):
            request.session["user"] = {"discord_id": "1"}
            return {}

        @app.post("/form")
        async def form(request: Request):
            return {"name": (await request.form(max_files=0, max_fields=1)).get("name")}

        @app.post("/json")
        async def body(request: Request):
            await request.json()
            return {}

        with TestClient(app) as client, \
                patch("web.deps.get_current_user", return_value={"tier": "officer", "guild_id": 1, "discord_id": "1"}), \
                patch("database.log_audit") as record:
            client.get("/session")
            rejected = client.post("/form", files={"upload": ("test.txt", b"test")})
            self.assertEqual(rejected.status_code, 400)
            self.assertEqual(record.call_args.kwargs["status"], "400")
            valid = client.post("/form", files={"name": (None, "test")})
            self.assertEqual(valid.json(), {"name": "test"})
            self.assertEqual(record.call_args.kwargs["details"], {"name": "test"})
            client.post("/json?token=synthetic-query", json={"nested": [{"password": "synthetic-body"}]})
            details = record.call_args.kwargs["details"]
            self.assertIn("***", details["nested"])
            self.assertIn("token=%2A%2A%2A", details["?"])
            self.assertNotIn("synthetic", str(details))
