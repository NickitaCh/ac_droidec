"""Discord REST от имени бота для веб-процесса (у веба нет шлюза disnake): отправка
сообщения в канал и список текстовых каналов сервера. Токен — DISCORD_TOKEN из .env."""

import os

import httpx

DISCORD_API = "https://discord.com/api/v10"
# Типы каналов, куда бот может писать: текстовый, новостной, форум (для гайдов ВГ).
TEXT_CHANNEL_TYPES = {0: "text", 5: "news", 15: "forum"}


class DiscordRestError(Exception):
    pass


def _headers() -> dict:
    token = os.environ.get("DISCORD_TOKEN")
    if not token:
        raise DiscordRestError("DISCORD_TOKEN не настроен на сервере.")
    return {"Authorization": f"Bot {token}", "User-Agent": "DiscordBot (https://swgoh-sng.ru, 1.0)"}


def chunk_lines(header: str, lines: list[str], limit: int = 1900) -> list[str]:
    """Строки → сообщения не длиннее limit (Discord режет на 2000)."""
    chunks, current = [], header
    for line in lines:
        if len(current) + len(line) + 1 > limit:
            chunks.append(current)
            current = ""
        current = f"{current}\n{line}" if current else line
    if current:
        chunks.append(current)
    return chunks


async def send_message(channel_id: str, content: str, mention_user_ids: list[str] | None = None) -> None:
    payload = {"content": content, "allowed_mentions": {"parse": [], "users": list(mention_user_ids or [])[:100]}}
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(f"{DISCORD_API}/channels/{channel_id}/messages", headers=_headers(), json=payload)
    if resp.status_code >= 400:
        raise DiscordRestError(f"Discord ответил {resp.status_code}: {resp.text[:200]}")


async def list_guild_channels(discord_guild_id: str) -> list[dict]:
    """[{id, name, type, category}] — каналы сервера, куда бот может писать, в порядке
    как в Discord (категория, затем позиция)."""
    from services import demo_mode
    if demo_mode.active():  # демо: выдуманный сервер — каналы из реестра demo.db, без Discord
        import database
        return [{"id": c["channel_id"], "name": c["channel_name"], "type": "text", "category": "Демо"}
                for c in database.get_guild_channels(demo_mode.DEMO_GUILD_ID)]
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(f"{DISCORD_API}/guilds/{discord_guild_id}/channels", headers=_headers())
    if resp.status_code >= 400:
        raise DiscordRestError(f"Discord ответил {resp.status_code}: {resp.text[:200]}")
    raw = resp.json()
    categories = {c["id"]: (c.get("position", 0), c.get("name", "")) for c in raw if c.get("type") == 4}
    channels = [
        {"id": c["id"], "name": c.get("name", ""), "type": TEXT_CHANNEL_TYPES[c["type"]],
         "category": categories.get(c.get("parent_id"), (-1, ""))[1],
         "_order": (categories.get(c.get("parent_id"), (-1, ""))[0], c.get("position", 0))}
        for c in raw if c.get("type") in TEXT_CHANNEL_TYPES
    ]
    channels.sort(key=lambda c: c.pop("_order"))
    return channels


async def find_member_id(discord_guild_ids: list[str], query: str) -> str | None:
    """ID пользователя по нику (username, глобальное имя или ник на сервере) — поиск по
    серверам гильдий бота, совпадение без учёта регистра. Нужен, когда в форму вписали
    ник вместо числового ID (так появились гранты, которые ни с кем не совпадали)."""
    needle = query.strip().lstrip("@").lower()
    if not needle:
        return None
    async with httpx.AsyncClient(timeout=10.0) as client:
        for gid in discord_guild_ids:
            try:
                resp = await client.get(f"{DISCORD_API}/guilds/{gid}/members/search",
                                        params={"query": needle, "limit": 10}, headers=_headers())
            except httpx.HTTPError:
                continue
            if resp.status_code != 200:
                continue
            for m in resp.json():
                user = m.get("user") or {}
                names = {(user.get("username") or "").lower(), (user.get("global_name") or "").lower(), (m.get("nick") or "").lower()}
                if needle in names:
                    return str(user.get("id"))
    return None


async def send_dm(user_id: str, content: str) -> None:
    """Личное сообщение от бота (канал лички создаётся/находится через /users/@me/channels)."""
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(f"{DISCORD_API}/users/@me/channels", headers=_headers(), json={"recipient_id": str(user_id)})
        if resp.status_code >= 400:
            raise DiscordRestError(f"Discord ответил {resp.status_code}: {resp.text[:200]}")
        channel_id = resp.json()["id"]
    await send_message(channel_id, content[:2000])


async def notify_super_admins(content: str) -> None:
    """Лички всем супер-админам (bot_admins); сбой одной не мешает остальным."""
    import database
    for admin in database.get_all_bot_admins():
        try:
            await send_dm(admin["discord_id"], content)
        except Exception as e:
            print(f"⚠️ [Уведомление] не отправлено {admin['discord_id']}: {e!r}")
