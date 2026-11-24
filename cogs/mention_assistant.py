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


class ReminderConfirmation(disnake.ui.View):
    def __init__(self, cog, owner, gid, source_id, plan):
        super().__init__(timeout=120)
        self.cog, self.owner, self.gid = cog, str(owner), gid
        self.source_id, self.plan = str(source_id), dict(plan)

    async def interaction_check(self, inter):
        access = await asyncio.to_thread(assistant.access_for, inter.author.id)
        if str(inter.author.id) != self.owner or not access or access["guild_id"] != self.gid:
            await inter.response.send_message("Подтвердить может только автор с действующим доступом офицера.", ephemeral=True)
            return False
        return True

    @disnake.ui.button(label="Создать личное напоминание", style=disnake.ButtonStyle.success)
    async def confirm(self, button, inter):
        await inter.response.defer(with_message=False)
        if self.is_finished():
            return
        self.stop()
        try:
            # Check DM availability before saving; never fall back to a guild channel.
            user = self.cog.bot.get_user(int(self.owner)) or await self.cog.bot.fetch_user(int(self.owner))
            await user.send("Создаю личное напоминание: " + safe(self.plan["text"])
                            + "\n" + assistant.format_due(assistant.reminder_fields(self.plan["due"], self.plan["text"])[0]),
                            allowed_mentions=NO_MENTIONS)
            access = await asyncio.to_thread(assistant.access_for, self.owner)
            if not access or access["guild_id"] != self.gid:
                raise ValueError("Доступ офицера изменился. Напоминание не создано.")
            reminder_id = await asyncio.to_thread(assistant.add_reminder, self.gid, self.owner,
                                                  self.cog.profile, self.source_id, self.plan["due"], self.plan["text"])
            text = f"Напоминание №{reminder_id} создано. Пришлю в личку " + assistant.format_due(assistant.reminder_fields(self.plan["due"], self.plan["text"])[0]) + "."
        except disnake.Forbidden:
            text = "Не могу написать вам в личку. Разрешите личные сообщения от бота и повторите запрос. Напоминание не создано."
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
        if message.attachments or message.mention_everyone or message.role_mentions or re.search(r"<@!?\d+>|https?://", question, re.I):
            await self.reply(message, "Пришлите обычный текст без вложений, ссылок и других упоминаний.")
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
            text = "Ваши напоминания:\n" + "\n".join(f"№{r['id']} — {assistant.format_due(r['due'])}: {safe(r['text'])}" for r in rows) if rows else "Активных напоминаний нет."
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
            elif plan["action"] == "remind":
                epoch, text = assistant.reminder_fields(plan["due"], plan["text"])
                view = ReminderConfirmation(self, message.author.id, access["guild_id"], message.id, plan)
                await self.reply(message, "Создать напоминание **только вам в личку**?\n"
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
                        user = self.bot.get_user(int(row["owner"])) or await self.bot.fetch_user(int(row["owner"]))
                        await user.send("⏰ Личное напоминание №" + str(row["id"]) + "\n" + safe(row["text"])
                                        + "\n" + assistant.format_due(row["due"]), allowed_mentions=NO_MENTIONS)
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
