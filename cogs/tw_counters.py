"""/вг_ордер — быстрый сборщик ордера по Территориальной Войне: для каждой из до
5 локаций офицер выбирает вражеский пак (автокомплит по guilds.tw_guide_forum_
channel_id) и нашу контру на него (автокомплит, зависит от выбранного пака),
плюс свободный текст локации. Команда только возвращает готовый текст ответом
(ephemeral) — офицер сам копирует его в ВГ-канал, бот сам ничего не постит.

База контр наполняется периодическим синком форум-канала гайдов (каждый тред
форума = один вражеский пак, каждое сообщение внутри = один вариант контры на
него, либо на конкретный вариант пака, если сообщение начинается со своего
"## <вариант>" — см. _parse_counter_message)."""

import re

import disnake
from disnake.ext import commands, tasks

import database
import guild_resolver

# ------------------ Разбор сообщений с контрами ------------------
# Формат сообщений в гайд-канале неоднороден (разные офицеры, годы): от строгого
# "## <вариант>" / "### - <лидер>" / "**Состав:**" / "**Датакрон:**" / "**Ход
# боя:**" / "**Команда:**" до голой прозы без всякой разметки (старый формат
# 2023-2024). Парсер разбирает то, что размечено, и падает обратно на raw_text
# (parsed_ok=False), если структуры не нашлось — никогда не теряет исходный текст.
HEADER_LINE_RE = re.compile(r'^#{1,4}\s*(.+)$', re.MULTILINE)
# \b перед "Состав" обязателен — без него ловятся ложные срабатывания на словах
# вроде "оставить"/"заставить", которые содержат подстроку "остав".
COMPOSITION_RE = re.compile(r'^\*{0,2}\b[Cс]остав\w*:?\*{0,2}\s*(.+)$', re.IGNORECASE | re.MULTILINE)
# Датакрон/Ход боя — многострочные поля, читаем не-жадно от заголовка (в начале
# СВОЕЙ строки — иначе "дк"/"состав" как случайное слово в тексте до заголовка
# ложно матчится) до следующего известного маркера или конца сообщения.
DATACRON_RE = re.compile(
    r'^\*{0,2}\b(?:Датакрон\w*|ДК)\b\s*:?\*{0,2}[ \t]*\n?(.*?)'
    r'(?=^\s*\*{0,2}\b(?:Ход\s+боя|Команда|Видео)\b|\Z)',
    re.IGNORECASE | re.MULTILINE | re.DOTALL
)
BATTLE_PLAN_RE = re.compile(
    r'^\*{0,2}\bХод\s+боя\b\s*:?\*{0,2}[ \t]*\n?(.*?)'
    r'(?=^\s*\*{0,2}\bКоманда\b|^\s*\*{0,2}\bВидео\b|\Z)',
    re.IGNORECASE | re.MULTILINE | re.DOTALL
)
TEAM_CODE_RE = re.compile(
    r'^\*{0,2}\bКоманда\b(?:\s+в\s+[^\s:]+)?\s*:?\*{0,2}\s*(.+)$',
    re.IGNORECASE | re.MULTILINE
)
VIDEO_URL_RE = re.compile(r'https?://\S+')


def _strip_blockquote(text: str) -> str:
    """Часть гайдов оформлена построчной цитатой ('> '), часть — без неё; снимаем
    префикс там, где он есть, чтобы остальные регексы не знали про разницу."""
    lines = text.split("\n")
    out = []
    for line in lines:
        m = re.match(r'^>\s?(.*)$', line)
        out.append(m.group(1) if m else line)
    return "\n".join(out)


def _clean(value):
    if value is None:
        return None
    value = value.replace("*", "").strip()
    return value or None


