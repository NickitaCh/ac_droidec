"""Энка (рейдовые билеты) — напоминания и итог дня в канал гильдии (как у HotBot):
за 2 ч и за 30 мин до сброса — список недосдавших с тегами, после сброса — итог
без тегов (или «все сдали — N дней подряд»). Логика — services/raid_tickets.py.

Состояние в bot_state (переживает рестарт): момент сброса, сделанные этапы,
последний снимок билетов. Comlink дёргается только в моменты этапов (+1 раз на
старте, чтобы узнать время сброса), не каждую минуту."""

import asyncio
import json
import time

import disnake
from disnake.ext import commands, tasks

import database
from services import feature_flags
from services import raid_tickets as rt

STATE_RESET = "raid_tickets:reset"
STATE_DONE = "raid_tickets:done"   # "<reset>:r120,r30,snap,final"
STATE_SNAP = "raid_tickets:snap"   # {"reset": R, "data": {player_id: [current, lifetime]}}


class RaidTicketsCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.raid_tickets_loop.start()

    def cog_unload(self):
        self.raid_tickets_loop.cancel()

    async def _fetch(self, swgoh_guild_id: str):
        resp = await asyncio.to_thread(
            self.bot.comlink.get_guild, swgoh_guild_id, include_recent_guild_activity_info=True
        )
        return rt.parse_guild(resp)

    @staticmethod
    def _done(gid: int, reset: int) -> set:
        raw = database.get_bot_state(STATE_DONE, guild_id=gid) or ""
        prefix, _, stages = raw.partition(":")
        return set(stages.split(",")) - {""} if prefix == str(reset) else set()

    @staticmethod
    def _mark(gid: int, reset: int, done: set, stage: str) -> None:
        done.add(stage)
        database.set_bot_state(STATE_DONE, f"{reset}:{','.join(sorted(done))}", guild_id=gid)

    @staticmethod
    def _save_snapshot(gid: int, reset: int, members: list) -> None:
        database.set_bot_state(STATE_SNAP, json.dumps({"reset": reset, "data": rt.snapshot(members)}), guild_id=gid)

    async def _send(self, channel_id: str, lines: list[str], mentions: bool) -> None:
        channel = self.bot.get_channel(int(channel_id))
        if channel is None:
            print(f"⚠️ [Энка] Канал {channel_id} не найден")
            return
        allowed = disnake.AllowedMentions(users=True, roles=False, everyone=False) if mentions else disnake.AllowedMentions.none()
        for text in rt.chunk(lines):
            await channel.send(text, allowed_mentions=allowed)

    def _names(self, gid: int) -> dict:
        return database.get_roster_by_player_id(gid)  # {player_id: (ally_code, name)}

    async def _reminder(self, cfg: dict, reset: int, stage: str, members: list, now: int) -> None:
        gid = cfg["id"]
        roster = self._names(gid)
        debtors = []
        for m in sorted((m for m in members if m["current"] < rt.REQUIRED), key=lambda m: m["current"]):
            code, name = roster.get(m["player_id"], (None, None))
            discord_id = database.get_discord_id_for_ally(code, guild_id=gid) if code else None
            debtors.append((name or m["name"], m["current"], discord_id))
        if not debtors and stage == "r120":
            return  # за 2 часа «все сдали» не пишем — только за 30 мин
        await self._send(cfg["raid_tickets_channel_id"], rt.reminder_lines(cfg["name"], reset, now, debtors), True)

    async def _final(self, cfg: dict, reset: int, members_after: list) -> bool:
        gid = cfg["id"]
        raw = database.get_bot_state(STATE_SNAP, guild_id=gid)
        snap = json.loads(raw) if raw else {}
        if snap.get("reset") != reset or not snap.get("data"):
            print(f"⚠️ [Энка] [{cfg['name']}] Нет снимка до сброса — итог дня пропущен")
            return False
        totals = rt.final_totals(snap["data"], members_after)
        roster = self._names(gid)
        names = {m["player_id"]: m["name"] for m in members_after}
        rows = []
        for pid, tickets in totals.items():
            code, name = roster.get(pid, (None, None))
            rows.append((pid, code, name or names.get(pid, pid), tickets))
        database.save_raid_ticket_day(gid, rt.day_label(reset), rows)
        debtors = sorted(((name, t) for _pid, _code, name, t in rows if t < rt.REQUIRED), key=lambda d: d[1])
        streak = database.get_raid_ticket_full_streak(gid, rt.REQUIRED)
        await self._send(cfg["raid_tickets_channel_id"], rt.final_lines(cfg["name"], debtors, streak), False)
        return True

    async def _process_guild(self, cfg: dict, now: int) -> None:
        gid = cfg["id"]
        raw_reset = database.get_bot_state(STATE_RESET, guild_id=gid)
        reset = int(raw_reset) if raw_reset and raw_reset.isdigit() else None
        if reset is None:
            reset, _ = await self._fetch(cfg["swgoh_guild_id"])
            if reset is None:
                return
            database.set_bot_state(STATE_RESET, str(reset), guild_id=gid)

        done = self._done(gid, reset)

        # Напоминания с тегами.
        for stage, before in rt.REMINDERS:
            at = reset - before
            if stage in done or now < at:
                continue
            if now >= at + rt.STAGE_WINDOW:
                self._mark(gid, reset, done, stage)  # опоздали (бот лежал) — молча пропускаем
                continue
            _, members = await self._fetch(cfg["swgoh_guild_id"])
            self._save_snapshot(gid, reset, members)
            self._mark(gid, reset, done, stage)  # до отправки: сбой Discord не даст дубль через минуту
            await self._reminder(cfg, reset, stage, members, now)

        # Тихий снимок за 5 минут до сброса — база для итога.
        if "snap" not in done and reset - rt.SNAPSHOT_BEFORE <= now < reset:
            _, members = await self._fetch(cfg["swgoh_guild_id"])
            self._save_snapshot(gid, reset, members)
            self._mark(gid, reset, done, "snap")

        # Итог после сброса, затем переход на следующий день.
        if now >= reset + rt.FINAL_AFTER:
            next_reset, members_after = await self._fetch(cfg["swgoh_guild_id"])
            due = "final" not in done and now < reset + rt.FINAL_WINDOW
            self._mark(gid, reset, done, "final")
            if not next_reset or next_reset <= reset:
                next_reset = reset + 86400
            database.set_bot_state(STATE_RESET, str(next_reset), guild_id=gid)
            if due:
                await self._final(cfg, reset, members_after)

    @tasks.loop(seconds=60)
    async def raid_tickets_loop(self):
        now = int(time.time())
        for cfg in database.get_all_guild_configs():
            if not (cfg.get("raid_tickets_channel_id") and cfg.get("swgoh_guild_id")):
                continue
            if not feature_flags.is_enabled(cfg["id"], "raid_tickets"):
                continue
            try:
                await self._process_guild(cfg, now)
            except Exception as e:
                # Один сбой (Comlink/Discord) не должен останавливать цикл навсегда.
                print(f"⚠️ [Энка] [{cfg.get('name')}] {e!r}")

    @raid_tickets_loop.before_loop
    async def before_raid_tickets(self):
        await self.bot.wait_until_ready()


def setup(bot):
    bot.add_cog(RaidTicketsCog(bot))
