"""Разбор входящих обновлений Telegram и маршрутизация на команды (tg_bot/handlers.py).

Команды в Telegram — только латиница/цифры/«_» (так их подсвечивает клиент и так их
видит бот в группе с режимом приватности). Русские синонимы (/профиль) тоже
принимаются — в личке всегда, в группе только если боту видны все сообщения
(приватность выключена в @BotFather или бот — администратор группы).

Права — те же, что у Discord-бота: guild_resolver.resolve_access по owner_id
(tg_bot/store.py), то есть по привязанному коду союзника и игровому рангу."""

import asyncio
import html
import secrets
import time
import traceback
from dataclasses import dataclass, field

import database
import guild_resolver
from tg_bot import store
from tg_bot.api import TelegramAPI, TelegramError

# Анонимный администратор группы пишет от имени этого служебного бота — кто это, неизвестно.
ANONYMOUS_ADMIN_ID = 1087968824

TIER_ANY, TIER_MEMBER, TIER_OFFICER = "any", "member", "officer"
_TIER_RANK = {None: 0, "member": 1, "officer": 2}
_REQUIRED_RANK = {TIER_ANY: 0, TIER_MEMBER: 1, TIER_OFFICER: 2}


@dataclass
class Command:
    name: str
    handler: object
    description: str
    tier: str = TIER_MEMBER
    aliases: tuple = ()
    menu: bool = True
    where: str = "any"  # "any" | "private" | "group"


@dataclass
class Callback:
    prefix: str
    handler: object
    tier: str = TIER_MEMBER
    command_name: str | None = None


COMMANDS: dict[str, Command] = {}
COMMAND_ORDER: list[Command] = []
CALLBACKS: dict[str, Callback] = {}


def command(name: str, description: str, tier: str = TIER_MEMBER, aliases: tuple = (), menu: bool = True, where: str = "any"):
    def wrap(fn):
        cmd = Command(name, fn, description, tier, aliases, menu, where)
        COMMAND_ORDER.append(cmd)
        for key in (name, *aliases):
            COMMANDS[key.lower()] = cmd
        return fn
    return wrap


def callback(prefix: str, tier: str = TIER_MEMBER, command_name: str | None = None):
    """Кнопки: callback_data = "<prefix>|<данные>" (Telegram ограничивает 64 байтами)."""
    def wrap(fn):
        CALLBACKS[prefix] = Callback(prefix, fn, tier, command_name)
        return fn
    return wrap


@dataclass
class App:
    api: TelegramAPI
    comlink: object
    username: str
    bot_id: int
    site_url: str
    tasks: set = field(default_factory=set)


class Ctx:
    """Одно обращение к боту: команда или нажатие кнопки."""

    def __init__(self, app: App, message: dict, user: dict, args: str = "", callback_query: dict | None = None):
        self.app = app
        self.api = app.api
        self.message = message
        self.chat = message["chat"]
        self.chat_id = self.chat["id"]
        self.thread_id = message.get("message_thread_id") if message.get("is_topic_message") else None
        self.user = user
        self.args = args.strip()
        self.callback_query = callback_query
        self.owner_id = store.touch_user(user, dm_open=self.is_private)
        self._access = None

    @property
    def is_private(self) -> bool:
        return self.chat.get("type") == "private"

    @property
    def access(self) -> dict:
        if self._access is None:
            self._access = guild_resolver.resolve_access(self.owner_id)
        return self._access

    @property
    def reply_user(self) -> dict | None:
        """Автор сообщения, на которое ответили командой (не сам бот и не сервисные)."""
        if self.callback_query is not None:
            return None
        replied = self.message.get("reply_to_message") or {}
        author = replied.get("from")
        if not author or author.get("is_bot") or author["id"] == self.user["id"]:
            return None
        # Ответ на корневое сообщение темы форума — это не «ответ игроку».
        if self.message.get("is_topic_message") and replied.get("message_id") == self.thread_id:
            return None
        return author

    async def reply(self, text: str, keyboard: list | None = None) -> dict:
        reply_to = None if self.callback_query is not None else self.message.get("message_id")
        return await self.api.send_message(self.chat_id, text, reply_to=reply_to, keyboard=keyboard, thread_id=self.thread_id)

    async def reply_photo(self, png: bytes, filename: str, caption: str | None = None, keyboard: list | None = None,
                          replace: bool = False) -> dict:
        if replace and self.callback_query is not None and (self.message.get("photo") or self.message.get("document")):
            async def edit_media(kind):
                return await self.api.call("editMessageMedia", files={"image": (filename, png, "image/png")},
                    chat_id=self.chat_id, message_id=self.message["message_id"],
                    media={"type": kind, "media": "attach://image", "caption": (caption or "")[:1024], "parse_mode": "HTML"},
                    reply_markup={"inline_keyboard": keyboard or []})
            try:
                return await edit_media("photo")
            except TelegramError as e:
                if "PHOTO_INVALID_DIMENSIONS" not in e.description and "PHOTO_SAVE_FILE_INVALID" not in e.description:
                    raise
                return await edit_media("document")
        reply_to = None if self.callback_query is not None else self.message.get("message_id")
        try:
            return await self.api.send_photo(self.chat_id, png, filename, caption=caption, reply_to=reply_to, thread_id=self.thread_id, keyboard=keyboard)
        except TelegramError as e:
            # PHOTO_INVALID_DIMENSIONS и т.п. у очень вытянутых картинок — тогда файлом.
            print(f"⚠️ [TG] sendPhoto не прошёл ({e.description}), отправляю файлом")
            return await self.api.send_document(self.chat_id, png, filename, "image/png", caption=caption,
                                                reply_to=reply_to, thread_id=self.thread_id, keyboard=keyboard)

    async def edit(self, text: str, keyboard: list | None = None):
        if self.message.get("photo") or self.message.get("document"):
            await self.api.call("editMessageCaption", chat_id=self.chat_id, message_id=self.message["message_id"],
                                caption=text[:1024], parse_mode="HTML",
                                reply_markup={"inline_keyboard": keyboard or []})
            return
        await self.api.edit_message_text(self.chat_id, self.message["message_id"], text, keyboard=keyboard)

    async def action(self, kind: str = "typing"):
        try:
            await self.api.send_chat_action(self.chat_id, kind, thread_id=self.thread_id)
        except TelegramError:
            pass


