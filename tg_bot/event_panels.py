"""Publish and navigate officer event panels from a bound Telegram topic."""
import asyncio

from services import event_panels
from tg_bot import command_access, store
from tg_bot.api import TelegramError
from tg_bot.core import TIER_OFFICER, callback, command

PANEL_COMMANDS = ("tb", "tw", "raid")

async def _start(ctx, kind):
    if ctx.args.lower() not in ("старт", "start"):
        await ctx.reply(f"Вызовите <code>/{kind} старт</code> в нужной теме.")
        return
    guild_id = ctx.access.get("guild_id")
    if store.chat_guild(ctx.chat_id) != guild_id or not event_panels.allowed(guild_id, ctx.access.get("tier"), ctx.app.profile):
        await ctx.reply("Команда доступна офицерам АС в привязанной группе тестового бота.")
        return
    serial, token = await asyncio.to_thread(event_panels.reserve, "telegram", guild_id, kind)
    text = await asyncio.to_thread(event_panels.latest, guild_id, kind)
    sent = await ctx.reply(event_panels.telegram_text(text), event_panels.telegram_keyboard(kind, token))
    ok, replaced = await asyncio.to_thread(
        event_panels.activate, "telegram", guild_id, kind, serial, token, ctx.chat_id, sent["message_id"],
    )
    if not ok:
        await event_panels.edit({"platform": "telegram", "channel": ctx.chat_id, "message": sent["message_id"]}, retire=True)
    elif replaced:
        await event_panels.edit(replaced, retire=True)


@command("tb", "ТБ · /tb старт публикует сводку", tier=TIER_OFFICER, aliases=("тб",), where="group", menu=False)
async def start_tb(ctx):
    await _start(ctx, "tb")


@command("tw", "ВГ · /tw старт публикует сводку", tier=TIER_OFFICER, aliases=("вг",), where="group", menu=False)
async def start_tw(ctx):
    await _start(ctx, "tw")


@command("raid", "Рейд · /raid старт публикует сводку", tier=TIER_OFFICER, aliases=("рейд",), where="group", menu=False)
async def start_raid(ctx):
    await _start(ctx, "raid")


@callback("glp", tier=TIER_OFFICER)
async def open_panel(ctx, payload):
    query_id = ctx.callback_query["id"]
    parts = payload.split(":")
    if len(parts) != 3 or parts[0] not in PANEL_COMMANDS:
        return
    kind, token, action = parts
    guild_id = ctx.access.get("guild_id")
    if store.chat_guild(ctx.chat_id) != guild_id or not event_panels.allowed(guild_id, ctx.access.get("tier"), ctx.app.profile):
        await ctx.api.answer_callback(query_id, "Доступно офицерам АС в тестовой версии.", alert=True)
        return
    if command_access.denied(ctx, kind):
        await ctx.api.answer_callback(query_id, "Команда отключена в этой теме.", alert=True)
        return
    url = await asyncio.to_thread(
        event_panels.handoff, "telegram", guild_id, kind, token, ctx.chat_id,
        ctx.message["message_id"], ctx.owner_id, action,
    )
    if not url:
        await ctx.api.answer_callback(query_id, "Эта панель устарела. Откройте последнюю сводку.", alert=True)
        return
    try:
        await ctx.api.send_message(ctx.user["id"], "Откройте сводку под своим аккаунтом. Вход в игру подтверждается отдельно.",
                                   keyboard=[[{"text": "Открыть на сайте", "url": url}]])
    except TelegramError:
        await ctx.api.answer_callback(query_id, "Сначала откройте личку бота и нажмите Start, затем повторите.", alert=True)
    else:
        await ctx.api.answer_callback(query_id, "Ссылка в личке бота.")
