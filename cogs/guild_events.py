import asyncio
import disnake
from disnake.ext import commands, tasks
from datetime import datetime
from zoneinfo import ZoneInfo
from collections import defaultdict
import hashlib
import json
import re
import database
from swgoh_comlink import SwgohComlink
from cogs.violations import autocomplete_players
import tempfile

MSK = ZoneInfo("Europe/Moscow")

# ------------------ Декодер сырых ключей ТБ (comlink mapStatId) ------------------
# Ключи вида "power_zone_tb3_mixed_phase03_conflict01_bonus" разбираются на
# действие/фазу/планету/бонус/номер ОЗ-миссии. Ключи вида "power_round_3" — это
# официальные готовые итоги comlink по фазе (совпадают с внешними трекерами вроде
# HotUtils), а не пересчитанная нами сумма по зонам — используются как основной
# источник итогов по фазе, зональная разбивка идёт только как детализация.
TB_ACTION_LABELS = {
    "power": "GP развёрнуто",
    "summary": "Очки территории",
    "unit_donated": "Юниты во взвод",
    "strike_encounter": "БЗ: волн пройдено",
    "strike_attempt": "БЗ: попытки",
    "covert_complete": "ОЗ: миссий выполнено",
    "covert_round_attempted": "ОЗ: волны",
    "covert_attempt": "ОЗ: попытки",
    "disobey": "Rogue-действия",
}

TB_CONFLICT_LABELS = {"01": "Dark", "02": "Mixed", "03": "Light"}

TB_VALUE_COL = 30  # позиция, с которой начинается число — единая для всех уровней вложенности

# comlink для этой конкретной ТБ отдаёт заведомо неверные covert_round_attempted
# в этих зонах (проверено вручную: фаза 1 Mixed показывает 1 при фактических 2 волнах;
# фаза 3 Light-бонус показывает 3 при максимум возможных 2; фаза 3 Mixed показывает 6
# вместо фактических 3) — общее для всей гильдии искажение данных за этот ивент,
# не ошибка нашего парсинга. Прячем из основного вывода, показываем сырым ключом внизу.
TB_HIDDEN_ZONE_ACTIONS = {
    ("1", "03"): {"covert_round_attempted"},
    ("3", "01_bonus"): {"covert_round_attempted"},
    ("3", "03"): {"covert_round_attempted"},
}

TB_KNOWN_GLOBAL_ACTIONS = [
    "power", "summary", "unit_donated", "strike_encounter", "strike_attempt",
    "covert_attempt", "covert_complete", "covert_round_attempted", "disobey",
]

TB_ZONE_RE = re.compile(
    r"^(?P<action>[a-z_]+?)_(?P<kind>zone|mission)_tb3_mixed_phase(?P<phase>\d+)"
    r"_conflict(?P<conflict>\d+)(?P<bonus>_bonus)?(?:_covert(?P<covert>\d+))?$"
)
TB_ROUND_RE = re.compile(r"^(?P<action>[a-z_]+)_round_(?P<round>\d+)$")

# ------------------ Реальные названия планет (из офицерских анонсов) ------------------
# Comlink не отдаёт название планеты — только фазу и код ветки (01/02/03 = Dark/Mixed/
# Light, см. TB_ZONE_RE/TB_CONFLICT_LABELS). Офицеры перед каждой фазой вручную постят в
# #ac-тб-оповещения сообщение вида "Восход Империи — N этап" со списком реальных планет,
# каждая — под цветным маркером. Цвет маркера — это оформление конкретного офицера, а не
# код ветки (он не совпадает с TB_CONFLICT_LABELS и может отличаться от поста к посту),
# поэтому ветку определяем НЕ по цвету, а по самому названию планеты через справочник
# TB_PLANET_CONFLICT. У каждой планеты ветка фиксирована и не меняется от фазы к фазе —
# это подтверждено подтверждёнными данными завершённой ТБ (tb_planet_names, source=
# 'manual', сверено с сырыми ключами comlink): Датомир (01) — на 3 и 4 этапе, Кессель
# (02) — на 4 и 5, Мандалор (bonus) — на 4 и 5, Кольцо Кафрены (03) — на 5 и 6. Также
# сверено с картой swgohrote.com: Датомир лежит на "красной" ветке карты, Кессель — на
# "жёлто-оранжевой", что подтверждает постоянство ветки для каждой планеты.
# Планеты, которых нет в справочнике (новые в ротации), авто-разбор пропускает — их
# нужно доопределить вручную командой /тб_отчет план и одновременно дополнить справочник
# в коде, чтобы будущие анонсы с этой планетой разбирались автоматически.
TB_PLAN_HEADER_RE = re.compile(r"Восход\s+Импери\w*\s*[—\-]\s*(\d+)\s*этап", re.IGNORECASE)
TB_PLAN_CIRCLE_CHARS = "🔴🟠🟡🟢🔵🟣⚫⚪🟤"
TB_PLANET_CONFLICT = {
    "мустафар": "01",
    "джеонозис": "01",
    "датомир": "01",
    "медстанция": "01",
    "малахор": "01",
    "кореллия": "02",
    "фелуция": "02",
    "татуин": "02",
    "кессель": "02",
    "вандор": "02",
    "корусант": "03",
    "бракка": "03",
    "кашик": "03",
    "лотал": "03",
    "кольцо кафрены": "03",
    "кольцо каферны": "03",
    "зеффо": "bonus",
    "мандалор": "bonus",
}


