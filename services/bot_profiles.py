"""Product profiles; guild data stays in one database.

The web selection is request-local and never changes a guild's assignment.
Workers select BOT_PROFILE=test|prod. Only the test Discord worker owns shared
scheduled syncs; per-guild work belongs to the assigned pair. Tokens never fall
back from prod to test. The web selection is independent of worker identity.
"""

import os
import asyncio
from functools import wraps
from inspect import signature
from contextlib import contextmanager
from contextvars import ContextVar

PROFILES = {"test": "Тест", "prod": "Прод"}
web_profile = ContextVar("web_bot_profile", default=None)
_delivery_profile = ContextVar("delivery_bot_profile", default=None)
_worker_profile = None

PROD_DISABLED = frozenset({
    "fun", "omicron_phrases", "steal_build", "mod_analysis", "mod_compare",
    "mod_scan", "tw_order", "antispam", "mod_optimizer", "mod_ku", "gear_farm",
    "ai_models", "qa_checklist", "datacron_shorts", "unit_aliases", "mod_ku_plates",
})

PROD_BLOCKED_PATHS = (
    "/tw", "/steal-build", "/mod-analysis", "/mod-scan", "/mod-optimizer",
    "/mod-ku", "/farm", "/qa-checklist", "/omicron/phrases", "/admin/fun",
    "/admin/ai-models", "/admin/datacron-shorts", "/admin/unit-aliases",
)


def validate(value):
    if value not in PROFILES:
        raise ValueError("Неизвестная версия бота.")
    return value


def runtime_profile():
    return validate(os.getenv("BOT_PROFILE", "test"))


def activate_worker(profile=None):
    global _worker_profile
    _worker_profile = validate(profile or runtime_profile())
    return _worker_profile


def worker_profile():
    return _worker_profile


def current_profile():
    return _delivery_profile.get() or web_profile.get() or _worker_profile or runtime_profile()


@contextmanager
def delivery_context(profile):
    context = _delivery_profile.set(validate(profile))
    try:
        yield
    finally:
        _delivery_profile.reset(context)


def serves(guild_id):
    return guild_id is None or guild_profile(guild_id) == (worker_profile() or runtime_profile())


def accepts(guild_id, profile=None):
    """Prod accepts both audiences; background delivery still uses serves()."""
    profile = profile or worker_profile() or runtime_profile()
    return guild_id is None or profile == "prod" or guild_profile(guild_id) == profile


def served_guilds(active_only=True):
    import database
    profile = worker_profile() or runtime_profile()
    return [g for g in database.get_all_guild_configs(active_only=active_only)
            if (g.get("bot_profile") or "test") == profile]


def start_shared_loop(loop):
    if (worker_profile() or runtime_profile()) == "test":
        loop.start()


def extension_available(name):
    return (worker_profile() or runtime_profile()) != "prod" or name not in {
        "antispam", "mod_scan", "tw_counters", "guild_compare",
    }


def state_key(key, profile=None):
    profile = validate(profile or current_profile())
    return key if profile == "test" else "prod:" + key


def bot_link(platform, profile):
    import database
    profile = validate(profile)
    if platform == "telegram":
        username = database.get_bot_state(state_key("telegram_bot_username", profile), guild_id=0)
        return "https://t.me/" + username if username else None
    key = "PROD_DISCORD_CLIENT_ID" if profile == "prod" else "DISCORD_CLIENT_ID"
    bot_id = os.getenv(key) or database.get_bot_state(state_key("discord_bot_id", profile), guild_id=0)
    return "https://discord.com/users/" + bot_id if bot_id else None


def mismatch_message(guild_id, platform, *, profile=None):
    # Web access does not depend on the worker profile. Account selection and
    # permissions remain unchanged; never silently switch to an alt account.
    profile = profile or worker_profile()
    if guild_id is None or profile is None or accepts(guild_id, profile):
        return None
    link = bot_link(platform, guild_profile(guild_id)) or "https://swgoh-sng.ru/start"
    return "Этот аккаунт обслуживает другой бот. Откройте нужного бота: " + link


