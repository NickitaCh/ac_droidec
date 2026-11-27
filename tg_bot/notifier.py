"""Отправка очереди tg_bot/outbox.py: оповещения гильдии в её чаты, личные сообщения и
копии сообщений Discord в ветку по метке. Плюс /notify — какие оповещения гильдии приходят
в этот чат, и /topic — метка ветки для /тг и зеркал каналов."""

import asyncio
import base64
import html
import re
import sqlite3
import time

import httpx

from tg_bot import markup, outbox, store
from tg_bot.api import TelegramError, retry_after_header
from tg_bot.core import TIER_OFFICER, App, Ctx, callback, command

POLL_SECONDS = 5
CHECKPOINT_DRAIN_SECONDS = 10
MAX_AGE = 60 * 60  # не досылаем то, что пролежало час (бот лежал) — напоминание уже неактуально

class RetryDelivery(Exception):
    def __init__(self, seconds):
        self.seconds = seconds


def _retry_temporary(error):
    if isinstance(error, TelegramError) and (error.error_code == 429 or (error.error_code or 0) >= 500):
        raise RetryDelivery(max(POLL_SECONDS, error.retry_after or 30)) from error
    if isinstance(error, (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout)):
        raise RetryDelivery(30) from error
    if isinstance(error, httpx.HTTPStatusError) and (error.response.status_code == 429 or error.response.status_code >= 500):
        delay = retry_after_header(error.response)
        raise RetryDelivery(max(POLL_SECONDS, delay if delay is not None else 30)) from error


_OWNER_MENTION_RE = re.compile(r"<@!?((?:tg:)?\d+)>")


def _mention(owner_id: str) -> str:
    users = store.tg_users_for_owner(owner_id)
    if not users:
        return ""  # не в Telegram — имя игрока и так рядом в тексте
    u = users[0]
    label = f"@{u['username']}" if u["username"] else (u["full_name"] or "игрок")
    return f'<a href="tg://user?id={u["tg_user_id"]}">{html.escape(label, quote=False)}</a>'


def render(text: str) -> list[str]:
    """Discord-разметка → куски HTML; упоминания игроков → Telegram-упоминания."""
    mentions = {}

    def keep(m):
        key = f"TGMENTION{len(mentions)}X"
        mentions[key] = _mention(m.group(1))
        return key

    text = _OWNER_MENTION_RE.sub(keep, text)
    chunks = []
    for chunk in markup.chunk_md(text.split("\n")):
        converted = markup.md_to_html(chunk)
        for key, value in mentions.items():
            converted = converted.replace(key, value)
        chunks.append(re.sub(r"\n{3,}", "\n\n", converted))
    return chunks


# Вложение больше этого не качаем (у Bot API лимит загрузки 50 МБ) — даём ссылку.
ATTACHMENT_MAX_BYTES = 20 * 1024 * 1024


async def _send_attachment(app: App, chat_id: int, thread_id, att: dict):
    if "data_base64" in att:
        data = base64.b64decode(att["data_base64"], validate=True)
    else:
        try:
            async with httpx.AsyncClient(timeout=60, follow_redirects=True) as client:
                resp = await client.get(att["url"])
        except httpx.TransportError as error:
            # An interrupted download cannot have delivered anything to Telegram.
            raise RetryDelivery(30) from error
        resp.raise_for_status()
        data = resp.content
    filename = att.get("filename") or "file"
    mime = att.get("content_type") or "application/octet-stream"
    # Серверный эмодзи Discord (сообщение из одних эмодзи, cogs/tg_relay.py): статичный — стикером,
    # анимированный — GIF-анимацией; не принял Telegram — обычной картинкой ниже.
    method, field = {"sticker": ("sendSticker", "sticker"), "animation": ("sendAnimation", "animation")}.get(
        att.get("as"), (None, None))
    if method:
        try:
            await app.api.call(method, files={field: (filename, data, mime)}, chat_id=chat_id, message_thread_id=thread_id)
            return
        except TelegramError as error:
            _retry_temporary(error)
    if mime.startswith("image/") and not mime.endswith("gif"):
        try:
            await app.api.send_photo(chat_id, data, filename, thread_id=thread_id)
            return
        except TelegramError as error:
            _retry_temporary(error)  # слишком вытянутая/большая картинка — файлом
    await app.api.send_document(chat_id, data, filename, mime, thread_id=thread_id)


async def _deliver_relay(app: App, item: dict) -> str | None:
    """Копия сообщения Discord в ветку: текст, затем вложения по одному."""
    chat_id, thread_id = item["chat_id"], item["thread_id"]
    if await asyncio.to_thread(store.chat_guild, chat_id) != item["guild_id"]:
        return f"{chat_id}: чат больше не привязан к гильдии"
    return await _send_to(app, chat_id, thread_id, item)


