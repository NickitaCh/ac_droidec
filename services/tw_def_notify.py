"""Рассылка деф-плана ВГ в личку Discord и/или Telegram (веб /tw/def/plans/<id>/notify) — по
образцу рассылки взводов ТБ (tb_platoon_notify.py, оттуда же отправка через REST). Каждому
игроку: зона, пак, какие персонажи ставить и какой именно его датакрон (сезон, уровень и
бонусы 3/6/9 — чтобы найти его в игре). Запрос офицеров 2026-10-20 («рассылка по ВГ, где
будут прописаны ДК из игры»).

Кому отправлять — решает офицер галочками; мейн и альт одного Discord получают одно
сообщение с разбивкой по аккаунтам."""

import asyncio
import base64
import json
import re

import httpx

import database
import tb_platoon_notify
import tw_def_engine as engine

MESSAGE_LIMIT = 1900  # Discord режет сообщения на 2000 символах
DESTINATIONS = {"discord_only": ("discord",), "tg_only": ("tg",), "both": ("discord", "tg")}


def mode_for(destinations: list[str]) -> str | None:
    selected = set(destinations)
    return next((mode for mode, values in DESTINATIONS.items() if selected == set(values)), None)


def guild_mode(guild_id: int) -> str:
    mode = (database.get_guild_config(guild_id) or {}).get("tw_def_dm_mode")
    return mode if mode in DESTINATIONS else "both"


def _base_labels(season: dict) -> dict:
    labels = {}
    for level in (3, 6, 9):
        labels.update(dict(season.get(f"level{level}") or []))
    return labels


def describe_datacron(dc: dict, catalog: dict | None) -> str:
    """Человекочитаемое описание конкретного датакрона игрока: по нему его можно найти в игре."""
    set_id = dc.get("setId")
    level = len(dc.get("affix") or [])
    season = ((catalog or {}).get("seasons") or {}).get(set_id) or {}
    season_name = season.get("display_name") or f"сезон {set_id}"
    template = str(dc.get("templateId") or "")
    if dc.get("focused"):
        key = template.split("_focused_", 1)[-1]
        if key.endswith("_upgraded"):
            key = key[:-len("_upgraded")]
        from cogs.datacron_requirements import _focused_char_label
        return f"персональный ДК «{_focused_char_label(catalog, set_id, key)}», ур. {level} ({season_name})"
    labels = _base_labels(season)
    bonuses = []
    for lv, idx in ((3, 2), (6, 5), (9, 8)):
        affix = dc.get("affix") or []
        ability_id = affix[idx].get("abilityId") if len(affix) > idx else None
        if ability_id:
            bonuses.append(f"{lv}: {labels.get(ability_id, ability_id)}")
    text = f"ДК {season_name}, ур. {level}"
    return text + (" — " + "; ".join(bonuses) if bonuses else "")


def describe_recommendation(dc: dict, requirements: dict, catalog: dict | None, guild_id: int) -> str:
    """Рекомендация отряда, независимо от коллекции игрока и количества ДК."""
    from cogs.datacron_requirements import _focused_char_label, _level_label, DATACRON_NONE, DATACRON_ANY, DATACRON_STAT_LABELS
    row = requirements.get((dc.get("kind"), dc.get("req_id")))
    if row is None:
        return "Рекомендация удалена — уточните у офицеров"
    set_id = row[1]
    season = ((catalog or {}).get("seasons") or {}).get(set_id) or {}
    name = season.get("display_name") or f"сезон {set_id}"
    if dc.get("kind") == "focused":
        char = _focused_char_label(catalog, set_id, row[3]) if catalog else row[3]
        return f"персональный ДК «{char}», ур. {row[4]} ({name}) (рекомендуется)"
    bonuses = []
    for level, value in zip((3, 6, 9), row[3:6]):
        if value and value not in (DATACRON_NONE, DATACRON_ANY):
            bonuses.append(f"{level}: {_level_label(season.get(f'level{level}') or [], value)}")
    stats = row[10] if len(row) > 10 else None
    thresholds = database.get_datacron_requirement_stats(row[0], guild_id=guild_id)
    details = [str(stats)] if stats else []
    details.extend(f"{DATACRON_STAT_LABELS.get(stat_id, str(stat_id))} ≥ {minimum:g}%"
                   for stat_id, minimum in thresholds)
    title = f"ДК {name} (рекомендуется)" + (f" · {', '.join(details)}" if details else "")
    return title + (" — " + "; ".join(bonuses) if bonuses else "")


