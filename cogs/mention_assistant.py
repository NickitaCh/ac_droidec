"""Officer-only, explicitly mentioned assistant. No channel history is read."""

import asyncio
import logging
import re
import time

import disnake
from disnake.ext import commands, tasks

from services import bot_profiles, mention_assistant as assistant

log = logging.getLogger(__name__)
NO_MENTIONS = disnake.AllowedMentions.none()


def safe(text):
    return disnake.utils.escape_mentions(disnake.utils.escape_markdown(text))


async def checked_channel(bot, owner, server_id, channel_id):
    """Only accessible text channels/public threads in the original Discord server."""
    guild = bot.get_guild(int(server_id))
    if guild is None:
        raise ValueError("Сервер напоминания больше недоступен.")
    channel = guild.get_channel_or_thread(int(channel_id))
    if not isinstance(channel, (disnake.TextChannel, disnake.Thread)):
        raise ValueError("Нужен текстовый канал этого сервера или открытая ветка.")
    if isinstance(channel, disnake.Thread) and (channel.is_private() or channel.archived or channel.locked):
        raise ValueError("Нельзя отправить напоминание в закрытую, архивную или приватную ветку.")
    # Fetch current membership rather than trusting a cached role payload.
    member = await guild.fetch_member(int(owner))
    me = await guild.fetch_member(bot.user.id)
    for actor in (member, me):
        perms = channel.permissions_for(actor)
        can_send = perms.send_messages_in_threads if isinstance(channel, disnake.Thread) else perms.send_messages
        if not perms.view_channel or not can_send:
            raise ValueError("У вас или бота нет доступа на просмотр и отправку сообщений в этот канал.")
    return channel


def destination_request(message, question):
    """Destination is extracted from explicit user syntax, never from model output."""
    default = {"channel_id": message.channel.id, "discord_guild_id": message.guild.id} if message.guild else {}
    if not re.search(r'напомни|напоминан', question, re.I):
        return question, default
    explicit = list(re.finditer(r'\bв\s+канал(?:е)?\s+(<#\d+>|«[^»]+»|"[^"]+"|[^\s,;]+)', question, re.I))
    private = re.search(r'\b(?:в\s+личку|в\s+лс|личным\s+сообщением)\b', question, re.I)
    here = re.search(r'\b(?:в\s+этом\s+канале|в\s+тот\s+же\s+канал|сюда|здесь)\b', question, re.I)
    if not (explicit or private or here):
        return question, default
    if len(explicit) > 1 or (explicit and private) or (private and here):
        raise ValueError("Укажите одно место доставки: канал или личку.")
    if private:
        return (question[:private.start()] + question[private.end():]).strip(), {}
    if not message.guild:
        raise ValueError("Напоминание в канал создавайте на нужном сервере, упомянув канал через #.")
    destination = message.channel
    span = here
    if explicit:
        span = explicit[0]
        raw = span.group(1)
        match = re.fullmatch(r'<#(\d+)>', raw)
        if match:
            destination = message.guild.get_channel_or_thread(int(match[1]))
        else:
            name = raw.strip('#«»"').casefold()
            candidates = [c for c in message.guild.text_channels if c.name.casefold() == name]
            if len(candidates) != 1:
                raise ValueError("Канал не найден или название неоднозначно. Выберите его через #.")
            destination = candidates[0]
        if destination is None:
            raise ValueError("Канал не найден на этом сервере. Выберите его через #.")
    return (question[:span.start()] + question[span.end():]).strip(), {"channel_id": destination.id, "discord_guild_id": message.guild.id}


def destination_label(destination):
    return f"в канал <#{destination['channel_id']}>" if destination.get("channel_id") else "вам в личку"


