"""One current channel panel per platform, guild, and game event."""
import asyncio
from contextlib import closing
from datetime import datetime
from html import escape
import json
import logging
import secrets
import sqlite3
import time
from zoneinfo import ZoneInfo

import httpx

import database
from services import bot_profiles, discord_rest, tb_forecast, tb_live
import tb_plan_reader

LABELS = {"tb": "ТБ", "tw": "ВГ", "raid": "Рейд"}
PROFILE = "test"
SITE = "https://swgoh-sng.ru"
BUTTONS = {
    "tb": (("Обновить", "refresh"), ("Наблюдение", "watch"), ("Бои и ОЗ", "details"), ("Мои взводы", "mine")),
    "tw": (("Обновить", "refresh"), ("Наблюдение", "watch"), ("Зоны", "details"), ("Моя защита", "mine")),
    "raid": (("Обновить", "refresh"), ("Наблюдение", "watch"), ("По игрокам", "details")),
}
_MSK = ZoneInfo("Europe/Moscow")
_LOG = logging.getLogger(__name__)


def allowed(guild_id, tier, profile):
    return guild_id == 1 and tier == "officer" and profile == PROFILE and bot_profiles.accepts(guild_id, profile)


def _connect():
    db = sqlite3.connect(database.DB_NAME)
    db.row_factory = sqlite3.Row
    db.execute("""CREATE TABLE IF NOT EXISTS game_event_panels (
        platform TEXT NOT NULL, guild_id INTEGER NOT NULL, kind TEXT NOT NULL,
        next_revision INTEGER NOT NULL, revision INTEGER NOT NULL DEFAULT 0,
        token TEXT, channel TEXT, message TEXT,
        PRIMARY KEY (platform, guild_id, kind))""")
    db.execute("""CREATE TABLE IF NOT EXISTS game_event_panel_links (
        ticket TEXT PRIMARY KEY, platform TEXT NOT NULL, guild_id INTEGER NOT NULL,
        kind TEXT NOT NULL, panel_token TEXT NOT NULL, owner TEXT NOT NULL,
        action TEXT NOT NULL, expires INTEGER NOT NULL)""")
    return db


def reserve(platform, guild_id, kind):
    with closing(_connect()) as db, db:
        scope = (platform, guild_id, kind)
        db.execute("""INSERT INTO game_event_panels (platform,guild_id,kind,next_revision)
            VALUES (?,?,?,1) ON CONFLICT(platform,guild_id,kind)
            DO UPDATE SET next_revision=next_revision+1""", scope)
        serial = db.execute(
            "SELECT next_revision FROM game_event_panels WHERE platform=? AND guild_id=? AND kind=?", scope,
        ).fetchone()[0]
    return serial, secrets.token_urlsafe(9)


def activate(platform, guild_id, kind, serial, token, channel, message):
    with closing(_connect()) as db, db:
        scope = (platform, guild_id, kind)
        db.execute("BEGIN IMMEDIATE")
        previous = db.execute(
            "SELECT * FROM game_event_panels WHERE platform=? AND guild_id=? AND kind=?", scope,
        ).fetchone()
        if not previous or previous["revision"] >= serial or previous["next_revision"] < serial:
            return False, None
        db.execute("""UPDATE game_event_panels SET revision=?,token=?,channel=?,message=?
            WHERE platform=? AND guild_id=? AND kind=?""",
            (serial, token, str(channel), str(message), *scope))
    return True, dict(previous) if previous["message"] else None


def current(platform, guild_id, kind):
    with closing(_connect()) as db:
        row = db.execute(
            "SELECT * FROM game_event_panels WHERE platform=? AND guild_id=? AND kind=? AND token IS NOT NULL",
            (platform, guild_id, kind),
        ).fetchone()
    return dict(row) if row else None


def handoff(platform, guild_id, kind, token, channel, message, owner, action):
    """Bind a channel button to the account that pressed it."""
    if kind not in BUTTONS or action not in {value for _, value in BUTTONS[kind]}:
        return None
    ticket = secrets.token_urlsafe(18)
    now = int(time.time())
    with closing(_connect()) as db, db:
        db.execute("DELETE FROM game_event_panel_links WHERE expires<=?", (now,))
        inserted = db.execute("""INSERT INTO game_event_panel_links
            (ticket,platform,guild_id,kind,panel_token,owner,action,expires)
            SELECT ?,platform,guild_id,kind,token,?,?,? FROM game_event_panels
            WHERE platform=? AND guild_id=? AND kind=? AND token=? AND channel=? AND message=?""",
            (ticket, str(owner), action, now + 900, platform, guild_id, kind, token,
             str(channel), str(message)),
        ).rowcount
    return f"{SITE}/game-account/panel/{ticket}" if inserted else None


