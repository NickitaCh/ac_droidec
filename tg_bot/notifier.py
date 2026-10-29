"""Отправка очереди tg_bot/outbox.py: оповещения гильдии в её чаты, личные сообщения и
копии сообщений Discord в ветку по метке. Плюс /notify — какие оповещения гильдии приходят
в этот чат, и /topic — метка ветки для /тг и зеркал каналов."""

import asyncio
import html
import re
import time

import httpx

from tg_bot import markup, outbox, store
from tg_bot.api import TelegramError
from tg_bot.core import TIER_OFFICER, App, Ctx, callback, command

POLL_SECONDS = 5
MAX_AGE = 60 * 60  # не досылаем то, что пролежало час (бот лежал) — напоминание уже неактуально

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
    async with httpx.AsyncClient(timeout=60, follow_redirects=True) as client:
        resp = await client.get(att["url"])
    resp.raise_for_status()
    data, filename = resp.content, att.get("filename") or "file"
    mime = att.get("content_type") or "application/octet-stream"
    if mime.startswith("image/") and not mime.endswith("gif"):
        try:
            await app.api.send_photo(chat_id, data, filename, thread_id=thread_id)
            return
        except TelegramError:
            pass  # слишком вытянутая/большая картинка — файлом
    await app.api.send_document(chat_id, data, filename, mime, thread_id=thread_id)


async def _deliver_relay(app: App, item: dict) -> str | None:
    """Копия сообщения Discord в ветку: текст, затем вложения по одному."""
    chat_id, thread_id = item["chat_id"], item["thread_id"]
    if store.chat_guild(chat_id) != item["guild_id"]:
        return f"{chat_id}: чат больше не привязан к гильдии"
    try:
        for chunk in render(item["text"]) if item["text"].strip() else []:
            await app.api.send_message(chat_id, chunk, thread_id=thread_id)
    except TelegramError as e:
        return f"{chat_id}: {e.description}"
    errors = []
    for att in item["attachments"]:
        try:
            await _send_attachment(app, chat_id, thread_id, att)
        except (TelegramError, httpx.HTTPError) as e:
            errors.append(f"{att.get('filename')}: {getattr(e, 'description', None) or e!r}")
    return "; ".join(errors) or None


async def _deliver(app: App, item: dict) -> str | None:
    if item["kind"] == "relay":
        return await _deliver_relay(app, item)
    if item["kind"] == "dm":
        targets = [u["tg_user_id"] for u in store.tg_users_for_owner(item["owner_id"]) if u["dm_open"]]
    else:
        targets = outbox.chats_for(item["guild_id"], item["kind"])
    errors = []
    for chat_id in targets:
        for chunk in render(item["text"]):
            try:
                await app.api.send_message(chat_id, chunk)
            except TelegramError as e:
                errors.append(f"{chat_id}: {e.description}")
                break
    return "; ".join(errors) or None


async def run_notifier(app: App):
    while True:
        try:
            for item in await asyncio.to_thread(outbox.pending):
                if time.time() - item["created_at"] > MAX_AGE:
                    outbox.mark_sent(item["id"], "устарело")
                    continue
                error = await _deliver(app, item)
                outbox.mark_sent(item["id"], error)
                if error:
                    print(f"⚠️ [TG оповещения] #{item['id']} {item['kind']}: {error}")
        except Exception as e:
            print(f"⚠️ [TG оповещения] {e!r}")
        await asyncio.sleep(POLL_SECONDS)


# ---------------------------------------------------------------- /notify

def _notify_view(chat_id: int) -> tuple[str, list]:
    enabled = outbox.chat_kinds(chat_id)
    lines = ["<b>Оповещения гильдии в этот чат</b>", "Нажмите, чтобы включить или выключить:"]
    keyboard = [[{"text": f"{'✅' if kind in enabled else '▫️'} {label}", "callback_data": f"nt|{kind}"}]
                for kind, label in outbox.KINDS.items()]
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
    text, keyboard = _notify_view(ctx.chat_id)
    await ctx.reply(text, keyboard=keyboard)


@callback("nt", tier=TIER_OFFICER)
async def cb_notify(ctx: Ctx, kind: str):
    denial = _notify_denied(ctx)
    if denial:
        await ctx.api.answer_callback(ctx.callback_query["id"], denial, alert=True)
        return
    if kind not in outbox.KINDS:
        return
    kinds = outbox.chat_kinds(ctx.chat_id)
    kinds ^= {kind}
    outbox.set_chat_kinds(ctx.chat_id, kinds)
    text, keyboard = _notify_view(ctx.chat_id)
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


@callback("tp", tier=TIER_OFFICER)
async def cb_topic(ctx: Ctx, label: str):
    denial = _notify_denied(ctx)
    if denial:
        await ctx.api.answer_callback(ctx.callback_query["id"], denial, alert=True)
        return
    guild_id = store.chat_guild(ctx.chat_id)
    outbox.remove_topic(guild_id, label)
    text, keyboard = _topics_view(guild_id)
    await ctx.edit(text, keyboard=keyboard or None)
