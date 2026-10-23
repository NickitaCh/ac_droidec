"""Рейды: сохранение очков участников за каждый завершённый рейд и отчёты по ним
(как C3PO `raid history` / HotBot `/raids scorehistory`). Comlink отдаёт только
последние несколько рейдов, поэтому цикл раз в 3 часа дописывает новые в базу, а
история копится там. Логика — services/raids.py, страница — веб /raids."""

import asyncio
import io
import json
import time

import disnake
from disnake.ext import commands, tasks

import database
import guild_resolver
from services import feature_flags, guild_compare
from services import raids as rs
from services import guild_units, guild_units_image, raids_image

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


async def autocomplete_raid_kinds(inter: disnake.ApplicationCommandInteraction, string: str):
    kinds = rs.raid_kinds()
    return [disnake.OptionChoice(name=rs.raid_name(k), value=k) for k in kinds if string.lower() in rs.raid_name(k).lower()][:25]


async def autocomplete_raid_teams(inter: disnake.ApplicationCommandInteraction, string: str):
    raid = inter.filled_options.get("рейд")
    if raid:
        raid = rs.resolve_raid_kind(raid) or raid
    else:
        guild_id = guild_resolver.resolve_guild_id(inter.author)
        last = database.get_raid_results(guild_id, limit=1) if guild_id is not None else []
        raid = last[0]["raid_id"] if last else "order66"
    entry = rs.raid_entry_units(raid) or {"units": []}
    teams = rs.raid_teams(raid, entry["units"], database.get_game_unit_names(entry["units"]))
    return [disnake.OptionChoice(name=f"{name} ({len(ids)})"[:100], value=name) for name, ids in teams
            if string.lower() in name.lower()][:25]


class RaidsCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.raids_loop.start()

    def cog_unload(self):
        self.raids_loop.cancel()

    async def _sync_raid_units(self) -> None:
        """Раз в сутки: кого пускают в каждый рейд (для /рейд готовность)."""
        last = database.get_bot_state(rs.RAID_UNITS_STATE + "_at", guild_id=0)
        if last and time.time() - float(last) < 86400 and database.get_bot_state(rs.RAID_UNITS_STATE, guild_id=0):
            return
        from services.units_sync import UNIT_DEFINITIONS_FLAG
        campaigns = (await asyncio.to_thread(self.bot.comlink.get_game_data, items=str(rs.CAMPAIGN_FLAG),
                                             include_pve_units=False)).get("campaign") or []
        units = (await asyncio.to_thread(self.bot.comlink.get_game_data, items=str(UNIT_DEFINITIONS_FLAG),
                                         include_pve_units=False)).get("units") or []
        known = set(database.get_game_unit_names([u.get("baseId") for u in units if u.get("baseId")]))
        data = rs.parse_raid_entry_units(campaigns, units, known)
        if data:
            old_raw = database.get_bot_state(rs.RAID_UNITS_STATE, guild_id=0)
            database.set_bot_state(rs.RAID_UNITS_STATE, json.dumps(data), guild_id=0)
            if old_raw:  # на первом проходе «новыми» были бы все рейды
                await self._announce_new_raids(set(json.loads(old_raw)), data)
            database.set_bot_state(rs.RAID_UNITS_STATE + "_at", str(time.time()), guild_id=0)
            summary = ", ".join("%s=%d" % (k, len(v["units"])) for k, v in data.items())
            print(f"⚔️ [Рейды] состав рейдов обновлён: {summary}")

    async def _announce_new_raids(self, old_ids: set, data: dict) -> None:
        """Новый рейд появился в данных игры — личка супер-админам: состав уже подхвачен,
        вручную остаётся только разбивка на отряды (RAID_TEAMS)."""
        from services import discord_rest
        for node_id in sorted(set(data) - old_ids):
            preview = next((p for p in rs.PREVIEW_RAIDS if rs.release_of(p, data) == node_id), None)
            lines = [f"⚔️ В игре появился рейд «{rs.raid_name(node_id)}» (`{node_id}`): "
                     f"{len(data[node_id]['units'])} персонажей, вход с {data[node_id]['min_stars']}★ — "
                     "уже в /рейд готовность."]
            if preview:
                lines.append(f"Предварительный список «{rs.raid_name(preview)}» скрыт, порядок колонок и уровни перенесены. "
                             f"Запись `{preview}` в PREVIEW_RAIDS можно удалить.")
            if node_id not in rs.RAID_TEAMS:
                lines.append("Отрядов пока нет (все персонажи одной группой) — заполнить RAID_TEAMS, когда появятся.")
            await discord_rest.notify_super_admins("\n".join(lines))

    @tasks.loop(hours=3)
    async def raids_loop(self):
        try:
            await self._sync_raid_units()
        except Exception as e:
            print(f"⚠️ [Рейды] не удалось обновить состав рейдов: {e!r}")
        for cfg in database.get_all_guild_configs():
            if not cfg.get("swgoh_guild_id"):
                continue
            try:
                resp = await asyncio.to_thread(
                    self.bot.comlink.get_guild, cfg["swgoh_guild_id"], include_recent_guild_activity_info=True
                )
                # Тот же ответ несёт прошлых соперников по ВГ — для подсказки в /вг_соперник.
                await asyncio.to_thread(guild_compare.save_recent_opponents, cfg["id"], resp)
                saved = (await asyncio.to_thread(store_recent, cfg["id"], resp)
                         if feature_flags.is_enabled(cfg["id"], "raids") else 0)
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
                if "not found" in str(e).lower():  # аккаунт удалён — больше не переспрашиваем
                    database.set_raid_player_identity(gid, pid, "Удалённый аккаунт", None)
                else:
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
        idx = results.index(raid)
        older = results[idx + 1:]
        prev = next((r for r in older if r["raid_id"] == raid["raid_id"]), older[0] if older else None)
        scores = database.get_raid_scores(guild_id, [raid["key"]] + ([prev["key"]] if prev else []))
        s = rs.summarize(raid, [x for x in scores if x["raid_key"] == raid["key"]])
        prev_scores = {x["player_id"]: x["score"] for x in scores if prev and x["raid_key"] == prev["key"]}
        limit = int(порог * 1_000_000) if порог is not None else None

        lines = [f"⚔️ **{rs.raid_name(raid['raid_id'])} — {rs.end_date(raid['end_time'])}**: всего {rs.millions(s['total'])}"
                 f" · участников {s['count']} · в среднем {rs.millions(s['avg'])} · медиана {rs.millions(s['median'])}"]
        if limit is not None:
            below = [p for p in s["ranked"] if p["score"] < limit]
            lines.append(f"Ниже {rs.millions(limit)} ({len(below)}): "
                         + (", ".join(f"{p['name']} — {rs.millions(p['score'])}" for p in reversed(below)) or "таких нет 👍"))
        png = await asyncio.to_thread(raids_image.render, raid, s["ranked"], prev, prev_scores, limit)
        await inter.edit_original_response(content="\n".join(lines)[:2000],
                                           file=disnake.File(io.BytesIO(png), filename=f"raid_{raid['raid_id']}.png"))

    @raid_group.sub_command(name="готовность", description="Кто в гильдии готов к рейду: допущенные персонажи на нужной реликвии")
    async def raid_readiness(
        self,
        inter: disnake.ApplicationCommandInteraction,
        реликвия: int = commands.Param(default=7, ge=0, le=10, description="Порог реликвии (0 — G13), по умолчанию R7"),
        рейд: str = commands.Param(default=None, description="Рейд (пусто → текущий)", autocomplete=autocomplete_raid_kinds),
        отряд: str = commands.Param(default=None, description="Отряд рейда (пусто → все персонажи)", autocomplete=autocomplete_raid_teams),
    ):
        await inter.response.defer()
        guild_id = await guild_resolver.require_feature(inter, "raids")
        if guild_id is None:
            return
        if рейд is None:
            last = database.get_raid_results(guild_id, limit=1)
            рейд = last[0]["raid_id"] if last else "order66"
        else:
            рейд = rs.resolve_raid_kind(рейд) or рейд
        entry = rs.raid_entry_units(рейд)
        if not entry:
            await inter.edit_original_response("❌ Рейд не найден или его состав ещё не загружен из игры — выберите рейд из списка.")
            return
        target = ("relic", str(реликвия)) if реликвия > 0 else ("gear", "13")
        names = {**(entry.get("names") or {}), **database.get_game_unit_names(entry["units"])}
        teams = rs.raid_teams(рейд, entry["units"], names)
        if отряд:
            needle = отряд.strip().lower()
            exact = [t for t in teams if t[0].lower() == needle]
            teams = exact or [t for t in teams if needle in t[0].lower()][:1]
            if not teams:
                await inter.edit_original_response("❌ Такого отряда в этом рейде нет — выберите из списка.")
                return
        grouped = {u: [target] for _name, ids in teams for u in ids}
        r = await asyncio.to_thread(guild_units.readiness_grid, guild_id, grouped)
        r["groups"] = [(name, len(ids)) for name, ids in teams]
        for u in r["units"]:  # ещё не вышедшие юниты анонса — имени в game_units нет
            u["name"] = names.get(u["base_id"], u["name"])
        if not r["players"]:
            await inter.edit_original_response("❌ Нет данных по ростерам гильдии — дождитесь ежечасного обновления.")
            return
        cfg = database.get_guild_config(guild_id) or {}
        level = f"R{реликвия}" if реликвия > 0 else "G13"
        title = f"Готовность к рейду «{rs.raid_name(рейд)}»" + (f", {отряд}" if отряд else "") + f" ({level})"
        png = await asyncio.to_thread(guild_units_image.render, r, cfg.get("name") or "Гильдия",
                                      f"в рейд пускают с {entry['min_stars']}★ · "
                                      + (f"предварительно: {entry['preview']}" if entry.get("preview") else "данные ежечасного обновления"),
                                      title)
        ready = sum(1 for p in r["players"] if p["ready"])
        content = (f"**{rs.raid_name(рейд)}**" + (f", {отряд}" if отряд else "") + f" — {len(grouped)} персонажей, порог {level}: "
                   f"готовы {ready} из {len(r['players'])}")
        if entry.get("tiers"):
            content += f"\n-# Уровни (порог для всех юнитов отряда, очки за бой): {rs.tiers_text(entry)}"
        if entry.get("preview"):
            content += f"\n-# 🔮 Предварительно — {entry['preview']}."
        await inter.edit_original_response(
            content=content,
            file=disnake.File(io.BytesIO(png), filename=f"raid_ready_{рейд}.png"),
        )

    @raid_group.sub_command(name="игрок", description="История очков игрока в рейдах")
    async def raid_player(
        self,
        inter: disnake.ApplicationCommandInteraction,
        участник: disnake.User = commands.Param(default=None, description=guild_resolver.TARGET_USER_DESCRIPTION),
        аликод: str = commands.Param(default=None, description=guild_resolver.TARGET_CODE_DESCRIPTION),
    ):
        await inter.response.defer()
        guild_id = await guild_resolver.require_feature(inter, "raids")
        if guild_id is None:
            return
        ally_code, _name, error = guild_resolver.target_player(участник, аликод, guild_id)
        if error:
            await inter.edit_original_response(error)
            return
        if ally_code is None:
            registration = database.get_user_registration(str(inter.author.id), guild_id=guild_id)
            if not registration:
                await inter.edit_original_response("❌ Вы не зарегистрированы — используйте `/регистрация` или укажите участника.")
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
