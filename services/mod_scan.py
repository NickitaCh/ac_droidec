"""Скан моддинга ЧУЖИХ гильдий ("шпион по модам") — по запросу пользователя (чат,
2026-09-15): раз в 5 минут (cogs/mod_scan.py::mod_scan_loop) пройтись по 1-5 указанным
гильдиям (SWGOH guild ID или аликод любого её участника) и записать, у кого что
поменялось в моддинге — отдельно "минорные" изменения и "аномалии" (сильный % сдвиг по
вторичкам, либо смена сета/основного стата на слоте).

Bulk-эндпоинта с модами по всей гильдии сразу НЕТ — comlink.get_guild(...) отдаёт только
список участников (playerId/playerName, без allyCode и без ростера/модов, см.
services/steal_build.py::resolve_guild, тот же факт уже задокументирован там). Полные
данные (rosterUnit.equippedStatMod) даёт только comlink.get_player(...) по одному
игроку — отсюда тот же паттерн, что build_report в steal_build.py: живой get_player на
каждого участника чужой гильдии, через to_thread, с паузой между вызовами.

Формат мода decode_mod(...) — services/mod_search.py, переиспользуем как единственный
источник правды по разбору definitionId/статов (там же вся история эмпирической
проверки слотов/статов), не дублируем её здесь."""

import asyncio

import database
from services.mod_search import decode_mod

# Порог относительного изменения вторички (|new-old|/max(old, ANOMALY_MIN_BASE)), с
# которого изменение мода на том же сете/основе уже считается аномалией, а не рутинной
# калибровкой. 0.25 = ±25% — старт по запросу пользователя ("изменение % прибавки по
# статам"), крутить тут, если разделение шумное.
ANOMALY_RELATIVE_THRESHOLD = 0.25
ANOMALY_MIN_BASE = 0.01

# Пауза между comlink.get_player по участникам одной цели — то же значение, что уже
# использует cogs/stat_requirements.py::player_units_sync_loop для своих ~50-200 игроков,
# не изобретаем новый темп для похожей нагрузки на тот же локальный comlink.
PLAYER_FETCH_SLEEP = 0.1


class TargetLookupResult:
    def __init__(self, ok: bool, error: str = None, swgoh_guild_id: str = None,
                 guild_name: str = None, members: list = None):
        self.ok = ok
        self.error = error
        self.swgoh_guild_id = swgoh_guild_id
        self.guild_name = guild_name
        self.members = members or []  # [(player_id, player_name), ...]


async def resolve_target(comlink, input_value: str) -> TargetLookupResult:
    """input_value — 9-значный аликод любого участника ЛИБО уже готовый SWGOH guild ID
    (та длинная строка, которую видно в игре/на swgoh.gg). Аликод неотличим по формату от
    короткого guild ID только в теории — реальные guild ID у Comlink не 9-значные цифры,
    поэтому проверка "9 цифр" (guild_resolver.normalize_ally_code) однозначно разделяет
    два случая, как и в services/steal_build.py::resolve_guild."""
    import guild_resolver

    ally_code = guild_resolver.normalize_ally_code(input_value)
    swgoh_guild_id = None
    if ally_code:
        try:
            player_data = await asyncio.to_thread(comlink.get_player, allycode=ally_code)
        except Exception as e:
            return TargetLookupResult(False, error=f"Не удалось проверить код союзника: {e}")
        if not player_data or "name" not in player_data:
            return TargetLookupResult(False, error=f"Игрок с кодом союзника {ally_code} не найден.")
        swgoh_guild_id = player_data.get("guildId")
        if not swgoh_guild_id:
            return TargetLookupResult(False, error="Этот игрок не состоит ни в одной гильдии SWGOH.")
    else:
        swgoh_guild_id = (input_value or "").strip()
        if not swgoh_guild_id:
            return TargetLookupResult(False, error="Укажите аликод участника или ID гильдии.")

    try:
        guild = await asyncio.to_thread(comlink.get_guild, swgoh_guild_id, include_recent_guild_activity_info=True)
    except Exception as e:
        return TargetLookupResult(False, error=f"Не удалось получить данные гильдии из Comlink: {e}")
    guild = guild.get("guild", guild)
    profile = guild.get("profile", {})
    if not profile and not guild.get("member"):
        return TargetLookupResult(False, error=f"Гильдия с ID {swgoh_guild_id} не найдена.")
    name = profile.get("name") or f"Гильдия {swgoh_guild_id}"
    members = [(m["playerId"], m.get("playerName") or "") for m in guild.get("member", []) if m.get("playerId")]
    return TargetLookupResult(True, swgoh_guild_id=str(swgoh_guild_id), guild_name=name, members=members)


def _unit_mods(player_data: dict) -> dict:
    """player_data -> {base_id: raw_equippedStatMod_list} только для юнитов с надетыми
    модами (пустые пропускаем — нечего диффить и незачем занимать снапшот)."""
    roster = player_data.get("rosterUnit") or player_data.get("roster") or []
    out = {}
    for u in roster:
        mods = u.get("equippedStatMod") or []
        if not mods:
            continue
        base_id = u.get("baseId") or (u.get("definitionId", "") or "").split(":")[0]
        if base_id:
            out[base_id] = mods
    return out


