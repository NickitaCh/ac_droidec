"""Per-chat and per-topic command switches; guild officers configure the current location."""
import sqlite3

import database
from tg_bot import store
from tg_bot import topics


def _connect():
    conn = sqlite3.connect(database.DB_NAME)
    conn.execute("""CREATE TABLE IF NOT EXISTS tg_command_blocks (
        guild_id INTEGER NOT NULL, chat_id INTEGER NOT NULL, thread_id INTEGER NOT NULL,
        command TEXT NOT NULL, PRIMARY KEY (guild_id, chat_id, thread_id, command))""")
    conn.execute("""CREATE TABLE IF NOT EXISTS tg_command_defaults (
        guild_id INTEGER NOT NULL, chat_id INTEGER NOT NULL, allowed INTEGER NOT NULL,
        PRIMARY KEY(guild_id,chat_id))""")
    conn.execute("""CREATE TABLE IF NOT EXISTS tg_command_rules (
        guild_id INTEGER NOT NULL, chat_id INTEGER NOT NULL, thread_id INTEGER NOT NULL,
        audience TEXT NOT NULL, command TEXT NOT NULL, allowed INTEGER NOT NULL,
        PRIMARY KEY(guild_id,chat_id,thread_id,audience,command))""")
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


MANAGEMENT = {"command_access", "bind", "unbind"}


def policy(chat_id):
    gid = store.chat_guild(chat_id)
    if not gid:
        return {"default": True, "rules": {}, "blocks": set()}
    conn = _connect()
    try:
        row = conn.execute("SELECT allowed FROM tg_command_defaults WHERE guild_id=? AND chat_id=?", (gid,chat_id)).fetchone()
        rules = {(topics.topic_id(t), a, c): bool(v) for t,a,c,v in conn.execute(
            "SELECT thread_id,audience,command,allowed FROM tg_command_rules WHERE guild_id=? AND chat_id=?", (gid,chat_id))}
        blocks = {(topics.topic_id(t),c) for t,c in conn.execute(
            "SELECT thread_id,command FROM tg_command_blocks WHERE guild_id=? AND chat_id=?", (gid,chat_id))}
        return {"default": bool(row[0]) if row else True, "rules": rules, "blocks": blocks}
    finally:
        conn.close()


def general_allowed(settings, thread_id, name):
    tid, rules = topics.topic_id(thread_id), settings["rules"]
    if (tid,"all",name) in rules:
        return rules[tid,"all",name]
    if (tid,name) in settings["blocks"]:
        return False
    return rules.get((tid,"all","*"), settings["default"])


def officer_extra(settings, thread_id, name):
    tid, rules = topics.topic_id(thread_id), settings["rules"]
    return rules.get((tid,"officer",name), rules.get((tid,"officer","*"), False))


def set_rule(guild_id, chat_id, thread_id, audience, command_name, allowed):
    if audience not in ("all", "officer"):
        raise ValueError("Unknown audience")
    conn = _connect()
    try:
        with conn:
            conn.execute("INSERT OR REPLACE INTO tg_command_rules VALUES (?,?,?,?,?,?)",
                (guild_id,chat_id,topics.topic_id(thread_id),audience,command_name,int(allowed)))
    finally:
        conn.close()


def set_topic(guild_id, chat_id, thread_id, audience, allowed):
    """Replace a topic's exceptions atomically when its whole-topic switch changes."""
    tid = topics.topic_id(thread_id)
    conn = _connect()
    try:
        with conn:
            conn.execute("DELETE FROM tg_command_rules WHERE guild_id=? AND chat_id=? AND thread_id=? AND audience=?",
                         (guild_id, chat_id, tid, audience))
            if audience == "all":
                conn.execute("DELETE FROM tg_command_blocks WHERE guild_id=? AND chat_id=? AND thread_id IN (?,?)",
                             (guild_id, chat_id, tid, 1 if tid == 0 else tid))
            conn.execute("INSERT INTO tg_command_rules VALUES (?,?,?,?,?,?)",
                         (guild_id, chat_id, tid, audience, "*", int(allowed)))
    finally:
        conn.close()


