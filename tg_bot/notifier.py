"""Отправка очереди tg_bot/outbox.py: оповещения гильдии в её чаты и личные сообщения.
Плюс /notify — какие оповещения гильдии приходят в этот чат."""

import asyncio
import html
import re
import time

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


async def _deliver(app: App, item: dict) -> str | None:
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

