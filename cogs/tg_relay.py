"""Копии сообщений Discord в Telegram-ветку гильдии (идея NicolozZ 2026-10-29: у многих
VPN не всегда включён, в Telegram уведомления доходят стабильнее).

  * `/тг канал: <метка> сообщение: <ссылка или текст>` — офицер отправляет копию одного
    сообщения (ордер ВГ, объявление) в ветку Telegram с этой меткой;
  * зеркала каналов — всё, что пишут в выбранном Discord-канале, уходит копией в ветку.
    Настраиваются на сайте (/settings → Telegram), включаются общим тумблером гильдии
    (флаг tg_relay_auto, по умолчанию выключен) и паузой у каждого зеркала.

Метку ветке даёт офицер в самом Telegram: /topic вг в нужной ветке группы (tg_bot/notifier.py).
Сам бот в Telegram не пишет — кладёт копию в очередь tg_bot/outbox.py (enqueue_relay).
Свои сообщения бота не зеркалятся: у его оповещений в Telegram есть свои виды (/notify).
Правки и удаления в Discord в Telegram не переносятся."""

import asyncio
import re

import disnake
from disnake.ext import commands, tasks

import database
import guild_resolver
from services import feature_flags
from services.tb_order_store import parse_message_link
from tg_bot import outbox

FEATURE = "tg_relay_auto"
MAX_ATTACHMENTS = 10
ATTACHMENT_MAX_BYTES = 20 * 1024 * 1024  # больше — ссылкой в тексте (лимит Bot API на загрузку 50 МБ)

_USER_MENTION_RE = re.compile(r"<@!?(\d+)>")
_ROLE_MENTION_RE = re.compile(r"<@&(\d+)>")
_CHANNEL_MENTION_RE = re.compile(r"<#(\d+)>")
_CUSTOM_EMOJI_RE = re.compile(r"<a?:(\w+):\d+>")
_EVERYONE_RE = re.compile(r"@(?:everyone|here)\b")


def relay_payload(message: disnake.Message, guild_id: int) -> tuple[str, list[dict]]:
    """Текст для Telegram (Discord-разметка, её переводит tg_bot/notifier.py) и вложения.
    Упоминание игрока становится Telegram-упоминанием, если у него есть Telegram (привязка
    к профилю или регистрация в боте на тот же код союзника, outbox.tg_owner_for), иначе — имя.
    Роли и @everyone/@here из текста убираются, а их участники перечисляются строкой в конце:
    с Telegram — упоминанием, без него — ником Discord (в Telegram нет ролей; просьба из
    TG-ветки 2026-10-29; @everyone = зарегистрированные игроки гильдии). Каналы — по имени."""
    users = {str(u.id): u.display_name for u in message.mentions}
    channels = {str(c.id): getattr(c, "name", "канал") for c in message.channel_mentions}
    tagged: list[str] = []  # упомянутые в тексте — в строку тегов не повторяем

    def tag(discord_id, name):
        owner = outbox.tg_owner_for(discord_id)
        return f"<@{owner}>" if owner else f"@{name}"

    def user(m):
        uid = m.group(1)
        value = tag(uid, users.get(uid, "игрок"))
        tagged.append(value)
        return value

    group: list[str] = []
    if message.mention_everyone:
        for owner in outbox.guild_owners(guild_id):
            if outbox.is_tg_owner(owner):
                group.append(f"<@{owner}>")
                continue
            member = message.guild.get_member(int(owner)) if message.guild else None
            if member is not None:  # ушедших с сервера не перечисляем
                group.append(tag(owner, member.display_name))
    for role in message.role_mentions:
        for member in getattr(role, "members", []):
            if not member.bot:
                group.append(tag(member.id, member.display_name))

    text = message.content or ""
    text = _USER_MENTION_RE.sub(user, text)
    text = _ROLE_MENTION_RE.sub("", text)
    text = _EVERYONE_RE.sub("", text)
    text = _CHANNEL_MENTION_RE.sub(lambda m: f"#{channels.get(m.group(1), 'канал')}", text)
    text = _CUSTOM_EMOJI_RE.sub("", text)
    text = "\n".join(line.rstrip() for line in text.split("\n")).strip()
    # Сообщения других ботов часто без текста, всё в эмбедах. Превью ссылок (YouTube и т. п.)
    # пропускаем: Telegram покажет своё, а их текст в копии выглядит мусором.
    for embed in message.embeds:
        if getattr(embed, "type", "rich") != "rich":
            continue
        part = "\n".join(p for p in (f"**{embed.title}**" if embed.title else "", embed.description or "") if p)
        if part:
            text = f"{text}\n\n{part}" if text else part

    attachments, too_big = [], []
    for att in message.attachments[:MAX_ATTACHMENTS]:
        if att.size > ATTACHMENT_MAX_BYTES:
            too_big.append(f"{att.filename}: {att.url}")
        else:
            attachments.append({"url": att.url, "filename": att.filename, "content_type": att.content_type})
    if too_big:
        text = "\n".join([text, "Большие файлы — по ссылке:", *too_big]).strip()

    tags = list(dict.fromkeys(o for o in group if o not in tagged))
    if tags:
        text = f"{text}\n\n" + " ".join(tags)

    # Сообщение самого бота (ордер ВИ по ссылке) — без подписи: в Telegram оно и так от бота.
    me = getattr(message.guild, "me", None)
    if me is not None and message.author.id == me.id:
        return text, attachments
    author = getattr(message.author, "display_name", None) or message.author.name
    text = f"**{author}**:\n{text}" if text.strip() else f"**{author}**"
    return text, attachments