def _stat_label(decoded_stat) -> str:
    return decoded_stat["name"] if decoded_stat else "?"


def _fmt_set_primary(decoded: dict) -> str:
    primary = _stat_label(decoded.get("primary"))
    return f"сет #{decoded['set_id']}/{primary}"


def _diff_unit_mods(old_mods: list, new_mods: list) -> list[tuple[str, str]]:
    """Сравнивает старый/новый equippedStatMod ОДНОГО юнита по слотам. Возвращает
    [(kind, description), ...] — 'minor' или 'anomaly' на каждый реально изменившийся
    слот; неизменившиеся/новые-без-базы слоты не попадают."""
    old_by_slot = {d["slot_key"]: d for d in (decode_mod(m) for m in old_mods) if d}
    new_by_slot = {d["slot_key"]: d for d in (decode_mod(m) for m in new_mods) if d}
    events = []
    for slot_key in set(old_by_slot) | set(new_by_slot):
        old_d, new_d = old_by_slot.get(slot_key), new_by_slot.get(slot_key)
        if old_d is None or new_d is None:
            present = new_d or old_d
            action = "надет" if new_d else "снят"
            events.append(("minor", f"{present['slot_label']}: мод {action}"))
            continue
        old_primary_id = (old_d["primary"] or {}).get("stat_id")
        new_primary_id = (new_d["primary"] or {}).get("stat_id")
        if old_d["set_id"] != new_d["set_id"] or old_primary_id != new_primary_id:
            events.append((
                "anomaly",
                f"{new_d['slot_label']}: сменил сет/основу — было {_fmt_set_primary(old_d)}, стало {_fmt_set_primary(new_d)}",
            ))
            continue
        old_secondaries = {s["stat_id"]: s for s in old_d["secondaries"]}
        new_secondaries = {s["stat_id"]: s for s in new_d["secondaries"]}
        biggest_relative = 0.0
        biggest_desc = None
        for stat_id in set(old_secondaries) | set(new_secondaries):
            old_s, new_s = old_secondaries.get(stat_id), new_secondaries.get(stat_id)
            old_v = old_s["value"] if old_s else 0.0
            new_v = new_s["value"] if new_s else 0.0
            if old_v == new_v:
                continue
            relative = abs(new_v - old_v) / max(abs(old_v), ANOMALY_MIN_BASE)
            if relative > biggest_relative:
                biggest_relative = relative
                stat_name = _stat_label(new_s or old_s)
                biggest_desc = f"{new_d['slot_label']} ({stat_name}): было {old_v:g}, стало {new_v:g}"
        if biggest_desc is None:
            continue
        kind = "anomaly" if biggest_relative >= ANOMALY_RELATIVE_THRESHOLD else "minor"
        events.append((kind, biggest_desc))
    return events


async def scan_target(comlink, target: dict):
    """Один прогон одной цели: тянет свежий ростер, диффит против mod_scan_snapshot,
    пишет события и обновляет снапшот+статус цели. Ошибки на уровне ОТДЕЛЬНОГО игрока не
    прерывают скан остальных (см. docstring выше); ошибка на уровне САМОЙ гильдии (сеть/ID
    протух) пишется в last_error и снапшот не трогается."""
    target_id = target["id"]
    try:
        guild = await asyncio.to_thread(
            comlink.get_guild, target["swgoh_guild_id"], include_recent_guild_activity_info=True
        )
    except Exception as e:
        database.update_mod_scan_target_sync(target_id, None, None, error=str(e))
        return
    guild = guild.get("guild", guild)
    profile = guild.get("profile", {})
    guild_name = profile.get("name") or target.get("guild_name") or f"Гильдия {target['swgoh_guild_id']}"
    members = [(m["playerId"], m.get("playerName") or "") for m in guild.get("member", []) if m.get("playerId")]
    if not members:
        database.update_mod_scan_target_sync(target_id, None, None, error="Гильдия пуста или недоступна.")
        return

    old_snapshot = database.get_mod_scan_snapshot(target_id)
    new_entries = []
    events = []
    for player_id, player_name in members:
        try:
            player_data = await asyncio.to_thread(comlink.get_player, player_id=player_id)
        except Exception:
            await asyncio.sleep(PLAYER_FETCH_SLEEP)
            continue
        ally_code = str(player_data.get("allyCode") or "")
        for base_id, mods in _unit_mods(player_data).items():
            new_entries.append((ally_code, player_name, base_id, mods))
            if ally_code:
                old_mods = old_snapshot.get((ally_code, base_id))
                if old_mods is not None:
                    for kind, description in _diff_unit_mods(old_mods, mods):
                        events.append((target_id, ally_code, player_name, base_id, kind, description))
        await asyncio.sleep(PLAYER_FETCH_SLEEP)

    database.upsert_mod_scan_snapshot(target_id, new_entries)
    database.add_mod_scan_events(events)
    database.update_mod_scan_target_sync(target_id, guild_name, len(members), error=None)