def reset_policy(guild_id, chat_id, allowed):
    conn = _connect()
    try:
        with conn:
            conn.execute("INSERT OR REPLACE INTO tg_command_defaults VALUES (?,?,?)", (guild_id,chat_id,int(allowed)))
            for table in ("tg_command_rules", "tg_command_blocks"):
                conn.execute(f"DELETE FROM {table} WHERE guild_id=? AND chat_id=?", (guild_id,chat_id))
    finally:
        conn.close()


def denied(ctx, name: str | None) -> bool:
    if ctx.is_private or name is None or name in MANAGEMENT:
        return False
    settings = policy(ctx.chat_id)
    if general_allowed(settings, ctx.thread_id, name):
        return False
    if not officer_extra(settings, ctx.thread_id, name):
        return True
    access = ctx.access
    return not (access.get("is_super_admin") or
                (access.get("tier") == "officer" and access.get("guild_id") == store.chat_guild(ctx.chat_id)))


def install():
    from tg_bot.core import COMMAND_ORDER, TIER_OFFICER, callback, command, esc
    from tg_bot.notifier import _notify_denied

    def commands():
        return [c for c in COMMAND_ORDER if c.where != "private" and c.name not in
                MANAGEMENT]

    def root(ctx):
        settings = policy(ctx.chat_id)
        return ("<b>Команды по темам этой группы</b>\n"
            + ("По умолчанию разрешены во всех темах." if settings["default"] else "По умолчанию запрещены, включая новые темы.")
            + "\nСначала запретите везде, затем отметьте нужные темы в «Темы для всех». "
              "Для офицеров можно добавить другие темы и выбрать в них отдельные команды. "
              "Права офицера эти настройки не выдают. Личка и уведомления настраиваются отдельно.", [
                [{"text":"🚫 Запретить везде", "callback_data":"ca|reset|0"}, {"text":"✅ Разрешить везде", "callback_data":"ca|reset|1"}],
                [{"text":"🗂 Темы для всех", "callback_data":"ca|topics|all|0"}],
                [{"text":"👮 Дополнительные темы для офицеров", "callback_data":"ca|topics|officer|0"}],
                [{"text":"➕ Добавить отсутствующую тему", "callback_data":"ca|add"}]])

    def topic_list(ctx, audience, page=0):
        entries = topics.known(ctx.chat_id)
        page = max(0,min(page,(len(entries)-1)//8))
        settings = policy(ctx.chat_id)
        keyboard = []
        for item in entries[page*8:(page+1)*8]:
            allowed = general_allowed(settings,item["id"],"*") if audience == "all" else officer_extra(settings,item["id"],"*")
            keyboard.append([
                {"text":f"{'✅' if allowed else '▫️'} {item['title'][:35]}", "callback_data":f"ca|flip|{audience}|{item['id']}|{page}"},
                {"text":"Команды", "callback_data":f"ca|commands|{audience}|{item['id']}|0"}])
        nav = []
        if page: nav.append({"text":"‹", "callback_data":f"ca|topics|{audience}|{page-1}"})
        if (page+1)*8 < len(entries): nav.append({"text":"›", "callback_data":f"ca|topics|{audience}|{page+1}"})
        if nav: keyboard.append(nav)
        keyboard.append([{"text":"К настройкам группы", "callback_data":"ca|root"}])
        title = "Темы для всех" if audience == "all" else "Дополнительные разрешения для офицеров"
        return f"<b>{title}</b>\nНажатие на тему переключает все команды; «Команды» — выбор по отдельности. " \
               "Офицерские исключения расширяют общий список, но не меняют права участника. " \
               "Показаны известные боту темы; недостающие можно добавить по ссылке.", keyboard

    def command_list(ctx, audience, tid, page=0):
        available = commands()
        page = max(0,min(page,(len(available)-1)//8))
        settings = policy(ctx.chat_id)
        keyboard=[]
        for cmd in available[page*8:(page+1)*8]:
            allowed = general_allowed(settings,tid,cmd.name) if audience == "all" else officer_extra(settings,tid,cmd.name)
            keyboard.append([{"text":f"{'✅' if allowed else '▫️'} /{cmd.name}" + (" · офицеры" if cmd.tier == TIER_OFFICER else ""),
                              "callback_data":f"ca|cmd|{audience}|{tid}|{cmd.name}|{page}"}])
        nav=[]
        if page: nav.append({"text":"‹", "callback_data":f"ca|commands|{audience}|{tid}|{page-1}"})
        if (page+1)*8<len(available): nav.append({"text":"›", "callback_data":f"ca|commands|{audience}|{tid}|{page+1}"})
        if nav: keyboard.append(nav)
        keyboard.append([{"text":"К списку тем", "callback_data":f"ca|topics|{audience}|0"}])
        title = next(t["title"] for t in topics.known(ctx.chat_id) if t["id"]==tid)
        return f"<b>{esc(title)}</b>\n" + ("Для всех пользователей." if audience=="all" else "Дополнительно только для офицеров; общие разрешения сохраняются."), keyboard

    @command("command_access", "Выбрать темы для команд и исключения для офицеров", tier=TIER_OFFICER, where="group")
    async def configure(ctx):
        error = _notify_denied(ctx)
        if error:
            await ctx.reply(error)
            return
        topics.observe(ctx.message)
        if ctx.args:
            args=ctx.args.split(maxsplit=2)
            if len(args)!=3 or args[0] not in ("добавить","add"):
                await ctx.reply("Добавить старую тему: <code>/command_access добавить https://t.me/c/…/… Название темы</code>")
                return
            try:
                topics.add_link(ctx.chat_id,args[1],args[2])
            except ValueError as e:
                await ctx.reply(esc(e))
                return
        text, keyboard = root(ctx)
        await ctx.reply(text, keyboard=keyboard)

    @callback("ca", tier=TIER_OFFICER, command_name="command_access")
    async def configure_callback(ctx, payload):
        error = _notify_denied(ctx)
        if error:
            await ctx.api.answer_callback(ctx.callback_query["id"], error, alert=True)
            return
        parts=payload.split("|")
        action=parts[0]
        gid=store.chat_guild(ctx.chat_id)
        try:
            if action=="reset" and len(parts)==2 and parts[1] in ("0","1"):
                reset_policy(gid,ctx.chat_id,parts[1]=="1")
                text,keyboard=root(ctx)
            elif action=="topics" and len(parts)==3 and parts[1] in ("all","officer"):
                text,keyboard=topic_list(ctx,parts[1],int(parts[2]))
            elif action in ("flip","commands","cmd"):
                audience,tid=parts[1],int(parts[2])
                if audience not in ("all","officer") or tid not in {t["id"] for t in topics.known(ctx.chat_id)}:
                    return
                settings=policy(ctx.chat_id)
                if action=="flip" and len(parts)==4:
                    allowed=general_allowed(settings,tid,"*") if audience=="all" else officer_extra(settings,tid,"*")
                    set_topic(gid, ctx.chat_id, tid, audience, not allowed)
                    text,keyboard=topic_list(ctx,audience,int(parts[3]))
                elif action=="commands" and len(parts)==4:
                    text,keyboard=command_list(ctx,audience,tid,int(parts[3]))
                elif action=="cmd" and len(parts)==5 and parts[3] in {c.name for c in commands()}:
                    name=parts[3]
                    allowed=general_allowed(settings,tid,name) if audience=="all" else officer_extra(settings,tid,name)
                    set_rule(gid,ctx.chat_id,tid,audience,name,not allowed)
                    text,keyboard=command_list(ctx,audience,tid,int(parts[4]))
                else: return
            elif action=="add":
                text="Добавьте тему по её ссылке:\n<code>/command_access добавить https://t.me/c/…/… Дроидец</code>\nНазвания тем также запоминаются из новых сообщений и событий Telegram."
                keyboard=[[{"text":"Назад","callback_data":"ca|root"}]]
            else:
                text,keyboard=root(ctx)  # old buttons reopen the new configuration instead of changing rules
        except (ValueError,IndexError):
            return
        await ctx.edit(text, keyboard=keyboard)