def build_player_rows(guild_id: int, plan: dict, catalog: dict | None) -> list[dict]:
    """[{ally_code, name, discord_id, entries: [{zone, squad, units: [имя], datacrons: [описание]}]}]."""
    assignments = database.get_tw_def_assignments(guild_id, plan["id"])
    squads = {s["id"]: s for s in database.list_tw_def_squads(guild_id)}
    requirements = {("base", r[0]): r for r in database.get_all_datacron_requirements(guild_id)}
    requirements.update({("focused", r[0]): r for r in database.get_all_datacron_focused_requirements(guild_id)})
    names = database.get_player_names(guild_id)
    for _discord_id, ally_code, ingame_name in database.get_all_user_mappings(guild_id):
        if ingame_name:
            names[ally_code] = ingame_name
    discord_by_ally = {code: did for did, code, _name, _main in database.get_all_registrations(guild_id)}
    unit_names = database.get_game_unit_names(list({b for a in assignments for b in a["units"]}))
    codes = list({a["ally_code"] for a in assignments})
    datacrons = database.get_player_datacrons_bulk(codes) if any(a.get("datacron_id") for a in assignments) else {}
    zone_order = {z["key"]: i for i, z in enumerate(engine.ZONES)}

    rows: dict[str, dict] = {}
    for a in sorted(assignments, key=lambda a: (zone_order.get(a["zone"], 99), a["squad_name"])):
        row = rows.setdefault(a["ally_code"], {
            "ally_code": a["ally_code"], "name": names.get(a["ally_code"]) or a["ally_code"],
            "discord_id": discord_by_ally.get(a["ally_code"]), "entries": [],
        })
        dc_texts = []
        dc_setting = (squads.get(a.get("squad_id")) or {}).get("datacron")
        if dc_setting and not dc_setting.get("required"):
            dc_texts.append(describe_recommendation(dc_setting, requirements, catalog, guild_id))
        elif a.get("datacron_id"):
            own = {str(d.get("id")): d for d in datacrons.get(a["ally_code"]) or []}
            for dc_id in a["datacron_id"].split(","):
                dc = own.get(dc_id)
                dc_texts.append(describe_datacron(dc, catalog) if dc else "ДК больше не найден у игрока — уточните у офицеров")
        row["entries"].append({
            "zone_key": a["zone"],
            "unit_ids": list(a["units"]),
            "zone": engine.ZONES_BY_KEY.get(a["zone"], {}).get("label", a["zone"]),
            "squad": a["squad_name"],
            "units": [unit_names.get(b, b) for b in a["units"]],
            "datacrons": dc_texts,
        })
    for row in rows.values():
        row["channels"] = tb_platoon_notify.channels(row["discord_id"])
    return sorted(rows.values(), key=lambda r: r["name"].lower())


def format_dm(guild_name: str, plan_name: str, player_rows: list[dict]) -> list[str]:
    """Текст для одного Discord-аккаунта, разбитый на сообщения до MESSAGE_LIMIT."""
    lines = [f"🛡️ Ваша защита на ВГ «{guild_name}» (план «{plan_name}»):", ""]
    multi = len(player_rows) > 1
    for row in player_rows:
        if multi:
            lines.append(f"**Аккаунт: {row['name']}**")
        for e in row["entries"]:
            lines.append(f"**{e['zone']} — {e['squad']}: {', '.join(e['units'])}**")
            multiple_dc = len(e["datacrons"]) > 1
            for index, dc in enumerate(e["datacrons"], 1):
                parts = re.split(r" — (?=[369]: )", dc, maxsplit=1)
                title, separator, bonuses = parts[0], len(parts) > 1, parts[-1]
                label = f"Датакрон {index}" if multiple_dc else "Датакрон"
                lines.append(f"{label}: {title}")
                if separator:
                    for bonus in re.split(r"; (?=[369]: )", bonuses):
                        level, _, text = bonus.partition(": ")
                        lines.append(f"• {level} ур.: {text}")
            lines.append("")
        lines.append("")
    lines.append("Ставьте отряды в указанные зоны в этом составе. Вопросы — офицерам.")
    chunks, cur = [], ""
    for line in lines:
        if len(cur) + len(line) + 1 > MESSAGE_LIMIT:
            chunks.append(cur.rstrip())
            cur = ""
        cur += line + "\n"
    if cur.strip():
        chunks.append(cur.rstrip())
    return chunks