def panel_link(ticket, guild_id, owner):
    with closing(_connect()) as db, db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("""SELECT link.kind,link.action FROM game_event_panel_links link
            JOIN game_event_panels panel ON panel.platform=link.platform AND panel.guild_id=link.guild_id
                 AND panel.kind=link.kind AND panel.token=link.panel_token
            WHERE link.ticket=? AND link.guild_id=? AND link.owner=? AND link.expires>?""",
            (ticket, guild_id, str(owner), int(time.time())),
        ).fetchone()
        if not row:
            return None
        db.execute("DELETE FROM game_event_panel_links WHERE ticket=?", (ticket,))
    return destination(row["kind"], row["action"], guild_id)


def destination(kind, action, guild_id):
    if kind not in BUTTONS or action not in {value for _, value in BUTTONS[kind]}:
        return None
    path = "/tb/live" if kind == "tb" else f"/game-account/events/{kind}"
    if kind == "tw" and action == "mine":
        plan_id = database.get_bot_state("tw_active_plan", guild_id=guild_id) or ""
        if plan_id.isascii() and plan_id.isdecimal():
            path += f"?plan={int(plan_id)}"
    fragment = {"refresh": "tb-live-refresh", "watch": "tb-watch-controls", "mine": "personal-preview"}.get(action)
    return SITE + path + (f"#{fragment}" if fragment else "")


def telegram_keyboard(kind, token):
    buttons = [{"text": label, "callback_data": f"glp|{kind}:{token}:{action}"}
               for label, action in BUTTONS[kind]]
    return [buttons[i:i+2] for i in range(0, len(buttons), 2)]


def _score(value):
    return f"{value:,}".replace(",", " ") if isinstance(value, int) else "—"


def _clock(epoch):
    if epoch >= 100_000_000_000:
        epoch /= 1000
    return datetime.fromtimestamp(epoch, _MSK).strftime("%d.%m %H:%M")


def summary(kind, data, captured):
    if not data or (kind == "raid" and not data["active"]):
        return f"**{LABELS[kind]} · сейчас**\nИгра не вернула активное событие. Снимок {_clock(captured)} МСК."
    lines = []
    if kind == "tb":
        lines.append(f"**ТБ · этап {data['phase']} · до {_clock(data['end_ms'])} МСК**")
        planets = tb_forecast.zones().get("planets", {})
        for key, zone in data["zones"].items():
            if zone["status"] != tb_live.OPEN_ZONE:
                continue
            where = tb_forecast.planet_of(key) if key.startswith("tb3_mixed_") else None
            name = tb_plan_reader.PLANET_NAME_TO_RU.get(where[1], where[1]).capitalize() if where else "Зона"
            score = zone["points"]
            stars = planets.get(where[1], {}).get("stars", []) if where else []
            next_star = min((threshold - score for threshold in stars if score is not None and threshold > score), default=None)
            operations = [op["slots"] for op in zone["operations"] if op["slots"]]
            complete = sum(all(slot["donor"] for slot in slots) for slots in operations)
            missing = sum(not slot["donor"] for slots in operations for slot in slots)
            lines.append(f"**{name}** · {_score(score)} очков")
            if next_star is not None:
                lines.append(f"До следующей ★ {_score(next_star)} очков")
            if operations:
                lines.append(f"Взводы {complete} из {len(operations)} · не хватает {missing} юнитов")
    elif kind == "tw":
        phase = "расстановка защиты" if data["round"] == 0 else f"раунд {data['round']}"
        lines.append(f"**ВГ · {phase} · до {_clock(data['end'])} МСК**")
        for label, side in (("Мы", "own"), ("Соперник", "opponent")):
            zones = data[side]
            if zones:
                capacities = [zone["capacity"] for zone in zones]
                filled = [zone["deployed"] for zone in zones]
                beaten = [zone["defeated"] for zone in zones]
                slots = _score(sum(capacities)) if None not in capacities else "—"
                deployed = _score(sum(filled)) if None not in filled else "—"
                line = f"{label} · слоты защиты: {deployed} из {slots}"
                if data["round"] != 0 and None not in beaten:
                    line += f" · разгромлено в защите: {_score(sum(beaten))}"
                lines.append(line)
    else:
        raid = data["active"][0]
        expires = f" · до {_clock(raid['end'])} МСК" if raid.get("end") else ""
        lines.append(f"**Рейд{expires}**")
        lines.append(f"Очки гильдии {_score(raid.get('points'))} · игроков в снимке {len(raid['players'])}")
    lines.append(f"Снимок {_clock(captured)} МСК")
    return "\n".join(lines)[:1900]


