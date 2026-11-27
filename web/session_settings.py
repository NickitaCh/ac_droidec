import os
import secrets


def session_settings():
    configured = os.getenv("WEB_ENV")
    if configured not in (None, "development", "production"):
        raise RuntimeError("WEB_ENV must be development or production")
    production = configured != "development"
    secret = (os.getenv("WEB_SESSION_SECRET") or "").strip()
    if production and (not secret or secret == "dev-insecure-secret-change-me"):
        raise RuntimeError("WEB_SESSION_SECRET must be set in production")
    return {"secret_key": secret or secrets.token_urlsafe(48), "https_only": production}