async def _write_outbox(operation, *args):
    while True:
        try:
            await asyncio.to_thread(operation, *args)
            return
        except sqlite3.OperationalError as error:
            code = getattr(error, "sqlite_errorcode", 0) & 0xff
            if code not in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED) and str(error) != "database is locked":
                raise
            # Retain the acknowledgement or retry deadline until SQLite is writable.
            await asyncio.sleep(POLL_SECONDS)


async def _persist_outbox(operation, *args):
    checkpoint = asyncio.create_task(_write_outbox(operation, *args))
    try:
        await asyncio.shield(checkpoint)
    except asyncio.CancelledError:
        try:
            await asyncio.wait_for(checkpoint, CHECKPOINT_DRAIN_SECONDS)
        except Exception as error:
            print(f"⚠️ [TG оповещения] не сохранились данные очереди при остановке: {error!r}")
        raise


async def _send_to(app: App, chat_id: int, thread_id, item: dict) -> str | None:
    progress = item["delivery_progress"]
    errors = []
    chunks = await asyncio.to_thread(render, item["text"]) if item["text"].strip() else []
    parts = [("text", index, chunk) for index, chunk in enumerate(chunks)]
    parts += [("file", index, att) for index, att in enumerate(item["attachments"])]
    for kind, index, part in parts:
        key = f"{chat_id}:{thread_id}:{kind}:{index}"
        if key not in progress:
            error = None
            try:
                if kind == "text":
                    await app.api.send_message(chat_id, part, thread_id=thread_id)
                else:
                    await _send_attachment(app, chat_id, thread_id, part)
            except (TelegramError, httpx.HTTPError) as exc:
                _retry_temporary(exc)
                error = f"{chat_id}: {getattr(exc, 'description', type(exc).__name__)}"
            progress[key] = error
            await _persist_outbox(outbox.record_delivery_progress, item["id"], progress)
        if progress[key]:
            errors.append(progress[key])
            if kind == "text":
                break
    return "; ".join(errors) or None


async def _deliver(app: App, item: dict) -> str | None:
    if item["kind"] == "relay":
        return await _deliver_relay(app, item)
    if item["kind"] == "dm":
        users = await asyncio.to_thread(
            store.tg_users_for_owner, item["owner_id"], profile=getattr(app, "profile", None)
        )
        targets = [(u["tg_user_id"], None) for u in users if u["dm_open"]]
        if not targets:
            return "Игрок ещё не открыл личку с этим ботом."
    else:
        targets = await asyncio.to_thread(outbox.chats_for, item["guild_id"], item["kind"])  # маршрут вида: чат или ветка
    errors = []
    for chat_id, thread_id in targets:
        error = await _send_to(app, chat_id, thread_id, item)
        if error:
            errors.append(error)
    return "; ".join(errors) or None


async def run_notifier(app: App):
    while True:
        try:
            from services import bot_profiles
            profile = getattr(app, "profile", bot_profiles.runtime_profile())
            for item in await asyncio.to_thread(outbox.pending, profile=profile):
                if item.get("guild_id") is not None and await asyncio.to_thread(bot_profiles.guild_profile, item["guild_id"]) != profile:
                    continue
                if time.time() - item["created_at"] >= MAX_AGE:
                    await asyncio.to_thread(outbox.mark_sent, item["id"], "устарело")
                    continue
                try:
                    error = await _deliver(app, item)
                except RetryDelivery as retry:
                    next_attempt = min(time.time() + retry.seconds, item["created_at"] + MAX_AGE)
                    await _persist_outbox(outbox.retry_delivery, item["id"], next_attempt)
                    continue
                await asyncio.to_thread(outbox.mark_sent, item["id"], error)
                if error:
                    print(f"⚠️ [TG оповещения] #{item['id']} {item['kind']}: {error}")
        except Exception as e:
            print(f"⚠️ [TG оповещения] {e!r}")
        await asyncio.sleep(POLL_SECONDS)


# ---------------------------------------------------------------- /notify

