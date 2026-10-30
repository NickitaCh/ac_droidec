"""Рассылка деф-плана ВГ в личку Discord и/или Telegram (веб /tw/def/plans/<id>/notify) — по
образцу рассылки взводов ТБ (tb_platoon_notify.py, оттуда же отправка через REST). Каждому
игроку: зона, пак, какие персонажи ставить и какой именно его датакрон (сезон, уровень и
бонусы 3/6/9 — чтобы найти его в игре). Запрос офицеров 2026-10-20 («рассылка по ВГ, где
будут прописаны ДК из игры»).

Кому отправлять — решает офицер галочками; мейн и альт одного Discord получают одно
сообщение с разбивкой по аккаунтам."""

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


def build_player_rows(guild_id: int, plan: dict, catalog: dict | None) -> list[dict]:
    """[{ally_code, name, discord_id, entries: [{zone, squad, units: [имя], datacrons: [описание]}]}]."""
    assignments = database.get_tw_def_assignments(guild_id, plan["id"])
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
        if a.get("datacron_id"):
            own = {str(d.get("id")): d for d in datacrons.get(a["ally_code"]) or []}
            for dc_id in a["datacron_id"].split(","):
                dc = own.get(dc_id)
                dc_texts.append(describe_datacron(dc, catalog) if dc else "ДК больше не найден у игрока — уточните у офицеров")
        row["entries"].append({
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
            lines.append(f"— {row['name']} —")
        for e in row["entries"]:
            lines.append(f"**{e['zone']}** — {e['squad']}: {', '.join(e['units'])}")
            for dc in e["datacrons"]:
                lines.append(f"   Датакрон: {dc}")
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
            deliveries = []
            for channel in DESTINATIONS[mode]:
                ok, detail = True, "ok"
                for text in format_dm(guild_name, plan["name"], player_rows):
                    ok, detail = await tb_platoon_notify.send_dm(client, token, discord_id, text,
                        "discord_only" if channel == "discord" else "tg_only")
                    if not ok:
                        break
                label = "Discord" if channel == "discord" else "Telegram"
                deliveries.append((ok, f"{label}: " + (("отправлено" if channel == "discord" else "в очереди отправки") if ok else detail)))
            count = sum(ok for ok, _ in deliveries)
            status = "sent" if count == len(deliveries) else "partial" if count else "failed"
            for row in player_rows:
                report.append({"ally_code": row["ally_code"], "name": row["name"],
                               "status": status, "detail": "; ".join(detail for _, detail in deliveries)})
    return report