def guild_delivery(func):
    """Keep guild-specific web broadcasts on the assigned pair, including DMs."""
    parameters = signature(func)

    @wraps(func)
    async def wrapped(*args, **kwargs):
        bound = parameters.bind(*args, **kwargs)
        gid = bound.arguments["guild_id"]
        profile = guild_profile(gid)
        mismatch = mismatch_message(gid, "discord")
        if mismatch:
            raise ValueError(mismatch)
        with delivery_context(profile):
            if "token" in bound.arguments and profile == "prod":
                bound.arguments["token"] = token("discord", profile) or ""
            return await func(*bound.args, **bound.kwargs)

    return wrapped


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
    return feature_available(key, guild_profile(guild_id)) and feature_available(key, current_profile())


def path_available(path, profile):
    return profile != "prod" or not any(
        path == prefix or path.startswith(prefix + "/") for prefix in PROD_BLOCKED_PATHS
    )


def token(platform, profile=None):
    profile = validate(profile or current_profile())
    name = {"discord": "DISCORD_TOKEN", "telegram": "TELEGRAM_BOT_TOKEN"}[platform]
    if profile == "prod":
        name = "PROD_" + name
    value = os.getenv(name)
    if profile == "prod" and value and value == os.getenv(name.removeprefix("PROD_")):
        raise ValueError("Публичному боту нужен отдельный токен.")
    return value


def configured(profile):
    return bool(token("discord", profile) and token("telegram", profile))


def production_ready():
    return os.getenv("PROD_BOTS_READY") == "1" and configured("prod")


def public_profile():
    # Operator changes this when public onboarding is ready. Existing guilds
    # keep their explicit assignment regardless of this default.
    return validate(os.getenv("WEB_PUBLIC_PROFILE", "test"))


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


async def transfer_guild(guild_id, profile):
    """Verify destination bots before changing a live guild's assignment."""
    import database
    from services import discord_rest
    from tg_bot import store
    validate(profile)
    cfg = database.get_guild_config(guild_id)
    if not cfg:
        raise ValueError("Гильдия не найдена.")
    if (cfg.get("bot_profile") or "test") == profile:
        return
    if profile == "prod" and not production_ready():
        raise ValueError("Публичные боты ещё не запущены. Перевод гильдии пока недоступен.")
    if cfg.get("discord_guild_id"):
        try:
            available = await discord_rest.list_bot_guilds(profile=profile)
        except Exception:
            raise ValueError("Не удалось проверить доступ нужного Discord-бота.") from None
        if str(cfg["discord_guild_id"]) not in available:
            from services.discord_invite import build_invite_url
            link = build_invite_url(profile) or "https://swgoh-sng.ru/start"
            raise ValueError("Сначала пригласите нужного Discord-бота на сервер: " + link)
    conn = store._connect()
    try:
        chats = [r[0] for r in conn.execute("SELECT chat_id FROM tg_chats WHERE guild_id=?", (guild_id,))]
    finally:
        conn.close()
    if chats:
        from tg_bot.api import TelegramAPI
        api_token = token("telegram", profile)
        if not api_token:
            raise ValueError("Не настроен нужный Telegram-бот.")
        api = TelegramAPI(api_token)
        try:
            me = await api.get_me()
            for chat_id in chats:
                member = await api.call("getChatMember", chat_id=chat_id, user_id=me["id"])
                if member.get("status") not in {"creator", "administrator", "member", "restricted"} or (member.get("status") == "restricted" and not member.get("is_member")):
                    raise ValueError("Сначала добавьте нужного Telegram-бота во все чаты гильдии.")
        except Exception:
            raise ValueError("Нужный Telegram-бот недоступен в одном из чатов гильдии. Добавьте его и повторите перевод.") from None
        finally:
            await api.close()
    assign_guild(guild_id, profile)


class ProfileMiddleware:
    """Inside SessionMiddleware: isolate concurrent admin selections reliably."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope.get("path", "").startswith("/static/"):
            return await self.app(scope, receive, send)
        session = scope.get("session") or {}
        user = session.get("user")
        profile = public_profile()
        if user:
            from web.deps import _refresh_access
            fresh = await asyncio.to_thread(_refresh_access, user)
            selected = session.get("bot_profile") if fresh.get("is_super_admin") else None
            if selected in PROFILES:
                profile = selected
            elif fresh.get("guild_id") is not None:
                profile = guild_profile(fresh["guild_id"])
            elif session.get("login_bot_profile") in PROFILES:
                profile = session["login_bot_profile"]
        context = web_profile.set(profile if profile in PROFILES else None)
        try:
            await self.app(scope, receive, send)
        finally:
            web_profile.reset(context)