async def send_maps(client, token: str, owner_id: str, maps: list[tuple[str, bytes]], channel: str) -> tuple[bool, str]:
    """Вложения Telegram хранятся в очереди, без публичных ссылок и временных файлов."""
    if channel == "tg":
        from tg_bot import outbox
        attachments = [{"filename": name, "content_type": "image/png",
                        "data_base64": base64.b64encode(data).decode("ascii")} for name, data in maps]
        ok = await asyncio.to_thread(outbox.enqueue_dm, owner_id, "", attachments)
        return ok, "карта в очереди" if ok else "Telegram: нет доступной лички"
    if not token or str(owner_id).startswith("tg:"):
        return False, "Discord: нет доступной лички"
    headers = {"Authorization": f"Bot {token}"}
    response = await client.post(f"{tb_platoon_notify.DISCORD_API_BASE}/users/@me/channels",
                                 headers=headers, json={"recipient_id": owner_id})
    if response.status_code != 200:
        return False, f"Discord не открыл личку ({response.status_code})"
    channel_id = response.json()["id"]
    # По одной карте: число альтов не ограничено лимитом вложений Discord.
    for name, data in maps:
        response = await client.post(f"{tb_platoon_notify.DISCORD_API_BASE}/channels/{channel_id}/messages",
            headers=headers, data={"payload_json": json.dumps({"allowed_mentions": {"parse": []}})},
            files={"files[0]": (name, data, "image/png")})
        if response.status_code not in (200, 201):
            return False, f"Discord не принял карту ({response.status_code})"
    return True, "карта отправлена"


async def send_broadcast(token: str, guild_id: int, plan: dict, catalog: dict | None, ally_codes: set[str], mode: str = "both") -> list[dict]:
    """Отчёт: [{ally_code, name, status: sent|partial|failed|skipped_no_discord, detail}]."""
    if mode not in DESTINATIONS:
        raise ValueError("Выберите Discord, Telegram или оба канала.")
    guild_name = (database.get_guild_config(guild_id) or {}).get("name") or "гильдия"
    rows = [r for r in build_player_rows(guild_id, plan, catalog) if r["ally_code"] in ally_codes]
    report, by_discord = [], {}
    for row in rows:
        if not row["discord_id"]:
            report.append({"ally_code": row["ally_code"], "name": row["name"], "status": "skipped_no_discord",
                           "detail": "нет регистрации"})
            continue
        by_discord.setdefault(row["discord_id"], []).append(row)
    async with httpx.AsyncClient(timeout=15.0) as client:
        for discord_id, player_rows in by_discord.items():
            from services import tw_def_image
            maps, map_errors = [], []
            for row in player_rows:
                if not row["entries"]:
                    continue
                try:
                    png = await asyncio.to_thread(tw_def_image.render, guild_name, plan["name"], row)
                    maps.append((f"tw-defense-{row['ally_code']}.png", png))
                except Exception:
                    map_errors.append(f"не удалось построить карту: {row['name']}")
            deliveries = []
            for channel in DESTINATIONS[mode]:
                ok, detail = True, "ok"
                map_ok, map_detail = not map_errors, "; ".join(map_errors)
                if maps:
                    try:
                        sent, info = await send_maps(client, token, discord_id, maps, channel)
                        map_ok = map_ok and sent
                        map_detail = "; ".join(filter(None, (map_detail, info)))
                    except Exception:
                        map_ok, map_detail = False, "ошибка доставки карты"
                for text in format_dm(guild_name, plan["name"], player_rows):
                    ok, detail = await tb_platoon_notify.send_dm(client, token, discord_id, text,
                        "discord_only" if channel == "discord" else "tg_only")
                    if not ok:
                        break
                label = "Discord" if channel == "discord" else "Telegram"
                deliveries.append((ok, f"{label}: " + (("отправлено" if channel == "discord" else "в очереди отправки") if ok else detail)))
                if not map_ok:
                    deliveries.append((False, f"{label}: {map_detail}"))
            count = sum(ok for ok, _ in deliveries)
            status = "sent" if count == len(deliveries) else "partial" if count else "failed"
            for row in player_rows:
                report.append({"ally_code": row["ally_code"], "name": row["name"],
                               "status": status, "detail": "; ".join(detail for _, detail in deliveries)})
    return report