# Состояние многошаговых ответов (выбор плейта → листание персонажей): в callback_data
# влезает только короткий ключ, само состояние — здесь, в памяти. После рестарта кнопки
# старых сообщений просят вызвать команду заново (как панели в Discord).
SESSION_TTL = 2 * 60 * 60
_sessions: dict[str, tuple[float, dict]] = {}


def new_session(data: dict) -> str:
    now = time.time()
    for key in [k for k, (exp, _) in _sessions.items() if exp < now]:
        _sessions.pop(key, None)
    token = secrets.token_urlsafe(6)
    _sessions[token] = (now + SESSION_TTL, data)
    return token


def get_session(token: str) -> dict | None:
    item = _sessions.get(token)
    if not item or item[0] < time.time():
        return None
    return item[1]


async def session_for_callback(ctx: "Ctx", token: str) -> dict | None:
    """Сессия кнопки + проверка, что жмёт тот, кто вызывал команду (в группе кнопки видят все)."""
    state = get_session(token)
    if state is None:
        await ctx.api.answer_callback(ctx.callback_query["id"], "Кнопка устарела — вызовите команду заново.", alert=True)
        return None
    if state.get("user_id") not in (None, ctx.user["id"]):
        await ctx.api.answer_callback(ctx.callback_query["id"], "Это чужая проверка — вызовите команду сами.", alert=True)
        return None
    return state


def esc(text) -> str:
    return html.escape(str(text if text is not None else ""), quote=False)


def denied_text(tier_needed: str, ctx: Ctx) -> str | None:
    have = _TIER_RANK.get(ctx.access["tier"], 0)
    if have >= _REQUIRED_RANK[tier_needed]:
        return None
    if have == 0:
        return (
            "Сначала привяжите свой игровой аккаунт: <code>/reg 123456789</code> — ваш код союзника.\n"
            "Бот работает для гильдий, подключённых к нему, права берутся из игры (офицер/участник)."
        )
    return "🔒 Эта команда — только для офицеров гильдии."


def _parse_command(text: str, bot_username: str) -> tuple[str, str] | None:
    if not text.startswith("/"):
        return None
    head, _, rest = text[1:].partition(" ")
    head, nl, after_nl = head.partition("\n")
    if nl:
        rest = after_nl + (" " + rest if rest else "")
    name, _, target = head.partition("@")
    if target and target.lower() != bot_username.lower():
        return None  # команда другому боту
    return name.lower(), rest


# Инлайн-подсказки «@бот …» — регистрирует tg_bot/inline.py (там поиск и команды).
INLINE_HANDLER: dict = {"handler": None}
# Обычные (не команды) сообщения групп — зеркала Telegram → Discord, регистрирует tg_bot/relay_in.py.
GROUP_MESSAGE_HANDLER: dict = {"handler": None}


async def handle_update(app: App, update: dict):
    try:
        if "message" in update:
            await _handle_message(app, update["message"])
        elif "callback_query" in update:
            await _handle_callback(app, update["callback_query"])
        elif "my_chat_member" in update:
            await _handle_membership(app, update["my_chat_member"])
        elif "inline_query" in update and INLINE_HANDLER["handler"]:
            await INLINE_HANDLER["handler"](app, update["inline_query"])
    except Exception:
        print(f"❌ [TG] Ошибка обработки обновления {update.get('update_id')}:\n{traceback.format_exc()}")