def telegram_text(text):
    return "\n".join("<b>" + escape(line[2:-2]) + "</b>" if line.startswith("**") and line.endswith("**")
                     else escape(line.replace("**", "")) for line in text.splitlines())


def latest(guild_id, kind):
    return database.get_bot_state(f"event_panel_text:{kind}", guild_id=guild_id) or (
        f"**{LABELS[kind]} · сейчас**\nСнимка пока нет. Нажмите «Обновить» или «Наблюдение»."
    )


def personal_tw_lines(guild_id, ally_code, rows, snapshot):
    from disnake.utils import escape_markdown
    from services import game_event_live, tw_live_image
    from tw_def_engine import ZONES_BY_KEY

    state = snapshot.get("data") or {}
    player_ids = {pid for pid, (code, _) in database.get_roster_by_player_id(guild_id).items()
                  if str(code) == str(ally_code)}
    zones = state.get("own", [])
    captured = snapshot.get("at")
    valid = bool(zones and player_ids and captured and state.get("end")
                 and game_event_live.epoch(state["end"]) > time.time())
    complete = valid and all(zone.get("deployed") is not None and
                            len(zone.get("teams", [])) >= zone["deployed"] for zone in zones)
    teams = [(zone, team) for zone in zones for team in zone.get("teams", [])
             if team.get("player_id") in player_ids]
    names = database.get_game_unit_names(sorted({u for row in rows for u in row["units"] if u}))
    groups = {"✅ Уже выставлено": [], "⬜ Ещё поставить": [], "❔ Статус не подтверждён": []}
    for row in rows:
        units = [u for u in row["units"] if u]
        actual = next((zone for zone, team in teams if units and team.get("units")
                       and units[0] == team["units"][0] and set(units) == set(team["units"])), None) if valid else None
        key = "✅ Уже выставлено" if actual is not None else ("⬜ Ещё поставить" if complete else "❔ Статус не подтверждён")
        zone_key = tw_live_image.zone_key(actual["id"]) if actual is not None else row["zone"]
        label = ZONES_BY_KEY.get(zone_key, {}).get("label", zone_key or row["zone"])
        name = row.get("name") or (names.get(units[0], units[0]) if units else label)
        title = f"**{escape_markdown(label)} · {escape_markdown(name)}**"
        detail = " · ".join(escape_markdown(names.get(u) or u) for u in units)
        if row.get("recommendation"):
            detail += "\nРекомендованный ДК: " + escape_markdown(row["recommendation"])
        groups[key].append(title + "\n" + detail)
    lines = [f"По снимку {_clock(captured)} МСК. Обновите сводку после выставления." if valid else
             "Нет актуального снимка ВГ для проверки. Нажмите «Обновить» на сводке."]
    for label, entries in groups.items():
        if entries or (complete and label != "❔ Статус не подтверждён"):
            lines += [f"\n**{label} · {len(entries)}**", *entries]
    return lines


