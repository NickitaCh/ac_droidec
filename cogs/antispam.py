"""Детектор компрометации аккаунта/спам-рассылки: скомпрометированный (обычно
через ворованный токен/self-bot) аккаунт участника рассылает одно и то же
рекламное сообщение сразу в несколько каналов гильдии за секунды — это и
отличает такую рассылку от обычной активности живого человека (см. инцидент
2026-09-01: спам с @everyone + вложением сразу в несколько каналов). Триггер —
кросс-постинг: один автор публикует message с одинаковым (после нормализации)
содержимым/вложениями в N разных каналах за T секунд. Реакция полностью
автоматическая: тайм-аут автора, удаление всех запалившихся сообщений, алерт
офицерам в настраиваемый канал (см. cogs/guild_settings.py::антиспам_канал,
антиспам_таймаут — оба поля per-guild, guilds.antispam_alert_channel_id/
antispam_timeout_minutes).

Фича гейтится флагом antispam_enabled (/настройки антиспам_вкл/антиспам_выкл) —
в отличие от остальных опциональных per-guild фич бота (TB-план, ДР и т.д.), где
гейтом служит наличие канала, тут явный вкл/выкл нужен отдельно от канала: если
включили детектор, но забыли задать канал алертов, удаление/тайм-аут всё равно
должны работать (просто некуда слать уведомление — см. фолбэк в _handle_detected_spam).
antispam_timeout_minutes опционален — при отсутствии берём DEFAULT_TIMEOUT_MINUTES.
antispam_alert_role_id/antispam_alert_message — необязательная кастомизация того,
что писать в алерт и кого дополнительно тегать (см. _render_alert_message).

Состояние отслеживания (кто/что/куда постил) — только в памяти процесса
(self._recent), без БД: это одноразовая эвристика на текущий момент, не история
для отчётов, и переживать рестарт бота ей не нужно."""

import re
import time
from collections import defaultdict
from datetime import timedelta

import disnake
from disnake.ext import commands

import database

CROSS_POST_CHANNEL_THRESHOLD = 3   # столько РАЗНЫХ каналов с одинаковым содержимым — уже рассылка, не совпадение
CROSS_POST_WINDOW_SECONDS = 15     # окно, в котором ищем кросс-постинг
DEFAULT_TIMEOUT_MINUTES = 60       # если officer не задал antispam_timeout_minutes через /настройки
CONTENT_PREVIEW_LIMIT = 300

DEFAULT_ALERT_MESSAGE = (
    "{role}🚨 Похоже на скомпрометированный аккаунт или спам-рассылку: {member} отправил(а) "
    "одинаковое сообщение в {count} каналов ({channels}) за короткое время."
)

_WHITESPACE_RE = re.compile(r"\s+")


def _render_alert_message(template: str, *, member: disnake.Member, channels_hit: list[str], timeout_minutes: int, role_id) -> str:
    """Подстановка плейсхолдеров в кастомный текст алерта — обычный str.replace,
    не str.format, чтобы произвольный текст officer'а с фигурными скобками
    (код-блок, JSON-пример и т.п.) не падал с KeyError на незнакомых {ключах}."""
    role_prefix = f"<@&{role_id}> " if role_id else ""
    text = template.replace("{role}", role_prefix)
    text = text.replace("{member}", member.mention)
    text = text.replace("{channels}", ", ".join(channels_hit))
    text = text.replace("{count}", str(len(channels_hit)))
    text = text.replace("{timeout}", str(timeout_minutes))
    return text.strip()


def _content_key(message: disnake.Message):
    """Нормализованный ключ для сравнения «одинаковости» сообщений — текст (без
    учёта регистра/лишних пробелов) + отсортированный набор (имя, размер)
    вложений, чтобы ловить и рассылку с разным регистром, и голую картинку без
    подписи, если её постят повторно в разные каналы."""
    text = _WHITESPACE_RE.sub(" ", (message.content or "").strip().lower())
    attachments = tuple(sorted((a.filename.lower(), a.size) for a in message.attachments))
    return (text, attachments)


class AntiSpamCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        # (discord_guild_id, author_id) -> {content_key: [(channel_id, message, ts), ...]}
        self._recent: dict[tuple[int, int], dict[tuple, list[tuple[int, disnake.Message, float]]]] = defaultdict(dict)

    def _guild_cfg(self, discord_guild_id: int) -> dict | None:
        return next(
            (cfg for cfg in database.get_all_guild_configs()
             if cfg.get("discord_guild_id") and str(cfg["discord_guild_id"]) == str(discord_guild_id)),
            None,
        )

    @commands.Cog.listener()
    async def on_message(self, message: disnake.Message):
        if message.guild is None or message.author.bot or message.webhook_id is not None:
            return
        key = _content_key(message)
        if not key[0] and not key[1]:
            return  # нечего сравнивать (пустой текст без вложений, напр. только стикер)

        guild_cfg = self._guild_cfg(message.guild.id)
        if not guild_cfg or not guild_cfg.get("antispam_enabled"):
            return

        author_key = (message.guild.id, message.author.id)
        by_content = self._recent[author_key]

        now = time.monotonic()
        # чистим устаревшие записи этого автора по всем content_key разом
        for ck in list(by_content.keys()):
            fresh = [e for e in by_content[ck] if now - e[2] <= CROSS_POST_WINDOW_SECONDS]
            if fresh:
                by_content[ck] = fresh
            else:
                del by_content[ck]

        entries = by_content.setdefault(key, [])
        entries.append((message.channel.id, message, now))

        distinct_channels = {e[0] for e in entries}
        if len(distinct_channels) < CROSS_POST_CHANNEL_THRESHOLD:
            return

        matched = entries
        del by_content[key]
        if not by_content:
            del self._recent[author_key]

        await self._handle_detected_spam(message, guild_cfg, matched)

    async def _handle_detected_spam(self, trigger_message: disnake.Message, guild_cfg: dict, matched):
        member = trigger_message.author
        channels_hit = sorted({m.channel.mention for _, m, _ in matched}, key=str)

        deleted, delete_failed = 0, 0
        for _, msg, _ in matched:
            try:
                await msg.delete()
                deleted += 1
            except (disnake.NotFound, disnake.Forbidden, disnake.HTTPException):
                delete_failed += 1

        timeout_minutes = guild_cfg.get("antispam_timeout_minutes") or DEFAULT_TIMEOUT_MINUTES
        timeout_applied = True
        timeout_note = f"⏱ Тайм-аут на {timeout_minutes} мин. выдан."
        try:
            await member.timeout(
                duration=timedelta(minutes=int(timeout_minutes)),
                reason="Автообнаружение: кросс-постинг одинакового сообщения в несколько каналов "
                       "(похоже на скомпрометированный аккаунт/спам-рассылку)",
            )
        except disnake.Forbidden:
            timeout_applied = False
            timeout_note = "⚠️ Не удалось выдать тайм-аут — у бота не хватает прав (роль ниже роли участника или нет Moderate Members)."
        except disnake.HTTPException as e:
            timeout_applied = False
            timeout_note = f"⚠️ Не удалось выдать тайм-аут — ошибка Discord API: {e}"

        text, attachments = _content_key(trigger_message)
        preview = trigger_message.content.strip() or "*(без текста)*"
        if len(preview) > CONTENT_PREVIEW_LIMIT:
            preview = preview[:CONTENT_PREVIEW_LIMIT] + "…"

        database.log_antispam_incident(
            guild_id=guild_cfg["id"],
            discord_user_id=member.id,
            channels=", ".join(channels_hit),
            messages_deleted=deleted,
            messages_delete_failed=delete_failed,
            timeout_minutes=int(timeout_minutes),
            timeout_applied=timeout_applied,
            content_preview=preview,
        )

        embed = disnake.Embed(
            title="🚨 Обнаружена подозрительная рассылка",
            description=f"{member.mention} (`{member.id}`), {len(channels_hit)} каналов: {', '.join(channels_hit)}",
            color=disnake.Color.red(),
        )
        embed.add_field(name="Содержимое", value=preview, inline=False)
        if attachments:
            embed.add_field(name="Вложения", value="\n".join(f"{n} ({s} байт)" for n, s in attachments), inline=False)
        embed.add_field(
            name="Действия бота",
            value=f"🗑 Удалено сообщений: {deleted}" + (f" (не вышло удалить: {delete_failed})" if delete_failed else "")
                  + f"\n{timeout_note}",
            inline=False,
        )
        embed.set_footer(text="Если это ложное срабатывание — снимите тайм-аут вручную и проверьте настройки /настройки антиспам_*")

        alert_role_id = guild_cfg.get("antispam_alert_role_id")
        alert_content = _render_alert_message(
            guild_cfg.get("antispam_alert_message") or DEFAULT_ALERT_MESSAGE,
            member=member,
            channels_hit=channels_hit,
            timeout_minutes=timeout_minutes,
            role_id=alert_role_id,
        )
        allowed_mentions = disnake.AllowedMentions(everyone=False, roles=[int(alert_role_id)] if alert_role_id else False, users=[member.id])

        channel_id = guild_cfg.get("antispam_alert_channel_id")
        alert_channel = self.bot.get_channel(int(channel_id)) if channel_id else None
        if alert_channel:
            try:
                await alert_channel.send(content=alert_content, embed=embed, allowed_mentions=allowed_mentions)
            except (disnake.Forbidden, disnake.HTTPException) as e:
                print(f"[antispam] не удалось отправить алерт в канал {channel_id}: {e}")
        elif channel_id:
            print(f"[antispam] канал алертов {channel_id} не найден (guild {guild_cfg['id']})")
        else:
            print(f"[antispam] антиспам включён без канала алертов (guild {guild_cfg['id']}) — сработал по {member.id}, "
                  f"тайм-аут/удаление выполнены, алерт послать некуда")


def setup(bot: commands.Bot):
    bot.add_cog(AntiSpamCog(bot))
