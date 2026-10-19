"""Демо-режим сайта для неавторизованных: кнопка «Посмотреть демо» на главной → сессия
demo → весь сайт работает как у офицера выдуманной гильдии, но читает ОТДЕЛЬНУЮ базу
demo.db (собирается services/demo_data.py, реальных гильдий и игроков там нет).

Как переключается база: database.DB_NAME (и все sqlite3.connect(database.DB_NAME) в
cogs/web) получает os.PathLike-объект, который отдаёт путь demo.db, пока в текущем
контексте (contextvars) выставлен флаг демо, иначе — обычную базу. Флаг ставит
DemoMiddleware на запрос демо-сессии; asyncio.to_thread и потоки FastAPI копируют
контекст, так что флаг доходит и до синхронных зависимостей и to_thread-вызовов.
Устанавливается только в веб-процессе (web/app.py) — у бота DB_NAME обычная строка.

Comlink в демо: get_player/get_guild отвечают из demo.db (кэш ростеров выдуманных
игроков), а не ходят в игру. Игровые справочники (get_game_data и т.п.) — как обычно.

Запись: в демо разрешены только GET/HEAD; POST и прочее — редирект назад с плашкой
«в демо изменения не сохраняются» (или 403 JSON для fetch)."""

import contextvars
import json
import os
from pathlib import Path

DEMO_GUILD_ID = 900
DEMO_DB_NAME = "demo.db"
DEMO_DISCORD_ID = "900000000000000001"

_active: contextvars.ContextVar[bool] = contextvars.ContextVar("demo_active", default=False)
_installed = False


def active() -> bool:
    return _active.get()


def demo_db_path(real_db: str) -> str:
    return str(Path(real_db).resolve().parent / DEMO_DB_NAME)


def demo_db_ready(real_db: str) -> bool:
    return Path(demo_db_path(real_db)).exists()


class _DbPath(os.PathLike):
    """Путь к БД, зависящий от контекста запроса: демо-сессия → demo.db."""

    def __init__(self, real: str):
        self.real = real
        self.demo = demo_db_path(real)

    def __fspath__(self) -> str:
        return self.demo if _active.get() else self.real

    def __str__(self) -> str:
        return self.__fspath__()

    def __repr__(self) -> str:
        return f"_DbPath({self.__fspath__()!r})"


def demo_user() -> dict:
    return {
        "discord_id": DEMO_DISCORD_ID,
        "username": "Гость (демо)",
        "guild_id": DEMO_GUILD_ID,
        "tier": "officer",
        "is_super_admin": False,
        "is_demo": True,
    }


# ---- Comlink: игроки и гильдия демо — из demo.db ----

def _fake_player(allycode) -> dict:
    import database
    code = str(allycode or "").replace("-", "")
    units = database.get_player_units(code)
    if not units:
        raise RuntimeError(f"В демо нет игрока {code}")
    names = database.get_player_names(DEMO_GUILD_ID)
    mapping = {a: pid for _, a, _, _, pid in _mappings()}
    datacrons = database.get_player_datacrons_bulk([code]).get(code) or []
    return {
        "name": names.get(code, code),
        "allyCode": code,
        "playerId": mapping.get(code, ""),
        "guildId": "demo",
        "guildName": database.get_guild_config(DEMO_GUILD_ID)["name"],
        "rosterUnit": list(units.values()),
        "datacron": datacrons,
        "profileStat": [],
        "pvpProfile": [],
    }


def _mappings() -> list:
    import sqlite3
    import database
    conn = sqlite3.connect(database.DB_NAME)
    try:
        return conn.execute(
            "SELECT guild_id, ally_code, ingame_name, member_level, comlink_player_id FROM user_mapping WHERE guild_id = ?",
            (DEMO_GUILD_ID,),
        ).fetchall()
    finally:
        conn.close()


def _fake_guild() -> dict:
    import database
    cfg = database.get_guild_config(DEMO_GUILD_ID) or {}
    members = [
        {"playerId": pid, "playerName": name, "memberLevel": level, "allyCode": code}
        for _, code, name, level, pid in _mappings()
    ]
    return {"guild": {"profile": {"id": "demo", "name": cfg.get("name", "Демо"), "memberCount": len(members)},
                      "member": members}}


def _patch_comlink():
    from swgoh_comlink import SwgohComlink
    orig_player, orig_guild = SwgohComlink.get_player, SwgohComlink.get_guild

    def get_player(self, *args, **kwargs):
        if not _active.get():
            return orig_player(self, *args, **kwargs)
        return _fake_player(kwargs.get("allycode") or kwargs.get("ally_code") or (args[0] if args else None))

    def get_guild(self, *args, **kwargs):
        if not _active.get():
            return orig_guild(self, *args, **kwargs)
        return _fake_guild()

    SwgohComlink.get_player = get_player
    SwgohComlink.get_guild = get_guild


def install():
    """Вызывается один раз из web/app.py. Бот это не вызывает."""
    global _installed
    if _installed:
        return
    import database
    database.DB_NAME = _DbPath(str(database.DB_NAME))
    _patch_comlink()
    _installed = True


# ---- ASGI middleware ----

_WRITE_ALLOWED = ("/demo", "/demo/exit", "/logout")


class DemoMiddleware:
    """Ставит флаг демо на запросы демо-сессии и не пускает изменения. Должен стоять
    ВНУТРИ SessionMiddleware (в web/app.py добавляется раньше него)."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        session = scope.get("session") or {}
        if not session.get("demo") or session.get("user"):
            return await self.app(scope, receive, send)
        token = _active.set(True)
        try:
            method = scope.get("method", "GET")
            if method not in ("GET", "HEAD") and scope.get("path") not in _WRITE_ALLOWED:
                return await _deny_write(scope, send)
            return await self.app(scope, receive, send)
        finally:
            _active.reset(token)


async def _deny_write(scope, send):
    headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}
    wants_json = "application/json" in headers.get("accept", "") or headers.get("x-requested-with") == "fetch"
    if wants_json or "json" in headers.get("content-type", ""):
        body = json.dumps({"error": "Это демо — изменения не сохраняются."}, ensure_ascii=False).encode()
        await send({"type": "http.response.start", "status": 403,
                    "headers": [(b"content-type", b"application/json; charset=utf-8")]})
        await send({"type": "http.response.body", "body": body})
        return
    referer = headers.get("referer") or "/"
    sep = "&" if "?" in referer else "?"
    location = f"{referer.split('#')[0]}{sep}demo_readonly=1"
    await send({"type": "http.response.start", "status": 303,
                "headers": [(b"location", location.encode("latin-1", "ignore"))]})
    await send({"type": "http.response.body", "body": b""})
