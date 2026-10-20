"""Рейды: сохранение очков участников за каждый завершённый рейд и отчёты по ним
(как C3PO `raid history` / HotBot `/raids scorehistory`). Comlink отдаёт только
последние несколько рейдов, поэтому цикл раз в 3 часа дописывает новые в базу, а
история копится там. Логика — services/raids.py, страница — веб /raids."""

import asyncio

import disnake
from disnake.ext import commands, tasks

import database
import guild_resolver
from cogs.violations import autocomplete_players
from services import feature_flags
from services import raids as rs

PLAYER_HISTORY = 12  # рейдов в истории игрока


def store_recent(gid: int, resp: dict) -> int:
    """Записывает новые рейды из ответа get_guild; возвращает, сколько записано."""
    roster = database.get_roster_by_player_id(gid)  # {player_id: (ally_code, name)}
    known = database.get_raid_known_names(gid)       # {player_id: (name, ally_code)}
    saved = 0
    for raid in rs.parse_recent(resp):
        rows = []
        for m in raid["members"]:
            code, name = roster.get(m["player_id"], (None, None))
            old_name, old_code = known.get(m["player_id"], (None, None))
            rows.append((m["player_id"], code or old_code, name or raid["names"].get(m["player_id"]) or old_name or m["player_id"],
                         m["score"], m["rank"]))
        if database.save_raid_result(gid, raid, rows):
            saved += 1
    return saved


async def autocomplete_raids(inter: disnake.ApplicationCommandInteraction, string: str):
    guild_id = guild_resolver.resolve_guild_id(inter.author)
    if guild_id is None:
        return []
    out = []
    for r in database.get_raid_results(guild_id, limit=25):
        label = f"{rs.raid_name(r['raid_id'])} · {rs.end_date(r['end_time'])}"
        if string.lower() in label.lower():
            out.append(disnake.OptionChoice(name=label, value=r["key"]))
    return out


class RaidsCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.raids_loop.start()

    def cog_unload(self):
        self.raids_loop.cancel()

    @tasks.loop(hours=3)
    async def raids_loop(self):
        for cfg in database.get_all_guild_configs():
            if not cfg.get("swgoh_guild_id") or not feature_flags.is_enabled(cfg["id"], "raids"):
                continue
            try:
                resp = await asyncio.to_thread(
                    self.bot.comlink.get_guild, cfg["swgoh_guild_id"], include_recent_guild_activity_info=True
                )
                saved = store_recent(cfg["id"], resp)
                if saved:
                    print(f"⚔️ [Рейды] [{cfg.get('name')}] сохранено новых рейдов: {saved}")
                await self._resolve_unnamed(cfg["id"])
            except Exception as e:
                # Один сбой Comlink не должен останавливать цикл навсегда.
                print(f"⚠️ [Рейды] [{cfg.get('name')}] {e!r}")

    async def _resolve_unnamed(self, gid: int) -> None:
        """Ушедшие из гильдии до сохранения рейда остаются с сырым playerId — имя и код
        союзника берём из игры по playerId (по одному запросу на игрока, один раз)."""
        for pid in database.get_raid_unnamed_players(gid)[:20]:
            try:
                player = await asyncio.to_thread(self.bot.comlink.get_player, player_id=pid)
            except Exception as e:
                print(f"⚠️ [Рейды] get_player {pid}: {e!r}")
                continue
            name = (player or {}).get("name")
            if name:
                code = str(player.get("allyCode") or "") or None
                database.set_raid_player_identity(gid, pid, name, code)

    @raids_loop.before_loop
    async def before_raids(self):
        await self.bot.wait_until_ready()

    @commands.slash_command(name="рейд", description="Очки гильдии в рейдах")
    async def raid_group(self, inter: disnake.ApplicationCommandInteraction):
        pass

    @raid_group.sub_command(name="итоги", description="Очки всех участников за рейд и кто ниже порога")
    async def raid_summary(
        self,
        inter: disnake.ApplicationCommandInteraction,
        рейд: str = commands.Param(default=None, description="Рейд (пусто → последний)", autocomplete=autocomplete_raids),
        порог: float = commands.Param(default=None, ge=0, description="Порог в миллионах очков — кто набрал меньше"),
    ):
        await inter.response.defer()
        guild_id = await guild_resolver.require_feature(inter, "raids")
        if guild_id is None:
            return
        results = database.get_raid_results(guild_id, limit=50)
        raid = next((r for r in results if r["key"] == рейд), None) if рейд else (results[0] if results else None)
        if raid is None:
            await inter.edit_original_response(
                "❌ Рейд не найден." if рейд else "Сохранённых рейдов пока нет — бот записывает их после завершения рейда."
            )
            return
        scores = database.get_raid_scores(guild_id, [raid["key"]])
        s = rs.summarize(raid, scores)
        lines = [f"{i}. **{p['name']}** — {rs.millions(p['score'])}" for i, p in enumerate(s["ranked"], 1)]
        head = (f"Всего {rs.millions(s['total'])} · участников {s['count']} · "
                f"в среднем {rs.millions(s['avg'])} · медиана {rs.millions(s['median'])}")
        embed = disnake.Embed(
            title=f"⚔️ {rs.raid_name(raid['raid_id'])} — {rs.end_date(raid['end_time'])}",
            description=head + "\n\n" + "\n".join(lines),
            color=disnake.Color.dark_red(),
        )
        if порог is not None:
            limit = порог * 1_000_000
            below = [p for p in s["ranked"] if p["score"] < limit]
            text = "\n".join(f"**{p['name']}** — {rs.millions(p['score'])}" for p in reversed(below)) or "Таких нет 👍"
            embed.add_field(name=f"Ниже {rs.millions(limit)} ({len(below)})", value=text[:1024], inline=False)
        if len(embed.description) > 4096:
            embed.description = embed.description[:4090] + "\n…"
        await inter.edit_original_response(embed=embed)

    @raid_group.sub_command(name="игрок", description="История очков игрока в рейдах")
    async def raid_player(
        self,
        inter: disnake.ApplicationCommandInteraction,
        игрок: str = commands.Param(default=None, description="Игрок гильдии (пусто → вы)", autocomplete=autocomplete_players),
        аликод: str = commands.Param(default=None, description="Код союзника вместо выбора из списка"),
    ):
        await inter.response.defer()
        guild_id = await guild_resolver.require_feature(inter, "raids")
        if guild_id is None:
            return
        if аликод:
            ally_code = guild_resolver.normalize_ally_code(аликод)
            if ally_code is None:
                await inter.edit_original_response("❌ Код союзника должен состоять из 9 цифр.")
                return
        elif игрок:
            cache = self.bot.guild_roster_caches.get(guild_id) or {}
            ally_code = cache.get(игрок) if игрок in cache else None
            if ally_code is None:
                await inter.edit_original_response("❌ Игрок не найден — выберите его из списка.")
                return
        else:
            registration = database.get_user_registration(str(inter.author.id), guild_id=guild_id)
            if not registration:
                await inter.edit_original_response("❌ Вы не зарегистрированы — используйте `/регистрация` или выберите игрока.")
                return
            ally_code = registration[0]
        ally_code = str(ally_code)
        error = guild_resolver.self_only_error(inter.author, guild_id, ally_code)
        if error:
            await guild_resolver.send_denied(inter, error)
            return

        results = database.get_raid_results(guild_id, limit=PLAYER_HISTORY)
        scores = database.get_raid_scores(guild_id, [r["key"] for r in results])
        by_raid = {}
        for row in scores:
            by_raid.setdefault(row["raid_key"], []).append(row)
        lines, name = [], None
        for r in results:
            rows = sorted(by_raid.get(r["key"], []), key=lambda x: -x["score"])
            mine = next(((i, x) for i, x in enumerate(rows, 1) if x["ally_code"] == ally_code), None)
            label = f"{rs.end_date(r['end_time'])} · {rs.raid_name(r['raid_id'])}"
            if mine is None:
                lines.append(f"{label} — не участвовал(а)")
                continue
            place, row = mine
            name = name or row["name"]
            avg = sum(x["score"] for x in rows) / len(rows) if rows else 0
            diff = (row["score"] / avg - 1) * 100 if avg else 0
            lines.append(f"{label} — **{rs.millions(row['score'])}** · {place} место из {len(rows)} · "
                         f"{'+' if diff >= 0 else '−'}{abs(diff):.0f}% к среднему")
        if not results:
            await inter.edit_original_response("Сохранённых рейдов пока нет — бот записывает их после завершения рейда.")
            return
        if name is None:
            name = database.get_player_names(guild_id).get(ally_code, ally_code)
        embed = disnake.Embed(title=f"⚔️ Рейды — {name}", description="\n".join(lines), color=disnake.Color.dark_red())
        await inter.edit_original_response(embed=embed)


def setup(bot):
    bot.add_cog(RaidsCog(bot))