def discord_details(guild_id, owner, ally_code, kind, action):
    from services import game_event_live, tb_live_order

    if kind == "tw" and action == "mine":
        raw = database.get_bot_state(f"tw_panel_preview:{ally_code}", guild_id=guild_id)
        preview = json.loads(raw) if raw else None
        if preview and preview.get("owner") == str(owner) and preview.get("expires", 0) > time.time():
            panel = current("discord", guild_id, "tw")
            snapshot = game_event_live.snapshot(guild_id, owner, "tw")
            state = snapshot.get("data")
            if (panel and panel["token"] == preview.get("panel_token") and state
                    and state.get("event_id") == preview.get("event_id")):
                if preview.get("assignments"):
                    return personal_tw_lines(guild_id, ally_code, preview["assignments"], snapshot)
                return ["❔ Статус назначений не подтверждён: у этого старого предпросмотра нет составов для сверки.", *preview["lines"]]
        plan_id = database.get_bot_state("tw_active_plan", guild_id=guild_id) or ""
        plan = database.get_tw_def_plan(guild_id, int(plan_id)) if plan_id.isdecimal() else None
        if not plan:
            return ["Активного плана защиты ВГ нет."]
        rows = [row for row in database.get_tw_def_assignments(guild_id, int(plan_id))
                if str(row["ally_code"]) == ally_code]
        return personal_tw_lines(guild_id, ally_code, rows, game_event_live.snapshot(guild_id, owner, "tw")) if rows else ["В активном плане ВГ у вас нет назначений."]
    if kind == "tb":
        recent = tb_live.history(guild_id, owner, 1)
        state = recent[0][1] if recent else None
        if not state:
            return ["Снимка ТБ нет. Нажмите «Обновить» на сводке."]
        if action == "mine":
            owned = database.get_player_units(ally_code)
            rows = tb_live_order.personal_preview(state, ally_code, set(owned) if owned else None)
            names = database.get_game_unit_names(sorted({row["unit"] for row in rows}))
            return [
                f"{tb_plan_reader.PLANET_NAME_TO_RU.get(row['planet'], row['planet']).capitalize()}"
                f" · взвод {row['operation']} · {names.get(row['unit']) or row['unit']}" for row in rows
            ] or ["В текущем этапе ТБ для вас нет назначений во взводы."]
        lines = []
        for key, zone in state["zones"].items():
            if zone["status"] != tb_live.OPEN_ZONE:
                continue
            planet = tb_forecast.planet_of(key) if key.startswith("tb3_mixed_") else None
            label = tb_plan_reader.PLANET_NAME_TO_RU.get(planet[1], planet[1]).capitalize() if planet else key
            lines.append(f"**{label}**")
            lines.extend(f"Бой {index} · игроков: {_score(row['players'])}"
                         for index, row in enumerate(zone["missions"], 1))
            lines.extend(f"ОЗ {index} · игроков: {_score(row['players'])} · побед: {_score(row['wins'])}"
                         for index, row in enumerate(zone.get("special", ()), 1))
        return lines or ["Игра не передала данные о боях открытых зон."]
    state = game_event_live.snapshot(guild_id, owner, kind).get("data")
    if not state:
        return ["Снимка нет. Нажмите «Обновить» на сводке."]
    if kind == "tw":
        from services import tw_live_image
        from tw_def_engine import ZONES, ZONES_BY_KEY
        from disnake.utils import escape_markdown

        lines = []
        order = {entry["key"]: index for index, entry in enumerate(ZONES)}
        zones = sorted((zone for zone in state.get("opponent", ()) if zone.get("status") == tb_live.OPEN_ZONE),
                       key=lambda zone: order.get(tw_live_image.zone_key(zone["id"]), 99))
        names = database.get_game_unit_names(sorted({unit for zone in zones
                    for team in zone.get("teams", ()) for unit in team.get("units", ())}))
        for zone in zones:
            key = tw_live_image.zone_key(zone["id"])
            name = ZONES_BY_KEY[key]["label"] if key else zone["id"]
            lines.append(f"\n**{escape_markdown(name)}**")
            groups = {}
            for team in zone.get("teams", ()):
                units = team.get("units", ())
                if not units:
                    continue
                # Leader matters; the order of the remaining members does not.
                composition = (units[0], *sorted(units[1:]))
                groups.setdefault(composition, []).append(team)
            for units, teams in groups.items():
                players = ", ".join(escape_markdown(team.get("player_name") or "Имя не раскрыто") for team in teams)
                squad = " · ".join(escape_markdown(names.get(unit) or unit) for unit in units)
                lines.append(f"{players} — {squad}")
            if not groups:
                lines.append("Составы пока не раскрыты." if zone.get("deployed") else "Нет отрядов.")
        return lines or ["Открытых зон соперника пока нет."]
    if not state["active"]:
        return ["Активного рейда нет."]
    roster = database.get_roster_by_player_id(guild_id)
    return [f"{roster.get(player['id'], ('', 'Игрок вне ростера'))[1]} · {_score(player['points'])}"
            f" очков · попыток: {_score(player['attempts'])}"
            for player in sorted(state["active"][0]["players"], key=lambda row: row["points"] or 0, reverse=True)
            ] or ["В снимке рейда пока нет результатов по игрокам."]