def _parse_counter_message(content: str, thread_name: str) -> dict:
    """Разбирает одно сообщение гайд-треда. enemy_variant — из первого заголовка
    БЕЗ ведущего '-' (может отличаться от имени треда — у одного пака бывает
    несколько вариантов контры под разными заголовками); counter_leader — из
    первого заголовка, начинающегося с '-'. Если структуры не нашлось совсем —
    parsed_ok=False, enemy_variant заменяется на имя треда (полагаться на
    variant дальше нельзя, только на raw_text)."""
    text = _strip_blockquote(content or "")

    enemy_variant = None
    counter_leader = None
    for m in HEADER_LINE_RE.finditer(text):
        line_text = m.group(1).strip()
        if line_text.startswith("-"):
            if counter_leader is None:
                counter_leader = _clean(line_text.lstrip("-").strip())
        elif enemy_variant is None:
            enemy_variant = _clean(line_text)

    comp_match = COMPOSITION_RE.search(text)
    composition = _clean(comp_match.group(1)) if comp_match else None

    dk_match = DATACRON_RE.search(text)
    datacron_note = _clean(dk_match.group(1)) if dk_match else None

    bp_match = BATTLE_PLAN_RE.search(text)
    battle_plan = _clean(bp_match.group(1)) if bp_match else None

    tc_match = TEAM_CODE_RE.search(text)
    team_code = _clean(tc_match.group(1)) if tc_match else None

    video_match = VIDEO_URL_RE.search(text)
    video_url = video_match.group(0).rstrip(')>*]"\'.,') if video_match else None

    parsed_ok = any([enemy_variant, counter_leader, composition, datacron_note, battle_plan])
    if enemy_variant is None:
        enemy_variant = thread_name

    return {
        "enemy_variant": enemy_variant,
        "counter_leader": counter_leader,
        "composition": composition,
        "datacron_note": datacron_note,
        "battle_plan": battle_plan,
        "team_code": team_code,
        "video_url": video_url,
        "parsed_ok": parsed_ok,
    }


def _tw_counter_label(counter_leader, composition, team_code, parsed_ok, raw_text) -> str:
    if not parsed_ok:
        base = (raw_text or "").replace("\n", " ").strip()
        return f"⚠️ {base}" if base else "⚠️ (пусто)"
    main = counter_leader or composition or "?"
    return f"{main} [{team_code}]" if team_code else main


def _format_counter_block(pack_name: str, location: str, row, warning: str = None) -> str:
    """Собирает один блок готового к вставке ордера — точно в том же виде,
    в каком офицеры сами пишут его в #ac-вг-оповещения: '# Локация' / '## Пак' /
    построчная цитата с лидером/составом/датакроном/ходом боя/командой."""
    lines = []
    if location:
        lines.append(f"# {location}")
    lines.append(f"## {pack_name}")

    if warning:
        lines.append(f"> {warning}")
        return "\n".join(lines)

    _, _enemy_variant, counter_leader, composition, datacron_note, battle_plan, team_code, video_url, parsed_ok, raw_text = row

    if not parsed_ok:
        # Гайд не размечен структурно — вставляем как есть, той же цитатой.
        for line in (raw_text or "").split("\n"):
            lines.append(f"> {line}" if line else ">")
        return "\n".join(lines)

    if counter_leader:
        lines.append(f"> ### - {counter_leader}")
    if composition:
        lines.append(f"> Состав: {composition}")
    if datacron_note:
        lines.append(f"> Датакрон: {datacron_note}")
    if battle_plan:
        lines.append("> Ход боя:")
        for line in battle_plan.split("\n"):
            lines.append(f"> {line}" if line else ">")
    if team_code:
        lines.append(f"> Команда в хб: {team_code}")
    if video_url:
        lines.append(f"> Видео: {video_url}")
    return "\n".join(lines)


def _chunk_message(text: str, limit: int = 2000):
    """На случай, если суммарный текст ордера на несколько локаций перерастёт
    лимит Discord в 2000 символов — режем по строкам, а не обрезаем молча
    (тот же приём, что и в guild_events.py::_chunk_message)."""
    if len(text) <= limit:
        return [text]
    chunks = []
    current = ""
    for line in text.split("\n"):
        candidate = f"{current}\n{line}" if current else line
        if len(candidate) > limit:
            if current:
                chunks.append(current)
            current = line
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks


# ------------------ Автокомплиты ------------------
async def autocomplete_tw_pack(inter: disnake.ApplicationCommandInteraction, string: str):
    guild_id = guild_resolver.resolve_guild_id(inter.author)
    if guild_id is None:
        return []
    return database.get_tw_counter_packs(guild_id, string.strip())[:25]


def _make_tw_counter_autocomplete(pack_param_name: str):
    """5 отдельных замыканий (по одному на слот пакN), т.к. каждое должно знать
    имя СВОЕГО параметра, чтобы прочитать выбранный пак через inter.filled_options."""
    async def _autocomplete(inter: disnake.ApplicationCommandInteraction, string: str):
        guild_id = guild_resolver.resolve_guild_id(inter.author)
        if guild_id is None:
            return []
        pack = inter.filled_options.get(pack_param_name)
        if not pack:
            return ["⚠️ Сначала выберите пак"]
        rows = database.get_tw_counters_for_pack(guild_id, pack)
        if not rows:
            return ["❌ Нет сохранённых контр на этот пак"]
        search = string.lower().strip()
        options = []
        for row in rows:
            counter_id, counter_leader, composition, _dk, _bp, team_code, _video, parsed_ok, raw_text = row
            label = _tw_counter_label(counter_leader, composition, team_code, parsed_ok, raw_text)
            if not search or search in label.lower():
                options.append(disnake.OptionChoice(name=label[:100], value=str(counter_id)))
        return options[:25]
    return _autocomplete


