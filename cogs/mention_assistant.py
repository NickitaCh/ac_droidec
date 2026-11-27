"""Officer-only assistant with isolated direct dialogue; no channel history reads."""

import asyncio
import logging
import re
import time

import disnake
from disnake.ext import commands, tasks

from services import assistant_context, bot_profiles, mention_assistant as assistant, unit_abilities
from services.sync_queue import complete_on_cancel

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


def answer_embed(pages, index, footer):
    embed = disnake.Embed(description=pages[index], color=0x5865F2)
    embed.set_footer(text=(f'Страница {index+1}/{len(pages)} · ' if len(pages)>1 else '') + footer)
    return embed


class AnswerPages(disnake.ui.View):
    def __init__(self, owner, gid, pages, footer):
        super().__init__(timeout=600)
        self.owner, self.gid, self.pages, self.footer = str(owner), gid, pages, footer
        self.index = 0
        self.update_buttons()

    def update_buttons(self):
        self.previous.disabled = self.index == 0
        self.next.disabled = self.index >= len(self.pages)-1

    async def interaction_check(self, inter):
        access = await asyncio.to_thread(assistant.access_for, inter.author.id)
        if str(inter.author.id) == self.owner and access and access['guild_id'] == self.gid:
            return True
        await inter.response.send_message('Эти страницы доступны автору с доступом офицера АС.', ephemeral=True)
        return False

    @disnake.ui.button(label='Назад', style=disnake.ButtonStyle.secondary)
    async def previous(self, button, inter):
        self.index = max(0, self.index-1)
        self.update_buttons()
        await inter.response.edit_message(embed=answer_embed(self.pages, self.index, self.footer), view=self)

    @disnake.ui.button(label='Дальше', style=disnake.ButtonStyle.secondary)
    async def next(self, button, inter):
        self.index = min(len(self.pages)-1, self.index+1)
        self.update_buttons()
        await inter.response.edit_message(embed=answer_embed(self.pages, self.index, self.footer), view=self)


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
        self.usage_checks = {}
        self.reminders.start()
        if self.profile == 'test':
            self.ability_catalog.start()

    def cog_unload(self):
        self.reminders.cancel()
        self.ability_catalog.cancel()

    async def reply(self, message, text, **kwargs):
        return await message.reply(text, mention_author=False, allowed_mentions=NO_MENTIONS, **kwargs)

    async def send_pages(self, message, pages, access, *, fresh_quota=False):
        snapshot = await asyncio.to_thread(assistant.usage_snapshot, message.author.id, access['guild_id'],
                                           self.bot.AI_ASSISTANT_LIMITS, self.bot.openrouter_api_key, refresh=fresh_quota)
        footer = assistant.usage_footer(snapshot)
        view = AnswerPages(message.author.id, access['guild_id'], pages, footer) if len(pages) > 1 else None
        await self.reply(message, None, embed=answer_embed(pages, 0, footer), view=view)
        question = re.sub(r'<@!?' + str(self.bot.user.id) + r'>', '', message.content).strip()
        if question and not assistant.injection_request(question):
            self.dialogue().remember(self.dialogue_key(message, access), question, '\n\n'.join(pages))

    def dialogue(self):
        if not hasattr(self, '_dialogue'):
            self._dialogue = assistant_context.Dialogue()
        return self._dialogue

    @staticmethod
    def dialogue_key(message, access):
        return (access['guild_id'], message.channel.id, message.author.id)

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
        if action == 'social':
            await self.reply(message, value)
            return
        if action == 'usage':
            now = time.monotonic()
            if now - self.usage_checks.get(message.author.id, -10) < 5:
                return
            self.usage_checks[message.author.id] = now
            snapshot = await asyncio.to_thread(assistant.usage_snapshot, message.author.id, access['guild_id'],
                                               self.bot.AI_ASSISTANT_LIMITS, self.bot.openrouter_api_key)
            await self.reply(message, None, embed=disnake.Embed(description=assistant.render_usage(snapshot, self.bot.AI_ASSISTANT_LIMITS), color=0x5865F2))
            return
        history = self.dialogue().get(self.dialogue_key(message, access))
        context = await asyncio.to_thread(assistant_context.build, access) if action is None and question else {}
        # A short follow-up can refer to different characters for different people.
        cache_key = (question.casefold(), tuple(t['question'].casefold() for t in history))
        cached = self.reference_cache.get(cache_key)
        if cached and time.monotonic() - cached[0] > 300:
            cached = None
        local = await asyncio.to_thread(assistant.local_plan, question, context=context, history=history) if question and action is None and not cached else None
        needs_ai = bool(question) and action is None and cached is None and local is None
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
        if action is None and question:
            refreshed = await assistant_context.refresh_event(message.author.id, access, self.profile, question, context)
            if refreshed:
                context = await asyncio.to_thread(assistant_context.build, access)
                local = await asyncio.to_thread(assistant.local_plan, question, context=context, history=history)
        if not question or action == "help":
            await self.reply(message, assistant.HELP)
            return
        if action == 'ability_info':
            data = await asyncio.to_thread(unit_abilities.load)
            text = ('Да. У меня есть **описания способностей из Comlink**, включая максимальные уровни и ульты. '
                    'Использую игровой текст, а не догадки модели.\n\n'
                    'Например: «какими скилами СЛКР не промахивается?» или «способности Доктора Афры».') if data['units'] else (
                    'Справочник описаний способностей из Comlink сейчас готовится. Повторите вопрос после его обновления.')
            await self.send_pages(message, [text], access)
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
            elif local:
                plan, effects, docs = local, {}, []
            else:
                async with self.ai_lock:
                    async with message.channel.typing():
                        plan, effects, docs = await asyncio.to_thread(assistant.parse_plan, question, self.profile,
                                                                     self.bot.openrouter_api_key, self.bot.AI_ASSISTANT_LIMITS,
                                                                     context=context, history=history)
                if plan["action"] in {"effect", "docs", "abilities"}:
                    if len(self.reference_cache) >= 128:
                        self.reference_cache.pop(next(iter(self.reference_cache)))
                    self.reference_cache[cache_key] = (time.monotonic(), plan, effects, docs)
            # Long model calls must not preserve permissions revoked mid-request.
            current = await asyncio.to_thread(assistant.access_for, message.author.id)
            if not current or current["guild_id"] != access["guild_id"]:
                return
            if plan["action"] == "help":
                await self.send_pages(message, ['Могу помочь с вопросами по SWGOH, делами гильдии, игровыми шутками и напоминаниями. '
                                               'Уточните персонажа или задачу. Для напоминания нужны дата и время.'], current, fresh_quota=needs_ai)
            elif plan["action"] == "answer":
                await self.send_pages(message, [assistant.format_answer(plan["text"])], current, fresh_quota=needs_ai)
            elif plan['action'] == 'abilities':
                data = await asyncio.to_thread(unit_abilities.load)
                pages = await asyncio.to_thread(unit_abilities.evidence_pages, plan, data, question)
                await self.send_pages(message, pages, current, fresh_quota=needs_ai)
            elif plan["action"] == "remind":
                epoch, text = assistant.reminder_fields(plan["due"], plan["text"])
                if destination:
                    await checked_channel(self.bot, message.author.id, destination["discord_guild_id"], destination["channel_id"])
                view = ReminderConfirmation(self, message.author.id, access["guild_id"], message.id, plan, destination)
                await self.reply(message, f"Создать напоминание **{destination_label(destination)}**?\n"
                                 + assistant.format_due(epoch) + "\n" + safe(text) + "\nПодтверждение действует 2 минуты.", view=view)
            else:
                pages = await asyncio.to_thread(assistant.reference_pages, plan, effects, docs, current)
                await self.send_pages(message, pages, current, fresh_quota=needs_ai)
        except ValueError as exc:
            await self.reply(message, str(exc))
        except Exception:
            log.warning("Assistant model request failed", exc_info=True)
            await self.reply(message, "ИИ сейчас не ответил. Попробуйте позже. Просмотр и отмена напоминаний работают без ИИ.")

    async def _send_reminder(self, row, recipient):
        prefix = f"<@{row['owner']}> " if row.get("channel_id") else ""
        mentions = disnake.AllowedMentions(users=[disnake.Object(id=int(row["owner"]))], roles=False,
                                           everyone=False) if prefix else NO_MENTIONS
        text = f"⏰ {prefix}Напоминание №{row['id']}\n{safe(row['text'])}\n{assistant.format_due(row['due'])}"
        if not await asyncio.to_thread(assistant.begin_reminder_send, row["id"], row["claimed_at"]):
            return
        status = "sent"
        try:
            await recipient.send(text, allowed_mentions=mentions)
        except Exception:
            status = "failed"
            log.warning("Assistant reminder %s could not be delivered", row["id"], exc_info=True)
        await asyncio.to_thread(assistant.finish_reminder, row["id"], status, claimed_at=None)

    @tasks.loop(seconds=30)
    async def reminders(self):
        try:
            rows = await asyncio.to_thread(assistant.claim_due, self.profile)
            for row in rows:
                try:
                    access = await asyncio.to_thread(assistant.access_for, row["owner"])
                    if not access or access["guild_id"] != row["guild_id"] or time.time() - row["due"] > 86400:
                        await asyncio.to_thread(assistant.finish_reminder, row["id"], "cancelled",
                                                claimed_at=row["claimed_at"])
                        continue
                    if row.get("channel_id"):
                        recipient = await checked_channel(self.bot, row["owner"], row["discord_guild_id"], row["channel_id"])
                    else:
                        recipient = self.bot.get_user(int(row["owner"])) or await self.bot.fetch_user(int(row["owner"]))
                except (ValueError, disnake.Forbidden, disnake.NotFound):
                    await asyncio.to_thread(assistant.finish_reminder, row["id"], "failed", claimed_at=row["claimed_at"])
                    continue
                except Exception:
                    # No message was sent. The claim can be recovered after the lease expires.
                    log.warning("Assistant reminder %s could not be prepared", row["id"], exc_info=True)
                    continue
                await complete_on_cancel(self._send_reminder(row, recipient))
        except Exception:
            log.exception("Assistant reminder poll failed")

    @reminders.before_loop
    async def before_reminders(self):
        await self.bot.wait_until_ready()

    @tasks.loop(hours=1)
    async def ability_catalog(self):
        try:
            await unit_abilities.refresh(self.bot.comlink)
        except Exception:
            log.exception('Ability catalog refresh failed')

    @ability_catalog.before_loop
    async def before_ability_catalog(self):
        await self.bot.wait_until_ready()


def setup(bot):
    bot.add_cog(MentionAssistantCog(bot))