async def set_refreshing(panel, active):
    """Keep the existing panel and details; disable repeated game reads in its buttons."""
    if not panel or panel["platform"] != "discord":
        return
    url = f"{discord_rest.DISCORD_API}/channels/{panel['channel']}/messages/{panel['message']}"
    try:
        async with httpx.AsyncClient(timeout=8) as client:
            response = await client.get(url, headers=discord_rest._headers(PROFILE))
            response.raise_for_status()
            components = response.json().get("components", [])
            for row in components:
                for item in row.get("components", []):
                    parts = item.get("custom_id", "").split(":")
                    if len(parts) == 4 and parts[:3] == ["glp", panel["kind"], panel["token"]]:
                        if parts[3] in ("refresh", "watch"):
                            item["disabled"] = active
                        if parts[3] == "refresh":
                            item["label"] = "⏳ Обновляю…" if active else "Обновить"
            response = await client.patch(url, headers=discord_rest._headers(PROFILE),
                                          json={"components": components, "allowed_mentions": {"parse": []}})
            response.raise_for_status()
    except Exception as error:
        _LOG.warning("Event panel progress update failed (%s)", type(error).__name__)


async def edit(panel, text=None, *, retire=False, tw_board=None, replace_board=False):
    """Update a message already published by its bot."""
    if not panel:
        return
    platform = panel["platform"]
    token = bot_profiles.token(platform, PROFILE)
    if not token:
        return
    channel, message = panel["channel"], panel["message"]
    try:
        if platform == "discord":
            payload = {"allowed_mentions": {"parse": []}}
            if retire:
                payload["components"] = []
            if text is not None:
                payload["content"] = text
            async with httpx.AsyncClient(timeout=8) as client:
                url = f"{discord_rest.DISCORD_API}/channels/{channel}/messages/{message}"
                headers = discord_rest._headers(PROFILE)
                if tw_board:
                    payload["attachments"] = [{"id": 0, "filename": "tw-map.png"}]
                    response = await client.patch(
                        url, headers=headers, data={"payload_json": json.dumps(payload)},
                        files={"files[0]": ("tw-map.png", tw_board, "image/png")},
                    )
                else:
                    if replace_board:
                        payload["attachments"] = []
                    response = await client.patch(url, headers=headers, json=payload)
                if response.status_code >= 400:
                    _LOG.warning("Event panel Discord edit returned %s", response.status_code)
        else:
            from tg_bot.api import TelegramAPI
            api = TelegramAPI(token, timeout=8)
            try:
                if retire:
                    await api.call("editMessageReplyMarkup", chat_id=int(channel), message_id=int(message),
                                   reply_markup={"inline_keyboard": []})
                elif text is not None:
                    await api.edit_message_text(
                        int(channel), int(message), telegram_text(text), telegram_keyboard(panel["kind"], panel["token"]),
                    )
            finally:
                await api.close()
    except Exception as error:
        # Never log Telegram URLs: they contain the bot token.
        if "message is not modified" not in getattr(error, "description", "").lower():
            _LOG.warning("Event panel edit failed on %s (%s)", platform, type(error).__name__)

async def publish(guild_id, kind, data, captured):
    """Save a public summary and update existing panels without posting a new message."""
    if not allowed(guild_id, "officer", PROFILE):
        return
    text = await asyncio.to_thread(summary, kind, data, captured)
    await asyncio.to_thread(database.set_bot_state, f"event_panel_text:{kind}", text, guild_id=guild_id)
    panels = await asyncio.gather(*(
        asyncio.to_thread(current, platform, guild_id, kind) for platform in ("discord", "telegram")
    ))
    updates = []
    for panel in panels:
        if not panel:
            continue
        if kind == "tw" and panel["platform"] == "discord":
            board = await asyncio.to_thread(tw_board, data)
            updates.append(edit(panel, text, tw_board=board, replace_board=True))
        else:
            updates.append(edit(panel, text))
    await asyncio.gather(*updates)


def tw_board(data):
    if data:
        from services import tw_live_image

        try:
            return tw_live_image.render(data)
        except Exception as error:
            _LOG.warning("Territory War board render failed (%s)", type(error).__name__)
    return None
