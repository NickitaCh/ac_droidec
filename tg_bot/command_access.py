"""Per-chat and per-topic command switches; guild officers configure the current location."""
import sqlite3

import database
from tg_bot import store


def _connect():
    conn = sqlite3.connect(database.DB_NAME)
    conn.execute("""CREATE TABLE IF NOT EXISTS tg_command_blocks (
        guild_id INTEGER NOT NULL, chat_id INTEGER NOT NULL, thread_id INTEGER NOT NULL,
        command TEXT NOT NULL, PRIMARY KEY (guild_id, chat_id, thread_id, command))""")
    return conn


def blocked(chat_id: int, thread_id: int | None) -> set[str]:
    guild_id = store.chat_guild(chat_id)
    if not guild_id:
        return set()
    with _connect() as conn:
        rows = conn.execute("SELECT command FROM tg_command_blocks WHERE guild_id = ? AND chat_id = ? AND thread_id = ?",
                            (guild_id, chat_id, thread_id or 0)).fetchall()
    conn.close()
    return {r[0] for r in rows}


def toggle(guild_id: int, chat_id: int, thread_id: int | None, name: str):
    with _connect() as conn:
        args = (guild_id, chat_id, thread_id or 0, name)
        deleted = conn.execute("DELETE FROM tg_command_blocks WHERE guild_id=? AND chat_id=? AND thread_id=? AND command=?", args)
        if not deleted.rowcount:
            conn.execute("INSERT INTO tg_command_blocks VALUES (?, ?, ?, ?)", args)
    conn.close()


def denied(ctx, name: str | None) -> bool:
    return not ctx.is_private and name is not None and name in blocked(ctx.chat_id, ctx.thread_id)


def install():
    from tg_bot.core import COMMAND_ORDER, TIER_OFFICER, callback, command
    from tg_bot.notifier import _notify_denied

    def commands():
        return [c for c in COMMAND_ORDER if c.where != "private" and c.name not in
                {"command_access", "bind", "unbind", "start", "reg"}]

    def view(ctx):
        blocks = blocked(ctx.chat_id, ctx.thread_id)
        keyboard = [[{"text": f"{'🚫' if c.name in blocks else '✅'} /{c.name}",
                      "callback_data": f"ca|{c.name}"}] for c in commands()]
        return ("<b>Команды в этой теме</b>" if ctx.thread_id else "<b>Команды в общем чате</b>") + \
               "\nНажмите команду, чтобы запретить или разрешить её здесь. Настройка действует для всех, включая офицеров. " \
               "Другие темы и личка бота сохраняют свои настройки. Оповещения не меняются.", keyboard

    @command("command_access", "Разрешить или запретить команды в этой теме (офицеры)", tier=TIER_OFFICER, where="group")
    async def configure(ctx):
        error = _notify_denied(ctx)
        if error:
            await ctx.reply(error)
            return
        text, keyboard = view(ctx)
        await ctx.reply(text, keyboard=keyboard)

    @callback("ca", tier=TIER_OFFICER, command_name="command_access")
    async def configure_callback(ctx, name):
        error = _notify_denied(ctx)
        if error:
            await ctx.api.answer_callback(ctx.callback_query["id"], error, alert=True)
            return
        if name not in {c.name for c in commands()}:
            return
        toggle(store.chat_guild(ctx.chat_id), ctx.chat_id, ctx.thread_id, name)
        text, keyboard = view(ctx)
        await ctx.edit(text, keyboard=keyboard)