async def _handle_message(app: App, message: dict):
    if message.get("migrate_to_chat_id"):
        store.update_chat_id(message["chat"]["id"], message["migrate_to_chat_id"])
        return
    from tg_bot import topics
    topics.observe(message)
    user = message.get("from")
    if not user or (user.get("is_bot") and user["id"] != ANONYMOUS_ADMIN_ID):
        return
    parsed = _parse_command(message.get("text") or "", app.username)
    if parsed is None:
        if message["chat"].get("type") in ("group", "supergroup") and GROUP_MESSAGE_HANDLER["handler"]:
            await GROUP_MESSAGE_HANDLER["handler"](app, message)
        return
    name, args = parsed
    cmd = COMMANDS.get(name)
    private = message["chat"].get("type") == "private"
    if cmd is None:
        if private:
            await app.api.send_message(message["chat"]["id"], "Не знаю такой команды. Список — /help")
        return
    if user["id"] == ANONYMOUS_ADMIN_ID:
        await app.api.send_message(
            message["chat"]["id"], "Вы пишете анонимно от имени группы — бот не знает, кто вы. Отключите анонимность или напишите боту в личку.",
            reply_to=message.get("message_id"),
        )
        return
    ctx = Ctx(app, message, user, args)
    from tg_bot import command_access
    if command_access.denied(ctx, cmd.name):
        return  # Keep forbidden topics free of denial-message spam as well.
    if cmd.where == "private" and not ctx.is_private:
        await ctx.reply(f"Эта команда работает в личке с ботом: @{esc(app.username)}")
        return
    if cmd.where == "group" and ctx.is_private:
        await ctx.reply("Эта команда — для группы гильдии: добавьте бота в группу и вызовите её там.")
        return
    denial = denied_text(cmd.tier, ctx)
    if denial:
        await ctx.reply(denial)
        return
    database.log_command_usage(f"tg:{cmd.name}", ctx.access["guild_id"])
    await cmd.handler(ctx)


async def _handle_callback(app: App, query: dict):
    data = query.get("data") or ""
    prefix, _, payload = data.partition("|")
    cb = CALLBACKS.get(prefix)
    message = query.get("message")
    if cb is None or not message:
        await app.api.answer_callback(query["id"])
        return
    ctx = Ctx(app, message, query["from"], payload, callback_query=query)
    from tg_bot import topics
    topics.observe(message)
    from tg_bot import command_access
    if command_access.denied(ctx, cb.command_name):
        await app.api.answer_callback(query["id"], "Команда отключена в этой теме. Используйте другую тему или личку бота.", alert=True)
        return
    denial = denied_text(cb.tier, ctx)
    if denial:
        await app.api.answer_callback(query["id"], "Нет доступа: " + ("привяжите аккаунт командой /reg" if ctx.access["tier"] is None else "только офицерам"), alert=True)
        return
    try:
        await cb.handler(ctx, payload)
    finally:
        try:
            await app.api.answer_callback(query["id"])
        except TelegramError:
            pass  # уже отвечено внутри обработчика или кнопка устарела


async def _handle_membership(app: App, event: dict):
    """Бота добавили в группу — подсказываем офицеру, как привязать чат к гильдии."""
    chat = event["chat"]
    new_status = event.get("new_chat_member", {}).get("status")
    old_status = event.get("old_chat_member", {}).get("status")
    if chat.get("type") == "private":
        return
    if new_status in ("left", "kicked"):
        store.unbind_chat(chat["id"])
        return
    if old_status in ("left", "kicked") and new_status in ("member", "administrator"):
        if chat.get("type") == "channel":
            return  # в канал писать подсказку некому
        guild_id = store.chat_guild(chat["id"])
        if guild_id:
            return
        await app.api.send_message(
            chat["id"],
            "Привет! Я «Дроидец» — бот для гильдий SWGOH.\n\n"
            "• Каждый игрок один раз привязывает аккаунт: <code>/reg 123456789</code> (код союзника).\n"
            "• Офицер привязывает этот чат к гильдии: /bind — сюда будут приходить оповещения гильдии.\n"
            "• Все команды — /help",
        )


def menu_commands() -> list[tuple[str, str]]:
    return [(c.name, c.description) for c in COMMAND_ORDER if c.menu]


async def run_polling(app: App):
    offset = None
    while True:
        try:
            updates = await app.api.get_updates(offset)
        except TelegramError as e:
            print(f"⚠️ [TG] getUpdates: {e}")
            await asyncio.sleep(10 if e.error_code == 409 else 5)
            continue
        except Exception as e:
            print(f"⚠️ [TG] getUpdates (сеть): {e!r}")
            await asyncio.sleep(5)
            continue
        for update in updates:
            offset = update["update_id"] + 1
            task = asyncio.create_task(handle_update(app, update))
            app.tasks.add(task)
            task.add_done_callback(app.tasks.discard)
