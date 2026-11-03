"""Product profiles; guild data stays in one database.

The web selection is request-local and never changes a guild's assignment.
Worker token selection is reserved here for the next deployment stage.
PROD_BOTS_READY must remain unset until worker routing is implemented and checked.
This stage does not start a second bot or change ownership of scheduled syncs.
"""

import os
from contextvars import ContextVar

PROFILES = {"test": "Тест", "prod": "Прод"}
web_profile = ContextVar("web_bot_profile", default=None)

PROD_DISABLED = frozenset({
    "fun", "omicron_phrases", "steal_build", "mod_analysis", "mod_compare",
    "mod_scan", "tw_order", "antispam", "mod_optimizer", "mod_ku", "gear_farm",
    "ai_models", "qa_checklist",
})

PROD_BLOCKED_PATHS = (
    "/tw", "/steal-build", "/mod-analysis", "/mod-scan", "/mod-optimizer",
    "/mod-ku", "/farm", "/qa-checklist", "/omicron/phrases", "/admin/fun",
    "/admin/ai-models",
)


def validate(value):
    if value not in PROFILES:
        raise ValueError("Неизвестная версия бота.")
    return value


def runtime_profile():
    return validate(os.getenv("BOT_PROFILE", "test"))


def guild_profile(guild_id):
    if guild_id is None:
        return "test"
    import database
    cfg = database.get_guild_config(guild_id)
    return (cfg or {}).get("bot_profile") or "test"


def feature_available(key, profile):
    return validate(profile) != "prod" or key not in PROD_DISABLED


def feature_for_guild(key, guild_id):
    if key not in PROD_DISABLED:
        return True
    return feature_available(key, guild_profile(guild_id))


def path_available(path, profile):
    return profile != "prod" or not any(
        path == prefix or path.startswith(prefix + "/") for prefix in PROD_BLOCKED_PATHS
    )


def token(platform, profile=None):
    profile = validate(profile or runtime_profile())
    name = {"discord": "DISCORD_TOKEN", "telegram": "TELEGRAM_BOT_TOKEN"}[platform]
    if profile == "prod":
        name = "PROD_" + name
    return os.getenv(name)


def configured(profile):
    return bool(token("discord", profile) and token("telegram", profile))


def production_ready():
    return os.getenv("PROD_BOTS_READY") == "1" and configured("prod")


def assign_guild(guild_id, profile):
    """Only the explicit super-admin route calls this; never alliance membership."""
    import database
    validate(profile)
    if not database.get_guild_config(guild_id):
        raise ValueError("Гильдия не найдена.")
    # Deployment enables this only after routing and both public workers are ready.
    if profile == "prod" and os.getenv("PROD_BOTS_READY") != "1":
        raise ValueError("Публичные боты ещё не запущены. Перевод гильдии пока недоступен.")
    if profile == "prod" and not configured(profile):
        raise ValueError("Не настроены токены публичных ботов.")
    database.update_guild_config(guild_id, bot_profile=profile)


class ProfileMiddleware:
    """Inside SessionMiddleware: isolate concurrent admin selections reliably."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        profile = (scope.get("session") or {}).get("bot_profile")
        if profile not in PROFILES and (scope.get("session") or {}).get("user"):
            profile = guild_profile(scope["session"]["user"].get("guild_id"))
        context = web_profile.set(profile if profile in PROFILES else None)
        try:
            await self.app(scope, receive, send)
        finally:
            web_profile.reset(context)