def _notify_view(ctx: Ctx) -> tuple[str, list]:
    """✅ — вид идёт сюда, ↪ — включён, но в другой чат/ветку (нажатие перенесёт сюда)."""
    routes = outbox.guild_routes(store.chat_guild(ctx.chat_id))
    where = "эту ветку" if ctx.thread_id else "этот чат"
    lines = [f"<b>Оповещения гильдии в {where}</b>",
             "Нажмите, чтобы включить или выключить. ↪ — сейчас идёт в другое место, нажатие перенесёт сюда. "
             "Всё то же — на сайте: Настройки → Telegram."]
    keyboard = []
    for kind, label in outbox.KINDS.items():
        route = routes.get(kind)
        mark = "✅" if outbox.route_here(route, ctx.chat_id, ctx.thread_id) else \
            "↪" if route and route["enabled"] and route["bound"] else "▫️"
        keyboard.append([{"text": f"{mark} {label}", "callback_data": f"nt|{kind}"}])
    return "\n".join(lines), keyboard


def _notify_denied(ctx: Ctx) -> str | None:
    guild_id = store.chat_guild(ctx.chat_id)
    if not guild_id:
        return "Сначала привяжите группу к гильдии: /bind"
    if guild_id != ctx.access["guild_id"] and not ctx.access["is_super_admin"]:
        return "Настраивать оповещения может офицер гильдии, к которой привязан чат."
    return None


@command("notify", "Какие оповещения гильдии слать сюда (офицеры)", tier=TIER_OFFICER, where="group", aliases=("оповещения",))
async def cmd_notify(ctx: Ctx):
    denial = _notify_denied(ctx)
    if denial:
        await ctx.reply(denial)
        return
    text, keyboard = _notify_view(ctx)
    await ctx.reply(text, keyboard=keyboard)


@callback("nt", tier=TIER_OFFICER, command_name="notify")
async def cb_notify(ctx: Ctx, kind: str):
    denial = _notify_denied(ctx)
    if denial:
        await ctx.api.answer_callback(ctx.callback_query["id"], denial, alert=True)
        return
    if kind not in outbox.KINDS:
        return
    guild_id = store.chat_guild(ctx.chat_id)
    route = outbox.guild_routes(guild_id).get(kind)
    if outbox.route_here(route, ctx.chat_id, ctx.thread_id):
        outbox.set_route_enabled(guild_id, kind, False)
    else:
        outbox.set_route(guild_id, kind, ctx.chat_id, ctx.thread_id, True, ctx.owner_id)
    text, keyboard = _notify_view(ctx)
    await ctx.edit(text, keyboard=keyboard)



# ---------------------------------------------------------------- /topic

def _topics_view(guild_id: int) -> tuple[str, list]:
    topics = outbox.guild_topics(guild_id)
    lines = ["<b>Ветки для копий из Discord</b>"]
    if topics:
        lines += [f"• <b>{html.escape(t['label'])}</b> — {html.escape(outbox.topic_place(t))}" for t in topics]
        lines.append("")
    else:
        lines.append("Пока ни одной.")
    lines.append("Задать метку: напишите <code>/topic вг</code> в нужной ветке. Потом в Discord: "
                 "<code>/тг канал: вг сообщение: ссылка</code>, или зеркало канала на сайте (/settings → Telegram).")
    keyboard = [[{"text": f"✕ убрать «{t['label']}»", "callback_data": f"tp|{t['label']}"}] for t in topics]
    return "\n".join(lines), keyboard


def _topic_title(ctx: Ctx) -> str | None:
    if not ctx.thread_id:
        return None
    created = (ctx.message.get("reply_to_message") or {}).get("forum_topic_created") or {}
    return created.get("name")


@command("topic", "Метка ветки для копий из Discord (офицеры)", tier=TIER_OFFICER, where="group", aliases=("ветка",))
async def cmd_topic(ctx: Ctx):
    denial = _notify_denied(ctx)
    if denial:
        await ctx.reply(denial)
        return
    guild_id = store.chat_guild(ctx.chat_id)
    label = outbox.normalize_label(ctx.args)
    if not label:
        text, keyboard = _topics_view(guild_id)
        await ctx.reply(text, keyboard=keyboard or None)
        return
    outbox.set_topic(guild_id, label, ctx.chat_id, ctx.thread_id, _topic_title(ctx), ctx.owner_id)
    where = "эту ветку" if ctx.thread_id else "этот чат"
    await ctx.reply(f"✅ Метка <b>{html.escape(label)}</b> → {where}. В Discord: "
                    f"<code>/тг канал: {html.escape(label)} сообщение: ссылка или текст</code>")


@callback("tp", tier=TIER_OFFICER, command_name="topic")
async def cb_topic(ctx: Ctx, label: str):
    denial = _notify_denied(ctx)
    if denial:
        await ctx.api.answer_callback(ctx.callback_query["id"], denial, alert=True)
        return
    guild_id = store.chat_guild(ctx.chat_id)
    outbox.remove_topic(guild_id, label)
    text, keyboard = _topics_view(guild_id)
    await ctx.edit(text, keyboard=keyboard or None)