autocomplete_tw_counter1 = _make_tw_counter_autocomplete("пак1")
autocomplete_tw_counter2 = _make_tw_counter_autocomplete("пак2")
autocomplete_tw_counter3 = _make_tw_counter_autocomplete("пак3")
autocomplete_tw_counter4 = _make_tw_counter_autocomplete("пак4")
autocomplete_tw_counter5 = _make_tw_counter_autocomplete("пак5")


class TWCounters(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.sync_loop.start()

    def cog_unload(self):
        self.sync_loop.cancel()

    # ------------------ Синхронизация форум-канала гайдов ------------------
    async def _sync_guild(self, guild_cfg: dict) -> dict:
        forum_channel_id = guild_cfg.get("tw_guide_forum_channel_id")
        if not forum_channel_id:
            return {"skipped": True}

        channel = self.bot.get_channel(int(forum_channel_id))
        if channel is None:
            channel = await self.bot.fetch_channel(int(forum_channel_id))
        if not isinstance(channel, disnake.ForumChannel):
            return {"error": "канал не является форум-каналом"}

        gid = guild_cfg["id"]
        threads = list(channel.threads)
        async for thread in channel.archived_threads(limit=None):
            threads.append(thread)

        stats = {"threads": 0, "messages": 0, "parsed_ok": 0, "parsed_fail": 0}
        for thread in threads:
            stats["threads"] += 1
            tag_names = [t.name for t in getattr(thread, "applied_tags", [])]
            database.upsert_tw_counter_thread(
                gid, str(thread.id), thread.name, tag=", ".join(tag_names) if tag_names else None
            )
            async for message in thread.history(limit=None):
                if message.author.bot:
                    continue
                # У форум-треда id стартового сообщения всегда равен id самого
                # треда — это просто описание темы (не контра), пропускаем.
                if str(message.id) == str(thread.id):
                    continue
                parsed = _parse_counter_message(message.content, thread.name)
                # Отдельное сообщение, дословно повторяющее название треда (не
                # стартовый пост, но текст = название) — мусорный дубль темы, не
                # контра. Иначе он попадал бы в базу как отдельный "пак" с именем
                # треда, дублируя реальные варианты из ##-заголовков (см. живой
                # баг: тред "Хондо" давал лже-пак "Король пиратов Хондо Онака" без
                # контр вместо настоящих "Хондо"/"Хондо с вейнокроном"). Не просто
                # пропускаем — ещё и чистим, если такая запись уже была сохранена
                # прошлым синком.
                if not parsed["parsed_ok"] and (message.content or "").strip() == thread.name.strip():
                    database.delete_tw_counter(gid, str(message.id))
                    continue
                database.upsert_tw_counter(
                    gid, str(thread.id), str(message.id),
                    enemy_variant=parsed["enemy_variant"],
                    counter_leader=parsed["counter_leader"],
                    composition=parsed["composition"],
                    datacron_note=parsed["datacron_note"],
                    battle_plan=parsed["battle_plan"],
                    team_code=parsed["team_code"],
                    video_url=parsed["video_url"],
                    parsed_ok=parsed["parsed_ok"],
                    raw_text=message.content,
                    author=str(message.author),
                    posted_at=message.created_at.isoformat(),
                )
                stats["messages"] += 1
                stats["parsed_ok" if parsed["parsed_ok"] else "parsed_fail"] += 1
        return stats

    @tasks.loop(hours=6)
    async def sync_loop(self):
        for guild_cfg in database.get_all_guild_configs():
            if not guild_cfg.get("tw_guide_forum_channel_id"):
                continue
            try:
                stats = await self._sync_guild(guild_cfg)
                print(f"✅ [TWCounters] [{guild_cfg['name']}] синк гайдов ВГ: {stats}")
            except Exception as e:
                print(f"❌ [TWCounters] [{guild_cfg['name']}] ошибка синка гайдов ВГ: {e}")

    @sync_loop.before_loop
    async def _before_sync_loop(self):
        await self.bot.wait_until_ready()

    @commands.slash_command(
        name="вг_гайды_синхронизация",
        description="Форс-синхронизация базы контр по ВГ из форум-канала гайдов (офицеры)"
    )
    @commands.check(lambda inter: guild_resolver.is_officer_for_resolved_guild(inter.author))
    async def tw_sync_command(self, inter: disnake.ApplicationCommandInteraction):
        await inter.response.defer(ephemeral=True)
        guild_id = await guild_resolver.require_guild_id(inter)
        if guild_id is None:
            return
        guild_cfg = database.get_guild_config(guild_id)
        if not guild_cfg or not guild_cfg.get("tw_guide_forum_channel_id"):
            await inter.edit_original_response(
                "❌ Не задан форум-канал гайдов ВГ — настройте его через `/настройки вг_гайды_канал`."
            )
            return
        try:
            stats = await self._sync_guild(guild_cfg)
        except Exception as e:
            await inter.edit_original_response(f"Ошибка синхронизации: {e}")
            return
        await inter.edit_original_response(
            f"✅ Синхронизация завершена.\n"
            f"Тредов: {stats['threads']}\n"
            f"Сообщений: {stats['messages']}\n"
            f"Распознано структурно: {stats['parsed_ok']}\n"
            f"Не распознано (сохранено как есть): {stats['parsed_fail']}"
        )

    # ------------------ Слэш-команда ------------------
    @commands.slash_command(name="вг_ордер", description="Собрать быстрый ордер по ВГ: пак + наша контра для каждой локации (до 5)")
    @commands.check(lambda inter: guild_resolver.is_officer_for_resolved_guild(inter.author))
    async def tw_order(
        self,
        inter: disnake.ApplicationCommandInteraction,
        пак1: str = commands.Param(description="Вражеский пак №1", autocomplete=autocomplete_tw_pack),
        контра1: str = commands.Param(default=None, description="Наша контра на пак №1", autocomplete=autocomplete_tw_counter1),
        локация1: str = commands.Param(default=None, description="Где бить пак №1 (например 'верх', '2 низ')"),
        пак2: str = commands.Param(default=None, description="Вражеский пак №2", autocomplete=autocomplete_tw_pack),
        контра2: str = commands.Param(default=None, description="Наша контра на пак №2", autocomplete=autocomplete_tw_counter2),
        локация2: str = commands.Param(default=None, description="Где бить пак №2"),
        пак3: str = commands.Param(default=None, description="Вражеский пак №3", autocomplete=autocomplete_tw_pack),
        контра3: str = commands.Param(default=None, description="Наша контра на пак №3", autocomplete=autocomplete_tw_counter3),
        локация3: str = commands.Param(default=None, description="Где бить пак №3"),
        пак4: str = commands.Param(default=None, description="Вражеский пак №4", autocomplete=autocomplete_tw_pack),
        контра4: str = commands.Param(default=None, description="Наша контра на пак №4", autocomplete=autocomplete_tw_counter4),
        локация4: str = commands.Param(default=None, description="Где бить пак №4"),
        пак5: str = commands.Param(default=None, description="Вражеский пак №5", autocomplete=autocomplete_tw_pack),
        контра5: str = commands.Param(default=None, description="Наша контра на пак №5", autocomplete=autocomplete_tw_counter5),
        локация5: str = commands.Param(default=None, description="Где бить пак №5"),
    ):
        guild_id = await guild_resolver.require_guild_id(inter)
        if guild_id is None:
            return

        slots = [
            (пак1, контра1, локация1),
            (пак2, контра2, локация2),
            (пак3, контра3, локация3),
            (пак4, контра4, локация4),
            (пак5, контра5, локация5),
        ]

        blocks = []
        for pack, counter_id, location in slots:
            if not pack:
                continue
            row, warning = None, None
            if not counter_id:
                warning = "⚠️ контра не выбрана — впишите вручную"
            else:
                try:
                    row = database.get_tw_counter_by_id(guild_id, int(counter_id))
                except ValueError:
                    # Значение не число — это не ID из подсказки автокомплита, а
                    # текст, который ввели/вставили руками мимо выбора варианта.
                    warning = "⚠️ введено вручную — выберите вариант из подсказки, а не печатайте его"
                else:
                    if row is None:
                        warning = "⚠️ контра не найдена (устаревший ID — выберите заново из подсказки)"
            blocks.append(_format_counter_block(pack, location, row, warning))

        if not blocks:
            await inter.response.send_message("Не указано ни одного пака.", ephemeral=True)
            return

        full_text = "\n\n".join(blocks)
        guild_cfg = database.get_guild_config(guild_id)
        role_id = guild_cfg.get("tb_order_role_id") if guild_cfg else None
        if role_id:
            full_text += f"\n\n<@&{role_id}>"

        chunks = _chunk_message(full_text)
        await inter.response.send_message(chunks[0], ephemeral=True)
        for chunk in chunks[1:]:
            await inter.followup.send(chunk, ephemeral=True)


def setup(bot: commands.Bot):
    bot.add_cog(TWCounters(bot))
