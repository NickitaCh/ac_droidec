"""Зеркала в обратную сторону: сообщение из ветки Telegram → Discord-канал зеркала
(просьба 2026-10-29: «должно работать в обе стороны + подписи тг ников»).

Зеркало (tg_relay_channels, сайт /settings → Telegram) связывает Discord-канал с меткой ветки;
направление «в обе стороны» или «Telegram → Discord» копирует сюда. Подпись — имя и @ник в
Telegram. Вложения (фото, файлы, видео, голосовые) качаются через getFile (до 20 МБ — лимит
Bot API) и заливаются в Discord (до 10 МБ); что больше — строкой «не влезло». Ссылку на файл
Telegram не даём: в ней токен бота. Свои сообщения бот в Telegram не получает, а в Discord их
пишет от имени Discord-бота — его посты зеркало Discord → Telegram пропускает, петли нет."""

import asyncio

import httpx

from services import discord_rest, feature_flags
from tg_bot import core, outbox

FEATURE = "tg_relay_auto"
DISCORD_LIMIT = 2000
DISCORD_FILE_MAX = 10 * 1024 * 1024
TG_FILE_MAX = 20 * 1024 * 1024  # больше Bot API не отдаёт
FILE_URL = "https://api.telegram.org/file/bot{token}/{path}"


def _author(message: dict) -> str:
    if message.get("sender_chat") and (message.get("from") or {}).get("id") == core.ANONYMOUS_ADMIN_ID:
        return message["sender_chat"].get("title") or "администратор группы"
    user = message.get("from") or {}
    name = " ".join(p for p in (user.get("first_name"), user.get("last_name")) if p) or "игрок"
    return f"{name} (@{user['username']})" if user.get("username") else name


def _media(message: dict) -> list[tuple[str, str, int]]:
    """[(file_id, имя файла, размер)] — что из сообщения можно перелить в Discord."""
    out = []
    if message.get("photo"):
        p = message["photo"][-1]  # самый крупный размер
        out.append((p["file_id"], f"photo_{message['message_id']}.jpg", p.get("file_size") or 0))
    for key, default in (("document", "file"), ("video", "video.mp4"), ("animation", "animation.mp4"),
                         ("audio", "audio.mp3"), ("voice", "voice.ogg"), ("video_note", "video_note.mp4")):
        item = message.get(key)
        if item:
            out.append((item["file_id"], item.get("file_name") or default, item.get("file_size") or 0))
    sticker = message.get("sticker")
    if sticker and not sticker.get("is_animated") and not sticker.get("is_video"):
        out.append((sticker["file_id"], "sticker.webp", sticker.get("file_size") or 0))
    return out


async def _download(app, file_id: str) -> bytes:
    info = await app.api.call("getFile", file_id=file_id)
    async with httpx.AsyncClient(timeout=120) as client:
        resp = await client.get(FILE_URL.format(token=app.api._token, path=info["file_path"]))
    resp.raise_for_status()
    return resp.content


def _chunks(text: str) -> list[str]:
    out = []
    while len(text) > DISCORD_LIMIT:
        cut = text.rfind("\n", 0, DISCORD_LIMIT)
        cut = cut if cut > 0 else DISCORD_LIMIT
        out.append(text[:cut])
        text = text[cut:].lstrip("\n")
    return out + ([text] if text else [])


async def handle_group_message(app, message: dict):
    chat_id = message["chat"]["id"]
    thread_id = message.get("message_thread_id") if message.get("is_topic_message") else None
    targets = await asyncio.to_thread(outbox.tg_mirrors_for, chat_id, thread_id)
    targets = [t for t in targets if await asyncio.to_thread(feature_flags.is_enabled, t[0], FEATURE)]
    if not targets:
        return
    text = message.get("text") or message.get("caption") or ""
    media = _media(message)
    if not text.strip() and not media:
        return  # служебное сообщение (вход в группу, закреп и т. п.)

    files, skipped = [], []
    for file_id, name, size in media:
        if size > min(TG_FILE_MAX, DISCORD_FILE_MAX):
            skipped.append(f"{name} ({size / 1024 / 1024:.0f} МБ)")
            continue
        try:
            files.append((name, await _download(app, file_id)))
        except Exception as e:
            print(f"⚠️ [TG→Discord] файл {name}: {e!r}")
            skipped.append(name)
    body = f"**{_author(message)}** (Telegram):\n{text}".rstrip()
    if skipped:
        body += "\n-# не влезло в Discord: " + ", ".join(skipped)
    chunks = _chunks(body)
    for guild_id, channel_id in targets:
        try:
            for i, chunk in enumerate(chunks):
                last = i == len(chunks) - 1
                if last and files:
                    await discord_rest.send_message_files(channel_id, chunk, files)
                else:
                    await discord_rest.send_message(channel_id, chunk)
        except Exception as e:
            print(f"⚠️ [TG→Discord] guild {guild_id} → канал {channel_id}: {e!r}")


core.GROUP_MESSAGE_HANDLER["handler"] = handle_group_message