class ReminderConfirmation(disnake.ui.View):
    def __init__(self, cog, owner, gid, source_id, plan, destination=None):
        super().__init__(timeout=120)
        self.cog, self.owner, self.gid = cog, str(owner), gid
        self.source_id, self.plan = str(source_id), dict(plan)
        self.destination = dict(destination or {})

    async def interaction_check(self, inter):
        access = await asyncio.to_thread(assistant.access_for, inter.author.id)
        if str(inter.author.id) != self.owner or not access or access["guild_id"] != self.gid:
            await inter.response.send_message("Подтвердить может только автор с действующим доступом офицера.", ephemeral=True)
            return False
        return True

    @disnake.ui.button(label="Создать напоминание", style=disnake.ButtonStyle.success)
    async def confirm(self, button, inter):
        await inter.response.defer(with_message=False)
        if self.is_finished():
            return
        self.stop()
        try:
            if self.destination:
                await checked_channel(self.cog.bot, self.owner, self.destination["discord_guild_id"], self.destination["channel_id"])
            else:
                user = self.cog.bot.get_user(int(self.owner)) or await self.cog.bot.fetch_user(int(self.owner))
                await user.send("Создаю личное напоминание: " + safe(self.plan["text"])
                                + "\n" + assistant.format_due(assistant.reminder_fields(self.plan["due"], self.plan["text"])[0]),
                                allowed_mentions=NO_MENTIONS)
            access = await asyncio.to_thread(assistant.access_for, self.owner)
            if not access or access["guild_id"] != self.gid:
                raise ValueError("Доступ офицера изменился. Напоминание не создано.")
            reminder_id = await asyncio.to_thread(assistant.add_reminder, self.gid, self.owner,
                                                  self.cog.profile, self.source_id, self.plan["due"], self.plan["text"], **self.destination)
            text = f"Напоминание №{reminder_id} создано. Пришлю {destination_label(self.destination)} " + assistant.format_due(assistant.reminder_fields(self.plan["due"], self.plan["text"])[0]) + "."
        except disnake.Forbidden:
            text = "Место доставки недоступно. Проверьте права канала или разрешите личные сообщения от бота. Напоминание не создано."
        except ValueError as exc:
            text = str(exc)
        except Exception:
            log.exception("Assistant reminder confirmation failed")
            text = "Не удалось создать напоминание. Проверьте «мои напоминания» перед повтором."
        await inter.edit_original_response(content=text, view=None, allowed_mentions=NO_MENTIONS)

    @disnake.ui.button(label="Отмена", style=disnake.ButtonStyle.secondary)
    async def cancel(self, button, inter):
        await inter.response.defer(with_message=False)
        if self.is_finished():
            return
        self.stop()
        await inter.edit_original_response(content="Напоминание не создано.", view=None)


class MentionAssistantCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.profile = bot_profiles.worker_profile() or bot_profiles.runtime_profile()
        self.ai_lock = asyncio.Lock()
        self.reference_cache = {}
        self.reminders.start()

    def cog_unload(self):
        self.reminders.cancel()

    async def reply(self, message, text, **kwargs):
        return await message.reply(text, mention_author=False, allowed_mentions=NO_MENTIONS, **kwargs)

    @commands.Cog.listener()
    async def on_message(self, message):
        if (not self.bot.user or message.author.bot or message.webhook_id
                or message.type not in (disnake.MessageType.default, disnake.MessageType.reply)):
            return
        # Explicit mention only: replies that auto-mention the bot aren't commands.
        marker = re.compile(r"<@!?" + str(self.bot.user.id) + r">")
        if not marker.search(message.content or ""):
            return
        try:
            await self.handle(message, marker)
        except disnake.HTTPException:
            log.warning("Assistant could not reply to Discord message %s", message.id)
        except Exception:
            log.exception("Assistant request failed for message %s", message.id)
            await self.reply(message, "Не удалось обработать запрос. Попробуйте позже.")

    async def handle(self, message, marker):
        # A Cog listener bypasses slash checks: always resolve access here first.
        access = await asyncio.to_thread(assistant.access_for, message.author.id)
        if not access:
            # Silent for non-officers: no AI calls and no denial-message spam.
            return
        question = marker.sub("", message.content).strip()
        if len(question) > assistant.MAX_QUESTION:
            await self.reply(message, "Сократите запрос до 700 символов.")
            return
        if assistant.injection_request(question):
            await self.reply(message, "Могу помочь по SWGOH и гильдейским делам. Служебные инструкции и секреты недоступны.")
            return
        if message.attachments or message.mention_everyone or message.role_mentions or re.search(r"<@!?\d+>|https?://", question, re.I):
            await self.reply(message, "Пришлите обычный текст без вложений, ссылок и других упоминаний.")
            return
        try:
            question, destination = destination_request(message, question)
            if "<#" in question:
                raise ValueError("Укажите канал в форме «в канале #название».")
        except ValueError as exc:
            await self.reply(message, str(exc))
            return
        action, value = assistant.local_action(question)
        cache_key = question.casefold()
        cached = self.reference_cache.get(cache_key)
        if cached and time.monotonic() - cached[0] > 300:
            cached = None
        needs_ai = bool(question) and action is None and cached is None
        if needs_ai and not self.bot.openrouter_api_key:
            await self.reply(message, "ИИ сейчас не настроен. " + assistant.HELP)
            return
        if needs_ai and self.ai_lock.locked():
            await self.reply(message, "Помощник обрабатывает другой вопрос. Повторите чуть позже.")
            return
        allowed = await asyncio.to_thread(assistant.reserve_request, message.author.id, access["guild_id"],
                                           self.bot.AI_ASSISTANT_LIMITS, ai=needs_ai)
        if not allowed:
            await self.reply(message, "Лимит помощника: не чаще раза в 30 секунд, до 10 обращений в день на человека и 25 на гильдию. Попробуйте позже.")
            return
        if not question or action == "help":
            await self.reply(message, assistant.HELP)
            return
        if action == "list":
            rows = await asyncio.to_thread(assistant.list_reminders, access["guild_id"], message.author.id, self.profile)
            text = "Ваши напоминания:\n" + "\n".join(f"№{r['id']} — {assistant.format_due(r['due'])}, {destination_label(r)}: {safe(r['text'])}" for r in rows) if rows else "Активных напоминаний нет."
            # List may exceed a Discord message; private chunks never ping anyone.
            for start in range(0, len(text), 1800):
                await message.author.send(text[start:start + 1800], allowed_mentions=NO_MENTIONS)
            if message.guild:
                await self.reply(message, "Список напоминаний отправлен в личку.")
            return
        if action == "cancel":
            cancelled = await asyncio.to_thread(assistant.cancel_reminder, access["guild_id"], message.author.id, self.profile, value)
            await self.reply(message, "Напоминание отменено." if cancelled else "Такого активного напоминания у вас нет.")
            return
        try:
            if needs_ai and self.ai_lock.locked():
                await self.reply(message, "Помощник обрабатывает другой вопрос. Повторите чуть позже.")
                return
            if cached:
                plan, effects, docs = cached[1:]
            else:
                async with self.ai_lock:
                    async with message.channel.typing():
                        plan, effects, docs = await asyncio.to_thread(assistant.parse_plan, question, self.profile,
                                                                     self.bot.openrouter_api_key, self.bot.AI_ASSISTANT_LIMITS)
                if plan["action"] in {"effect", "docs"}:
                    if len(self.reference_cache) >= 128:
                        self.reference_cache.pop(next(iter(self.reference_cache)))
                    self.reference_cache[cache_key] = (time.monotonic(), plan, effects, docs)
            # Long model calls must not preserve permissions revoked mid-request.
            current = await asyncio.to_thread(assistant.access_for, message.author.id)
            if not current or current["guild_id"] != access["guild_id"]:
                return
            if plan["action"] == "help":
                await self.reply(message, assistant.HELP + "\nДля напоминания укажите точное время; если нужна дата — укажите и её.")
            elif plan["action"] == "answer":
                await self.reply(message, plan["text"])
            elif plan["action"] == "remind":
                epoch, text = assistant.reminder_fields(plan["due"], plan["text"])
                if destination:
                    await checked_channel(self.bot, message.author.id, destination["discord_guild_id"], destination["channel_id"])
                view = ReminderConfirmation(self, message.author.id, access["guild_id"], message.id, plan, destination)
                await self.reply(message, f"Создать напоминание **{destination_label(destination)}**?\n"
                                 + assistant.format_due(epoch) + "\n" + safe(text) + "\nПодтверждение действует 2 минуты.", view=view)
            else:
                text = await asyncio.to_thread(assistant.render_reference, plan, effects, docs, current)
                await self.reply(message, text)
        except ValueError as exc:
            await self.reply(message, str(exc))
        except Exception:
            log.warning("Assistant model request failed", exc_info=True)
            await self.reply(message, "ИИ сейчас не ответил. Попробуйте позже. Просмотр и отмена напоминаний работают без ИИ.")

    @tasks.loop(seconds=30)
    async def reminders(self):
        try:
            rows = await asyncio.to_thread(assistant.claim_due, self.profile)
            for row in rows:
                status = "failed"
                try:
                    access = await asyncio.to_thread(assistant.access_for, row["owner"])
                    if not access or access["guild_id"] != row["guild_id"] or time.time() - row["due"] > 86400:
                        status = "cancelled"
                    else:
                        if row.get("channel_id"):
                            recipient = await checked_channel(self.bot, row["owner"], row["discord_guild_id"], row["channel_id"])
                        else:
                            recipient = self.bot.get_user(int(row["owner"])) or await self.bot.fetch_user(int(row["owner"]))
                        prefix = f"<@{row['owner']}> " if row.get("channel_id") else ""
                        mentions = disnake.AllowedMentions(users=[disnake.Object(id=int(row["owner"]))], roles=False, everyone=False) if prefix else NO_MENTIONS
                        await recipient.send("⏰ " + prefix + "Напоминание №" + str(row["id"]) + "\n" + safe(row["text"])
                                             + "\n" + assistant.format_due(row["due"]), allowed_mentions=mentions)
                        status = "sent"
                except Exception:
                    log.warning("Assistant reminder %s could not be delivered", row["id"], exc_info=True)
                finally:
                    await asyncio.to_thread(assistant.finish_reminder, row["id"], status)
        except Exception:
            log.exception("Assistant reminder poll failed")

    @reminders.before_loop
    async def before_reminders(self):
        await self.bot.wait_until_ready()


def setup(bot):
    bot.add_cog(MentionAssistantCog(bot))