def _fmt_num(value) -> str:
    return f"{int(value):,}".replace(",", " ")


def _fmt_line(label, value, indent=0) -> str:
    prefix = " " * indent + label
    number = _fmt_num(value)
    if len(prefix) >= TB_VALUE_COL:
        return f"{prefix} {number}"
    return f"{prefix}{' ' * (TB_VALUE_COL - len(prefix))}{number:>12}"


class GuildEvents(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.comlink = SwgohComlink(url="http://localhost:3000")
        self.guild_id = "-kJhCaGGQqGOjgbWpJFEIg"
        self.officer_channel_id = bot.OFFICER_CHANNEL_ID
        self.allowed_role_id = bot.ALLOWED_OFFICER_ROLE_ID
        self.tb_plan_channel_id = bot.TB_PLAN_CHANNEL_ID
        self.tb_order_source_channel_id = bot.TB_ORDER_SOURCE_CHANNEL_ID
        self.tb_order_role_id = bot.TB_ORDER_ROLE_ID
        # Отпечаток последней ОТПРАВЛЕННОЙ ТБ, переживает рестарт бота (см. monitor_loop).
        self.last_reported_tb_fingerprint = database.get_bot_state("last_reported_tb_fingerprint")
        self.last_tw_status = None
        self._tb_order_sent_key = None
        self.monitor_loop.start()
        self.tb_order_loop.start()

    def cog_unload(self):
        self.monitor_loop.cancel()
        self.tb_order_loop.cancel()

    # ------------------ Авто-разбор плана планет из анонсов офицеров ------------------
    @commands.Cog.listener()
    async def on_message(self, message: disnake.Message):
        if message.author.bot or message.channel.id != self.tb_plan_channel_id:
            return
        parsed = self._parse_tb_plan_message(message.content)
        if not parsed:
            return
        phase, mapping = parsed
        if phase == "1":
            database.clear_tb_planet_names()
        for conflict_key, planet_name in mapping.items():
            database.set_tb_planet_name(phase, conflict_key, planet_name, source="auto")

    def _parse_tb_plan_message(self, content: str):
        """Разбирает анонс "Восход Империи — N этап" на {conflict_key: planet_name}.
        Ветка определяется по НАЗВАНИЮ планеты через справочник TB_PLANET_CONFLICT,
        а не по цвету маркера строки и не по порядку строк — цвет маркера офицер
        выбирает произвольно, а порядок перечисления от анонса к анонсу может
        меняться. Планеты, которых нет в справочнике, пропускаются — их нужно
        доопределить вручную через /тб_отчет план."""
        header = TB_PLAN_HEADER_RE.search(content or "")
        if not header:
            return None
        phase = str(int(header.group(1)))
        mapping = {}
        for line in content.splitlines():
            stripped = line.strip()
            if not stripped or stripped[0] not in TB_PLAN_CIRCLE_CHARS:
                continue
            name = stripped[1:].strip(" *#")
            if not name:
                continue
            conflict_key = TB_PLANET_CONFLICT.get(name.strip().lower())
            if not conflict_key:
                continue
            mapping[conflict_key] = name
        return (phase, mapping) if mapping else None

    # ------------------ Ежедневная публикация ордера на актуальный этап ------------------
    # Офицеры выкладывают план на все 6 этапов разом, одной веткой (TB_ORDER_SOURCE_
    # CHANNEL_ID), а не по дням — поэтому бот сам режет её на блоки по заголовкам
    # "Восход Империи — N этап" и публикует нужный блок каждый день. Какой этап
    # актуален "сегодня" определяем не по датам ТБ (бот их не знает), а по дню
    # недели внутри "тегаемой" недели: этап 1 — в тот день, что идёт первым в
    # расписании "ордер" (bot.PING_SCHEDULE), этап 2 — во второй и т.д. Так публикация
    # автоматически совпадает с днями, когда RotationPing и так напоминает про
    # взводы/ордер — отдельного расписания не заводим.
    def _is_ping_week(self, today_date) -> bool:
        start_date = datetime.strptime(self.bot.PING_START_DATE, "%Y-%m-%d").date()
        delta = (today_date - start_date).days
        return (delta // 7) % 2 == 0

    def _tb_order_phase_for_weekday(self, weekday: int):
        order_entry = next((e for e in self.bot.PING_SCHEDULE if e.get("text") == "ордер"), None)
        if not order_entry:
            return None
        days = sorted(order_entry.get("days", []))
        if weekday not in days:
            return None
        return str(days.index(weekday) + 1)

    async def _fetch_tb_order_source_text(self) -> str:
        channel = self.bot.get_channel(self.tb_order_source_channel_id)
        if channel is None:
            channel = await self.bot.fetch_channel(self.tb_order_source_channel_id)
        parts = []
        async for message in channel.history(limit=None, oldest_first=True):
            if message.content:
                parts.append(message.content)
        return "\n".join(parts)

    def _extract_tb_order_block(self, full_text: str, phase: str):
        """Вырезает из текста ветки-плана блок конкретного этапа целиком (со всеми
        заметками, ссылками и гайдами) — от заголовка "Восход Империи — N этап" до
        следующего такого заголовка или до конца текста. Блок всегда начинается
        ровно с текста заголовка, поэтому его можно просто превратить в "## Восход...".
        Если в ветке несколько планов на один и тот же этап (план от прошлой ТБ не
        удалили, а просто добавили новый ниже) — берём ПОСЛЕДНЕЕ совпадение, чтобы
        не нужно было вручную чистить историю канала между ТБ."""
        matches = list(TB_PLAN_HEADER_RE.finditer(full_text))
        result = None
        for i, m in enumerate(matches):
            if str(int(m.group(1))) != phase:
                continue
            start = m.start()
            end = matches[i + 1].start() if i + 1 < len(matches) else len(full_text)
            result = full_text[start:end].strip()
            # TB_PLAN_HEADER_RE матчит только сам текст "Восход Империи — N этап",
            # без markdown-префикса "#" — если следующий этап в исходной ветке
            # тоже оформлен как "# Восход...", этот "#" повисает в хвосте текущего
            # блока прямо перед пингом роли. Одинокий "#" перед пингом заставляет
            # Discord отрисовать сам тег огромным шрифтом — вырезаем его.
            result = re.sub(r"#+\s*$", "", result).strip()
        return result

    @staticmethod
    def _chunk_message(text: str, limit: int = 2000):
        """На случай, если блок этапа однажды перерастёт лимит Discord в 2000
        символов — режем по строкам, а не обрезаем текст молча."""
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

    @tasks.loop(seconds=30)
    async def tb_order_loop(self):
        now_msk = datetime.now(MSK)
        if now_msk.hour != 20 or now_msk.minute != 0:
            return
        if not self._is_ping_week(now_msk.date()):
            return
        phase = self._tb_order_phase_for_weekday(now_msk.weekday())
        if not phase:
            return

        current_key = now_msk.strftime("%Y%m%d%H%M")
        if current_key == self._tb_order_sent_key:
            return

        channel = self.bot.get_channel(self.tb_plan_channel_id)
        if channel is None:
            print(f"❌ [TBOrder] Канал {self.tb_plan_channel_id} не найден")
            return
        role = channel.guild.get_role(self.tb_order_role_id) if channel.guild else None
        if role is None:
            print(f"❌ [TBOrder] Роль {self.tb_order_role_id} не найдена")
            return

        try:
            full_text = await self._fetch_tb_order_source_text()
            block = self._extract_tb_order_block(full_text, phase)
            if not block:
                print(f"❌ [TBOrder] Не нашёл блок {phase} этапа в ветке-плане")
                return
            message_text = f"## {block}\n\n\n{role.mention}"
            for chunk in self._chunk_message(message_text):
                await channel.send(chunk)
            self._tb_order_sent_key = current_key
            print(f"✅ [TBOrder] Ордер на {phase} этап отправлен в {now_msk.strftime('%Y-%m-%d %H:%M')} МСК")
        except Exception as e:
            print(f"❌ [TBOrder] Ошибка отправки ордера: {e}")

    @tb_order_loop.before_loop
    async def _before_tb_order_loop(self):
        await self.bot.wait_until_ready()

    def _planet_label(self, phase, conflict_key, planet_map):
        """Метка планеты для одиночного отчёта: реальное название + ветка в скобках,
        либо просто ветка (Light/Dark/Mixed), если план для этой фазы ещё не известен."""
        base_conflict = conflict_key.split("_")[0]
        is_bonus = "_bonus" in conflict_key
        fallback = TB_CONFLICT_LABELS.get(base_conflict, f"Conflict {base_conflict}")
        name = planet_map.get((phase, "bonus" if is_bonus else base_conflict))
        if name:
            return f"{name} — {fallback}" + (" (бонус)" if is_bonus else "")
        return fallback + (" (бонус)" if is_bonus else "")

    def _planet_label_compare(self, phase, conflict_key, planet_maps):
        """То же самое для сравнения по нескольким ТБ: планета могла быть разной
        от ивента к ивенту на одной и той же ветке, поэтому показываем через "|"."""
        base_conflict = conflict_key.split("_")[0]
        is_bonus = "_bonus" in conflict_key
        fallback = TB_CONFLICT_LABELS.get(base_conflict, f"Conflict {base_conflict}")
        lookup_key = "bonus" if is_bonus else base_conflict
        names = [pm.get((phase, lookup_key)) if pm else None for pm in planet_maps]
        if any(names):
            shown = " | ".join(n if n else "—" for n in names)
            return f"{shown} ({fallback})" + (", бонус" if is_bonus else "")
        return fallback + (" (бонус)" if is_bonus else "")

    # ------------------ Мониторинг событий ------------------
    @tasks.loop(minutes=5)
    async def monitor_loop(self):
        await self.bot.wait_until_ready()
        try:
            guild = await asyncio.to_thread(
                self.comlink.get_guild, self.guild_id, include_recent_guild_activity_info=True
            )
        except Exception as e:
            print(f"Ошибка получения данных гильдии: {e}")
            return

        # Триггерим не по полю "status" (оно ненадёжно: рестарт бота обнуляет память,
        # а comlink может проскочить статус "completed" между опросами) — а по факту
        # изменения содержимого recentTerritoryBattleResult. Отпечаток хранится в БД,
        # поэтому рестарт бота не приводит ни к повторной, ни к пропущенной отправке.
        try:
            result = guild.get("recentTerritoryBattleResult", [])
            if result:
                fingerprint = hashlib.sha1(
                    json.dumps(result, sort_keys=True, default=str).encode()
                ).hexdigest()
                if fingerprint != self.last_reported_tb_fingerprint:
                    self.last_reported_tb_fingerprint = fingerprint
                    database.set_bot_state("last_reported_tb_fingerprint", fingerprint)
                    await self.generate_tb_report(guild, fingerprint)
        except Exception as e:
            print(f"Ошибка обработки отчёта по ТБ: {e}")

        tw_status = guild.get("territoryWarStatus", [])
        current_tw = tw_status[0] if tw_status else None
        if current_tw and self.last_tw_status and current_tw.get("status") != self.last_tw_status.get("status"):
            if current_tw.get("status") == "completed":
                await self.generate_tw_report(guild)
        self.last_tw_status = current_tw

    async def generate_tb_report(self, guild, fingerprint=None):
        result = guild.get("recentTerritoryBattleResult", [])
        if not result:
            await self.notify_officers("ТБ завершена, но отчёт пуст.")
            return

        members = guild.get("member", [])
        player_names = {m["playerId"]: m["playerName"] for m in members if "playerId" in m and "playerName" in m}
        stats = self._collect_guild_stats(result, player_names)
        if not stats:
            await self.notify_officers("Нет данных по очкам.")
            return

        if fingerprint:
            try:
                self._store_tb_history(fingerprint, result, members, player_names, stats)
            except Exception as e:
                print(f"Ошибка сохранения истории ТБ: {e}")

        report = self._format_stats_table("📊 **Итоги Территориальной Битвы (автоотчёт)**", stats)
        channel = self.bot.get_channel(self.officer_channel_id)
        if channel:
            await self.send_as_file(channel, report, "tb_report.txt")

    async def generate_tw_report(self, guild):
        pass

    async def notify_officers(self, message):
        channel = self.bot.get_channel(self.officer_channel_id)
        if channel:
            await channel.send(f"📢 {message}")

    # ------------------ Общие вспомогательные функции ------------------
    def _collect_guild_stats(self, tb_result, player_names):
        """Собирает итоги за весь ивент по гильдии: только "голые" глобальные ключи
        (map_id == имя действия, без _round_N и без _zone_...), чтобы не суммировать
        одни и те же очки повторно вместе с попытками/волнами из зональных ключей."""
        stats = {}
        for zone in tb_result[0].get("finalStat", []):
            map_id = zone.get("mapStatId")
            if map_id not in TB_KNOWN_GLOBAL_ACTIONS:
                continue
            for ps in zone.get("playerStat", []):
                member_id = ps.get("memberId")
                if not member_id:
                    continue
                if member_id not in stats:
                    stats[member_id] = {
                        "name": player_names.get(member_id, member_id[:8] + "…"),
                        **{action: 0 for action in TB_KNOWN_GLOBAL_ACTIONS},
                    }
                stats[member_id][map_id] += int(ps.get("score", 0))
        return stats

    def _format_stats_table(self, title, stats):
        columns = [
            ("summary", "Очки территории", 16),
            ("unit_donated", "Юниты во взвод", 15),
            ("covert_attempt", "ОЗ попытки", 11),
            ("strike_encounter", "БЗ волн пройдено", 17),
            ("strike_attempt", "БЗ попыток", 11),
        ]
        name_width = 24

        lines = [title, ""]
        header = f"{'Игрок':<{name_width}}" + "".join(f"{label:>{w}}" for _, label, w in columns)
        lines.append(header)
        lines.append("-" * len(header))

        sorted_stats = sorted(stats.items(), key=lambda x: x[1]["summary"], reverse=True)
        for member_id, s in sorted_stats:
            name = s["name"][:name_width - 1]
            row = f"{name:<{name_width}}"
            row += "".join(f"{_fmt_num(s[action]):>{w}}" for action, _, w in columns)
            lines.append(row)
        return "\n".join(lines)

    def _format_tb_compare_table(self, events, summary_rows):
        """events: [(event_id, completed_at), ...] от старых к новым (макс. 3).
        summary_rows: строки из get_tb_player_summary_for_events."""
        metrics = [
            ("summary", "Очки", 11),
            ("unit_donated", "Юн", 4),
            ("covert_attempt", "ОЗ", 4),
            ("strike_encounter", "Волн", 5),
            ("strike_attempt", "Поп", 4),
        ]
        block_width = sum(w for _, _, w in metrics)
        name_width = 22

        by_member = {}
        for event_id, member_id, name, summary, unit_donated, covert_attempt, strike_encounter, strike_attempt in summary_rows:
            entry = by_member.setdefault(member_id, {"name": name})
            entry["name"] = name  # берём самое свежее имя из последней встреченной записи
            entry[event_id] = {
                "summary": summary, "unit_donated": unit_donated, "covert_attempt": covert_attempt,
                "strike_encounter": strike_encounter, "strike_attempt": strike_attempt,
            }

        n = len(events)
        header1 = f"{'Игрок':<{name_width}}"
        header2 = " " * name_width
        for i, (event_id, completed_at) in enumerate(events):
            label = f"ТБ-{n - i} ({completed_at[:10]})"
            header1 += " | " + f"{label:^{block_width}}"
            header2 += " | " + "".join(f"{m_label:>{w}}" for _, m_label, w in metrics)

        lines = [header1, header2, "-" * len(header2)]

        latest_event_id = events[-1][0] if events else None

        def sort_key(item):
            _, entry = item
            return entry.get(latest_event_id, {}).get("summary", -1)

        for member_id, entry in sorted(by_member.items(), key=sort_key, reverse=True):
            row = f"{entry['name'][:name_width - 1]:<{name_width}}"
            for event_id, _ in events:
                data = entry.get(event_id)
                row += " | "
                if data:
                    row += "".join(f"{data[action]:>{w}}" for action, _, w in metrics)
                else:
                    row += "".join(f"{'-':>{w}}" for _, _, w in metrics)
            lines.append(row)
        return "\n".join(lines)

    def _store_tb_history(self, fingerprint, result, members, player_names, stats):
        """Сохраняет итоги завершённой ТБ (сводку по гильдии + полную расшифровку
        по каждому игроку) в БД, храним только последние TB_HISTORY_KEEP событий."""
        event_id = database.record_tb_event(fingerprint)
        database.snapshot_tb_planet_names(event_id)

        summary_rows = [
            (event_id, member_id, s["name"], s["summary"], s["unit_donated"],
             s["covert_attempt"], s["strike_encounter"], s["strike_attempt"])
            for member_id, s in stats.items()
        ]
        database.save_tb_player_summary(summary_rows)

        detail_rows = []
        for m in members:
            member_id = m.get("playerId")
            if not member_id:
                continue
            name = player_names.get(member_id, member_id[:8] + "…")
            zone_data, global_totals, round_totals, raw_keys, _ = self._decode_tb_stats(result, member_id)
            if not zone_data and not round_totals and not global_totals:
                continue
            detail_rows.append((
                event_id, member_id, name,
                json.dumps(zone_data), json.dumps(global_totals),
                json.dumps(round_totals), json.dumps(raw_keys),
            ))
        database.save_tb_player_detail(detail_rows)

        database.prune_tb_events()

    async def send_as_file(self, channel, content, filename):
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", delete=False, suffix=".txt") as f:
            f.write(content)
            f.flush()
            await channel.send(file=disnake.File(f.name, filename=filename))

    # ------------------ Детальная статистика игрока (расшифровка по фазам/планетам) ------------------
    def _decode_tb_stats(self, tb_result, member_id):
        """
        Разбирает все mapStatId игрока на три группы:
        - zone_data[phase][conflict_key][action][entry] — разбивка по планетам/бонус-зонам/ОЗ-миссиям
        - global_totals[action] — итог за весь ивент
        - round_totals[round][action] — официальный итог comlink по фазе (round == фаза)
        """
        matched = {}
        for zone in tb_result[0].get("finalStat", []):
            map_id = zone.get("mapStatId")
            for ps in zone.get("playerStat", []):
                if ps.get("memberId") == member_id:
                    matched[map_id] = int(ps.get("score", 0))

        zone_data = defaultdict(lambda: defaultdict(lambda: defaultdict(dict)))
        raw_keys = defaultdict(lambda: defaultdict(lambda: defaultdict(dict)))
        global_totals = {}
        round_totals = defaultdict(dict)
        unrecognized = {}

        for map_id, score in matched.items():
            m = TB_ZONE_RE.match(map_id)
            if m:
                gd = m.groupdict()
                phase = str(int(gd["phase"]))  # "01" -> "1", чтобы совпадало с ключом round_totals
                conflict_key = gd["conflict"] + ("_bonus" if gd["bonus"] else "")
                entry_key = f"covert{gd['covert']}" if gd["covert"] else "_"
                zone_data[phase][conflict_key].setdefault(gd["action"], {})[entry_key] = score
                raw_keys[phase][conflict_key].setdefault(gd["action"], {})[entry_key] = map_id
                continue

            if map_id in TB_KNOWN_GLOBAL_ACTIONS:
                global_totals[map_id] = score
                continue

            m2 = TB_ROUND_RE.match(map_id)
            if m2 and m2.group("action") in TB_KNOWN_GLOBAL_ACTIONS:
                round_totals[str(int(m2.group("round")))][m2.group("action")] = score
                continue

            unrecognized[map_id] = score

        return zone_data, global_totals, round_totals, raw_keys, unrecognized

    def _format_tb_player_report(self, player_name, zone_data, global_totals, round_totals, raw_keys, planet_map):
        """Формирует читаемый отчёт: общее по игроку, затем по фазам (round_N — источник
        итогов) с вложенной детализацией по планетам/бонус-зонам/ОЗ-миссиям (zone_data).
        Показатели из TB_HIDDEN_ZONE_ACTIONS (заведомо недостоверные для этой ТБ) не
        попадают в основной текст, а выводятся сырым ключом одним блоком в конце."""
        rule = "─" * 46
        lines = [f"📊 Территориальная Битва — {player_name}", rule]
        hidden_entries = []

        lines.append("")
        lines.append("ИТОГО ЗА СОБЫТИЕ")
        for action in ("summary", "unit_donated", "strike_encounter", "strike_attempt", "covert_attempt", "disobey"):
            if action in global_totals:
                lines.append(_fmt_line(TB_ACTION_LABELS.get(action, action), global_totals[action], indent=2))

        all_phases = sorted(set(round_totals.keys()) | set(zone_data.keys()), key=int)

        def conflict_sort_key(ck):
            base = ck.split("_")[0]
            return (base, 1 if "_bonus" in ck else 0)

        for phase in all_phases:
            rt = round_totals.get(phase, {})
            lines.append("")
            lines.append(rule)
            lines.append(f"ФАЗА {phase}")
            for action in ("summary", "power", "strike_attempt", "strike_encounter", "covert_attempt", "unit_donated", "disobey"):
                if action in rt:
                    lines.append(_fmt_line(TB_ACTION_LABELS.get(action, action), rt[action], indent=2))

            conflicts = zone_data.get(phase, {})
            if conflicts:
                lines.append("")
                lines.append("  Планеты:")
                for conflict_key in sorted(conflicts.keys(), key=conflict_sort_key):
                    lines.append(f"    {self._planet_label(phase, conflict_key, planet_map)}")

                    actions = conflicts[conflict_key]
                    hidden_actions = TB_HIDDEN_ZONE_ACTIONS.get((phase, conflict_key), set())
                    for action in sorted(actions.keys()):
                        if action in hidden_actions:
                            entries = raw_keys.get(phase, {}).get(conflict_key, {}).get(action, {})
                            for entry_key, value in actions[action].items():
                                raw_key = entries.get(entry_key, f"{action}_phase{phase}_conflict{conflict_key}_{entry_key}")
                                hidden_entries.append((raw_key, value))
                            continue
                        # Несколько ОЗ-миссий на одной планете (covert01, covert02...) —
                        # это разные независимые миссии, суммируем в одну строку.
                        total = sum(actions[action].values())
                        action_label = TB_ACTION_LABELS.get(action, action)
                        lines.append(_fmt_line(action_label, total, indent=6))
            elif rt:
                lines.append("  (детализация по планетам недоступна в comlink для этой фазы)")

        lines.append("")
        lines.append(rule)
        lines.append(
            "Примечание: «ОЗ: попытки» не привязано к планетам — comlink не даёт "
            "зональной детализации для этого показателя, только итог по фазе/игроку."
        )

        if hidden_entries:
            lines.append("")
            lines.append(rule)
            lines.append("Скрытые параметры (недостоверные данные comlink за эту ТБ):")
            for raw_key, value in hidden_entries:
                lines.append(f"  {raw_key} = {_fmt_num(value)}")

        return "\n".join(lines)

    def _format_tb_player_compare_report(self, player_name, events, per_event, planet_maps):
        """events: [(event_id, completed_at), ...] от старых к новым (макс. 3).
        per_event: список той же длины, элемент — dict с zone_data/global_totals/
        round_totals/raw_keys для игрока в эту ТБ, либо None если данных нет."""
        n = len(events)
        labels = [f"ТБ-{n - i} ({events[i][1][:10]})" for i in range(n)]
        rule = "─" * 70
        lines = [f"📊 Сравнение по ТБ — {player_name}", rule, "", " | ".join(labels)]
        hidden_entries = []  # (метка_ТБ, raw_key, value)

        def cmp_line(label, values):
            parts = ["—" if v is None else _fmt_num(v) for v in values]
            return f"{label}: " + " | ".join(parts)

        lines.append("")
        lines.append("ИТОГО ЗА СОБЫТИЕ")
        for action in ("summary", "unit_donated", "strike_encounter", "strike_attempt", "covert_attempt", "disobey"):
            values = [e["global_totals"].get(action) if e else None for e in per_event]
            if all(v is None for v in values):
                continue
            lines.append("  " + cmp_line(TB_ACTION_LABELS.get(action, action), values))

        all_phases = sorted(
            {p for e in per_event if e for p in set(e["round_totals"].keys()) | set(e["zone_data"].keys())},
            key=int
        )

        def conflict_sort_key(ck):
            base = ck.split("_")[0]
            return (base, 1 if "_bonus" in ck else 0)

        for phase in all_phases:
            lines.append("")
            lines.append(rule)
            lines.append(f"ФАЗА {phase}")
            for action in ("summary", "power", "strike_attempt", "strike_encounter", "covert_attempt", "unit_donated", "disobey"):
                values = [(e["round_totals"].get(phase, {}).get(action) if e else None) for e in per_event]
                if all(v is None for v in values):
                    continue
                lines.append("  " + cmp_line(TB_ACTION_LABELS.get(action, action), values))

            conflict_keys = set()
            for e in per_event:
                if e:
                    conflict_keys |= set(e["zone_data"].get(phase, {}).keys())

            if conflict_keys:
                lines.append("")
                lines.append("  Планеты:")
                for conflict_key in sorted(conflict_keys, key=conflict_sort_key):
                    lines.append(f"    {self._planet_label_compare(phase, conflict_key, planet_maps)}")

                    action_keys = set()
                    for e in per_event:
                        if e:
                            action_keys |= set(e["zone_data"].get(phase, {}).get(conflict_key, {}).keys())

                    hidden_actions = TB_HIDDEN_ZONE_ACTIONS.get((phase, conflict_key), set())
                    for action in sorted(action_keys):
                        values = []
                        for e in per_event:
                            if not e:
                                values.append(None)
                                continue
                            entries = e["zone_data"].get(phase, {}).get(conflict_key, {}).get(action)
                            values.append(sum(entries.values()) if entries else None)

                        if action in hidden_actions:
                            for idx, e in enumerate(per_event):
                                if not e or values[idx] is None:
                                    continue
                                entries = e["raw_keys"].get(phase, {}).get(conflict_key, {}).get(action, {})
                                actual_entries = e["zone_data"].get(phase, {}).get(conflict_key, {}).get(action, {})
                                for entry_key, val in actual_entries.items():
                                    raw_key = entries.get(entry_key, f"{action}_phase{phase}_conflict{conflict_key}_{entry_key}")
                                    hidden_entries.append((labels[idx], raw_key, val))
                            continue

                        action_label = TB_ACTION_LABELS.get(action, action)
                        lines.append("      " + cmp_line(action_label, values))

        if hidden_entries:
            lines.append("")
            lines.append(rule)
            lines.append("Скрытые параметры по ТБ (недостоверные данные comlink):")
            for tb_label, raw_key, value in hidden_entries:
                lines.append(f"  {tb_label}: {raw_key} = {_fmt_num(value)}")

        return "\n".join(lines)

    # ------------------ Slash-команды ------------------
    @commands.slash_command(name="тб_отчет", description="Управление отчётами по ТБ")
    @commands.has_any_role(1153753506772164629)
    async def tb_report(self, inter: disnake.ApplicationCommandInteraction):
        pass

    @tb_report.sub_command(name="последняя", description="Сводка последней завершённой ТБ")
    async def tb_last(self, inter: disnake.ApplicationCommandInteraction):
        await inter.response.defer()
        try:
            guild = await asyncio.wait_for(
                asyncio.to_thread(self.comlink.get_guild, self.guild_id, include_recent_guild_activity_info=True),
                timeout=15.0
            )
        except asyncio.TimeoutError:
            await inter.edit_original_message("⏰ Запрос к Comlink занял слишком много времени.")
            return
        except Exception as e:
            await inter.edit_original_message(f"Ошибка получения данных: {e}")
            return

        result = guild.get("recentTerritoryBattleResult", [])
        if not result:
            await inter.edit_original_message("Нет данных о последней ТБ.")
            return

        members = guild.get("member", [])
        player_names = {m["playerId"]: m["playerName"] for m in members if "playerId" in m and "playerName" in m}
        stats = self._collect_guild_stats(result, player_names)
        if not stats:
            await inter.edit_original_message("Нет очков в последней ТБ.")
            return

        report = self._format_stats_table("📊 **Итоги последней ТБ (автоотчёт)**", stats)
        await self.send_as_file(inter.channel, report, "tb_report.txt")
        await inter.edit_original_message("Отчёт отправлен файлом.")

    @tb_report.sub_command(name="игрок", description="Статистика игрока за последнюю ТБ по фазам и планетам")
    async def tb_player(
        self,
        inter: disnake.ApplicationCommandInteraction,
        name: str = commands.Param(description="Выберите игрока", autocomplete=autocomplete_players)
    ):
        await inter.response.defer()

        cache = self.bot.guild_roster_cache
        if not cache or name not in cache:
            await inter.edit_original_message("Ошибка: игрок не найден в кэше состава.")
            return
        allycode = cache[name]

        try:
            player = await asyncio.to_thread(self.comlink.get_player, allycode=allycode)
            player_id = player.get("playerId")
            if not player_id:
                await inter.edit_original_message("Не удалось определить игровой ID.")
                return

            guild = await asyncio.wait_for(
                asyncio.to_thread(self.comlink.get_guild, self.guild_id, include_recent_guild_activity_info=True),
                timeout=15.0
            )
            result = guild.get("recentTerritoryBattleResult", [])
            if not result:
                await inter.edit_original_message("Нет данных о последней ТБ.")
                return

            zone_data, global_totals, round_totals, raw_keys, unrecognized = self._decode_tb_stats(result, player_id)
            if not zone_data and not round_totals and not global_totals:
                await inter.edit_original_message(f"{name} не участвовал в последней ТБ.")
                return

            planet_map = database.get_tb_planet_names()
            report = self._format_tb_player_report(name, zone_data, global_totals, round_totals, raw_keys, planet_map)
            if unrecognized:
                report += "\n\nНераспознанные ключи (новая механика?):\n"
                report += "\n".join(f"  {k}: {v}" for k, v in unrecognized.items())

            await self.send_as_file(inter.channel, report, f"tb_{name}.txt")
            embed = disnake.Embed(title=f"📊 ТБ по фазам и планетам: {name}", color=0x3498db)
            embed.set_footer(text="Полная детализация отправлена файлом.")
            await inter.edit_original_message(embed=embed)

        except asyncio.TimeoutError:
            await inter.edit_original_message("⏰ Запрос к Comlink занял слишком много времени.")
        except Exception as e:
            await inter.edit_original_message(f"Ошибка: {e}")

    @tb_report.sub_command(name="сравнение_по_тб", description="Сравнение игроков по гильдии за последние 3 ТБ")
    async def tb_compare(self, inter: disnake.ApplicationCommandInteraction):
        await inter.response.defer()

        events = database.get_recent_tb_events()
        if not events:
            await inter.edit_original_message("Пока нет накопленной истории ТБ для сравнения.")
            return

        summary_rows = database.get_tb_player_summary_for_events([e[0] for e in events])
        if not summary_rows:
            await inter.edit_original_message("Нет сохранённых данных игроков для сравнения.")
            return

        report = self._format_tb_compare_table(events, summary_rows)
        title = f"📊 Сравнение по ТБ (последние {len(events)}: " + ", ".join(e[1][:10] for e in events) + ")"
        await self.send_as_file(inter.channel, title + "\n\n" + report, "tb_compare.txt")
        await inter.edit_original_message("Отчёт сравнения отправлен файлом.")

    @tb_report.sub_command(name="сравнение_по_игроку", description="Сравнение статистики игрока по фазам за последние 3 ТБ")
    async def tb_player_compare(
        self,
        inter: disnake.ApplicationCommandInteraction,
        name: str = commands.Param(description="Выберите игрока", autocomplete=autocomplete_players)
    ):
        await inter.response.defer()

        cache = self.bot.guild_roster_cache
        if not cache or name not in cache:
            await inter.edit_original_message("Ошибка: игрок не найден в кэше состава.")
            return
        allycode = cache[name]

        try:
            player = await asyncio.to_thread(self.comlink.get_player, allycode=allycode)
            player_id = player.get("playerId")
            if not player_id:
                await inter.edit_original_message("Не удалось определить игровой ID.")
                return
        except Exception as e:
            await inter.edit_original_message(f"Ошибка: {e}")
            return

        events = database.get_recent_tb_events()
        if not events:
            await inter.edit_original_message("Пока нет накопленной истории ТБ для сравнения.")
            return

        per_event = []
        for event_id, _ in events:
            row = database.get_tb_player_detail(event_id, player_id)
            if row:
                zone_data_json, global_totals_json, round_totals_json, raw_keys_json = row
                per_event.append({
                    "zone_data": json.loads(zone_data_json),
                    "global_totals": json.loads(global_totals_json),
                    "round_totals": json.loads(round_totals_json),
                    "raw_keys": json.loads(raw_keys_json),
                })
            else:
                per_event.append(None)

        if all(e is None for e in per_event):
            await inter.edit_original_message(f"{name} не участвовал ни в одной из сохранённых последних ТБ.")
            return

        planet_maps = [database.get_tb_event_planet_names(event_id) for event_id, _ in events]
        report = self._format_tb_player_compare_report(name, events, per_event, planet_maps)
        await self.send_as_file(inter.channel, report, f"tb_compare_{name}.txt")
        embed = disnake.Embed(title=f"📊 Сравнение по ТБ: {name}", color=0x9b59b6)
        embed.set_footer(text="Полная детализация отправлена файлом.")
        await inter.edit_original_message(embed=embed)

    @tb_report.sub_command(
        name="план",
        description="Вручную задать/поправить планету для фазы и ветки (фолбэк, если авто-разбор анонса не сработал)"
    )
    async def tb_set_plan(
        self,
        inter: disnake.ApplicationCommandInteraction,
        этап: int = commands.Param(description="Номер фазы", ge=1, le=6),
        ветка: str = commands.Param(
            description="Ветка",
            choices={
                "Light (красная)": "01",
                "Dark (жёлтая)": "02",
                "Mixed (синяя)": "03",
                "Бонус/ОЗ-зона": "bonus",
            },
        ),
        планета: str = commands.Param(description="Название планеты"),
    ):
        planet_name = планета.strip()
        database.set_tb_planet_name(str(этап), ветка, planet_name, source="manual")
        await inter.response.send_message(
            f"✅ Этап {этап}: сохранена планета «{planet_name}»", ephemeral=True
        )

    @tb_report.sub_command(name="синхронизация", description="Привязать Discord-пользователей к игровым аккаунтам")
    async def tb_sync_members(self, inter: disnake.ApplicationCommandInteraction):
        await inter.response.defer(ephemeral=True)
        try:
            guild = await asyncio.to_thread(self.comlink.get_guild, self.guild_id)
        except Exception as e:
            await inter.edit_original_message(f"Ошибка получения гильдии: {e}")
            return

        discord_guild = inter.guild
        if not discord_guild:
            await inter.edit_original_message("Команда доступна только на сервере.")
            return

        members = guild.get("member", [])
        linked = 0
        not_found = []
        for m in members:
            player_id = m.get("playerId")
            player_name = m.get("playerName")
            if not player_id or not player_name:
                continue
            try:
                player = await asyncio.to_thread(self.comlink.get_player, player_id=player_id)
                allycode = player.get("allyCode")
                if not allycode:
                    continue
            except Exception:
                continue

            normalized = re.sub(r'\W+', '', player_name).lower()
            member_found = None
            for discord_member in discord_guild.members:
                if re.sub(r'\W+', '', discord_member.display_name).lower() == normalized:
                    member_found = discord_member
                    break
            if not member_found:
                not_found.append(player_name)
                continue

            database.set_user_mapping(str(member_found.id), str(allycode), player_name)
            linked += 1

        msg = f"✅ Привязано: {linked} игроков."
        if not_found:
            msg += f"\nНе найдены на сервере: {', '.join(not_found[:10])}"
        await inter.edit_original_message(msg)


def setup(bot: commands.Bot):
    bot.add_cog(GuildEvents(bot))