async def forward_sent(message: disnake.Message | None, guild_id: int, kind: str) -> None:
    """Пост бота в Discord — той же копией в Telegram, если у гильдии включён этот вид
    оповещений (маршрут tg_routes, /settings → Telegram). Теги — как у зеркал (relay_payload).
    Ошибка не мешает посту в Discord."""
    if message is None:
        return
    try:
        if not await asyncio.to_thread(outbox.guild_wants, guild_id, kind):
            return
        text, attachments = await asyncio.to_thread(relay_payload, message, guild_id)
        await asyncio.to_thread(outbox.enqueue_guild, guild_id, kind, text, attachments)
    except Exception as e:
        print(f"⚠️ [TG] guild {guild_id} {kind}: {e!r}")


async def autocomplete_topics(inter: disnake.ApplicationCommandInteraction, current: str):
    guild_id = guild_resolver.autocomplete_guild_id(inter)
    if guild_id is None:
        return []
    needle = (current or "").strip().lower()
    topics = await asyncio.to_thread(outbox.guild_topics, guild_id)
    return {
        f"{t['label']} — {outbox.topic_place(t)}"[:100]: t["label"]
        for t in topics if needle in t["label"]
    }


class TgRelayCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        # {discord_channel_id: [(guild_id, label), ...]} — только включённые зеркала гильдий с
        # включённым тумблером; правки с сайта подхватываются за минуту.
        self.mirrors: dict[int, list[tuple[int, str]]] = {}
        self.refresh_mirrors.start()

    def cog_unload(self):
        self.refresh_mirrors.cancel()

    @tasks.loop(minutes=1)
    async def refresh_mirrors(self):
        def load():
            mirrors, flags = {}, {}
            for row in outbox.relay_channels():
                if not row["enabled"]:
                    continue
                gid = row["guild_id"]
                if gid not in flags:
                    flags[gid] = feature_flags.is_enabled(gid, FEATURE)
                if flags[gid]:
                    mirrors.setdefault(int(row["discord_channel_id"]), []).append((gid, row["label"]))
            return mirrors

        try:
            self.mirrors = await asyncio.to_thread(load)
        except Exception as e:
            print(f"⚠️ [TG зеркала] Не удалось обновить список: {e!r}")

    @refresh_mirrors.before_loop
    async def _before_refresh(self):
        await self.bot.wait_until_ready()

    @commands.Cog.listener()
    async def on_message(self, message: disnake.Message):
        targets = self.mirrors.get(message.channel.id)
        if not targets or message.guild is None:
            return
        if self.bot.user and message.author.id == self.bot.user.id:
            return
        if message.type not in (disnake.MessageType.default, disnake.MessageType.reply):
            return
        if not (message.content or message.attachments or message.embeds):
            return
        payloads = {}
        for guild_id, label in targets:
            try:
                if guild_id not in payloads:
                    payloads[guild_id] = await asyncio.to_thread(relay_payload, message, guild_id)
                text, attachments = payloads[guild_id]
                sent = await asyncio.to_thread(outbox.enqueue_relay, guild_id, label, text, attachments)
                if sent is None:
                    print(f"⚠️ [TG зеркала] guild {guild_id}: метки «{label}» нет в Telegram (/topic)")
            except Exception as e:
                print(f"⚠️ [TG зеркала] guild {guild_id} → «{label}»: {e!r}")

    async def _fetch_linked(self, inter, guild_id: int, link: str) -> disnake.Message:
        ids = parse_message_link(link)
        channel_id, message_id = int(ids[0]), int(ids[1])
        channel = self.bot.get_channel(channel_id) or await self.bot.fetch_channel(channel_id)
        # Только сообщения с сервера своей гильдии — иначе офицер мог бы вытащить чужой канал,
        # который бот видит на другом сервере.
        cfg = database.get_guild_config(guild_id) or {}
        channel_guild = getattr(channel, "guild", None)
        if (channel_guild is None or str(channel_guild.id) != str(cfg.get("discord_guild_id"))) \
                and not guild_resolver.is_super_admin(inter.author):
            raise PermissionError
        return await channel.fetch_message(message_id)

    @commands.slash_command(name="тг", description="Отправить копию сообщения в ветку Telegram-группы гильдии")
    async def tg(
        self,
        inter: disnake.ApplicationCommandInteraction,
        канал: str = commands.Param(description="Метка ветки в Telegram (задаётся там командой /topic)",
                                    autocomplete=autocomplete_topics),
        сообщение: str = commands.Param(description="Ссылка на сообщение Discord или свой текст"),
    ):
        guild_id = await guild_resolver.require_guild_id(inter)
        if guild_id is None:
            return
        await inter.response.defer(ephemeral=True)
        label = outbox.normalize_label(канал)
        topic = await asyncio.to_thread(outbox.topic, guild_id, label)
        if topic is None:
            known = ", ".join(t["label"] for t in await asyncio.to_thread(outbox.guild_topics, guild_id))
            await inter.edit_original_response(
                f"❌ В Telegram нет ветки с меткой «{label}». "
                + (f"Есть: {known}. " if known else "")
                + "Метку задаёт офицер в нужной ветке группы: /topic вг (группа привязана к гильдии через /bind)."
            )
            return
        if parse_message_link(сообщение):
            try:
                message = await self._fetch_linked(inter, guild_id, сообщение)
            except PermissionError:
                await inter.edit_original_response("❌ Можно пересылать только сообщения с сервера своей гильдии.")
                return
            except (disnake.NotFound, disnake.Forbidden):
                await inter.edit_original_response("❌ Сообщение не найдено или у бота нет доступа к этому каналу.")
                return
            text, attachments = await asyncio.to_thread(relay_payload, message, guild_id)
        else:
            text, attachments = f"**{inter.author.display_name}**:\n{сообщение}", []
        await asyncio.to_thread(outbox.enqueue_relay, guild_id, label, text, attachments)
        extra = f", вложений: {len(attachments)}" if attachments else ""
        await inter.edit_original_response(
            f"✅ Отправлено в Telegram → «{label}» ({outbox.topic_place(topic)}{extra}). Дойдёт за несколько секунд."
        )


def setup(bot):
    bot.add_cog(TgRelayCog(bot))
