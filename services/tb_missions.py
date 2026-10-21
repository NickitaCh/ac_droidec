"""Бои и спецмиссии «Восхода Империи» по игрокам — веб /tb/missions и отчёты /тб_отчет
(«Спецмиссии за ТБ», «Спецмиссии: % побед», «Бои за ТБ»).

Источник — сохранённая расшифровка каждой ТБ (tb_player_detail.zone_data_json, пишет
cogs/guild_events.py::_store_tb_history), ключи Comlink finalStat:
- спецмиссия (ОЗ): covert_round_attempted_mission_<zone> — волн начато, >0 значит игрок
  заходил (в ОЗ одна попытка: сумма заходов по миссиям = covert_attempt игрока, сверено
  2026-10-21 — 254 = 254); covert_complete_mission_<zone> — 1, если прошёл;
- бои (БЗ): только по планете целиком — strike_attempt_zone_<planet> (сколько боёв
  начато) и strike_encounter_zone_<planet> (сколько волн отбито). По отдельному бою
  Comlink не пишет, поэтому «отбил полностью» видно лишь у тех, кто сходил во все бои
  планеты: тогда недобитые волны = волн всего − отбито.

Число боёв и волн на планете — из игровых данных (strikeZoneDefinition → миссия кампании
t05D, волна = campaignNodeEncounter), их пишет units_sync в bot_state STRIKE_STATE_KEY.

«Мог, но не пошёл» считается только для последней ТБ и по текущему составу игрока: для
старых ТБ состав на тот момент неизвестен. Миссия считается открытой, если в неё
заходил хоть кто-то из гильдии."""

import json
import re

import database
from services import tb_covert
from services.tb_projection import BRANCHES

STRIKE_STATE_KEY = "tb_strike_zones"  # bot_state guild 0: {"<этап>/<ветка>": {...}} — пишет units_sync
_CONFLICT_RE = re.compile(r"phase0?(\d)_conflict(\d\d)(_bonus)?$")
_SIDE_BY_CONFLICT = {"01": "light", "02": "dark", "03": "mixed"}
_BONUS_PLANET = {"light": "Zeffo", "mixed": "Mandalore"}
_SIDE_ORDER = ["dark", "mixed", "light"]


def parse_strikes(tb_defs: list, campaigns: list) -> dict:
    """{"3/01_bonus": {"battles": 4, "fleet": 1, "waves": 7}} по планетам t05D."""
    tb = next((t for t in tb_defs if t.get("id") == tb_covert.TB_ID), None)
    camp = next((c for c in campaigns if c.get("id") == tb_covert.TB_ID), None)
    if not tb or not camp:
        return {}
    waves_by_mission = {}
    for cmap in camp.get("campaignMap") or []:
        for group in cmap.get("campaignNodeDifficultyGroup") or []:
            for node in group.get("campaignNode") or []:
                for mission in node.get("campaignNodeMission") or []:
                    waves_by_mission[mission.get("id")] = len(mission.get("campaignNodeEncounter") or []) or 1
    out = {}
    for zone in tb.get("strikeZoneDefinition") or []:
        zone_def = zone.get("zoneDefinition") or {}
        m = _CONFLICT_RE.search(zone_def.get("linkedConflictId") or "")
        if not m:
            continue
        key = f"{m.group(1)}/{m.group(2)}{m.group(3) or ''}"
        mission_id = (zone.get("campaignElementIdentifier") or {}).get("campaignMissionId")
        entry = out.setdefault(key, {"battles": 0, "fleet": 0, "waves": 0})
        entry["battles"] += 1
        entry["fleet"] += zone.get("combatType") == 2
        entry["waves"] += waves_by_mission.get(mission_id, 1)
    return out


def strike_zones() -> dict:
    raw = database.get_bot_state(STRIKE_STATE_KEY, guild_id=0)
    return json.loads(raw) if raw else {}


def zone_planet(phase: str, conflict_key: str) -> tuple[str, str]:
    """(англ. планета, «3 · Зеффо») по ключу расшифровки: этап "3", ветка "01_bonus"."""
    bonus = conflict_key.endswith("_bonus")
    side = _SIDE_BY_CONFLICT.get(conflict_key[:2])
    if side is None:
        return conflict_key, f"{phase} · {conflict_key}"
    planet = _BONUS_PLANET.get(side, conflict_key) if bonus else BRANCHES[side][int(phase) - 1]
    return planet, f"{phase} · {tb_covert.planet_ru(planet)}"


def _zone_sort_key(phase: str, conflict_key: str):
    side = _SIDE_BY_CONFLICT.get(conflict_key[:2], "")
    return (int(phase), conflict_key.endswith("_bonus"), _SIDE_ORDER.index(side) if side in _SIDE_ORDER else 9)


def _covert_zone_parts(zone_id: str):
    """tb3_mixed_phase03_conflict01_bonus_covert01 → ("3", "01_bonus", "covert01")."""
    m = re.search(r"phase0?(\d)_conflict(\d\d)(_bonus)?_(covert\d\d)$", zone_id)
    return (m.group(1), m.group(2) + (m.group(3) or ""), m.group(4)) if m else None


def _load(guild_id: int, event_ids: list) -> dict:
    """{event_id: {member_id: (имя, zone_data)}}."""
    out = {}
    for event_id, member_id, name, zone_json in database.get_tb_zone_data_for_events(event_ids):
        out.setdefault(event_id, {})[member_id] = (name, json.loads(zone_json))
    return out


def _covert_result(zone_data: dict, parts) -> str | None:
    """won / failed / None (не заходил)."""
    phase, conflict_key, covert = parts
    actions = zone_data.get(phase, {}).get(conflict_key, {})
    if actions.get("covert_complete", {}).get(covert, 0) > 0:
        return "won"
    if actions.get("covert_round_attempted", {}).get(covert, 0) > 0:
        return "failed"
    return None


def _ally_codes(guild_id: int) -> dict:
    return {pid: code for pid, (code, _name) in database.get_roster_by_player_id(guild_id).items()}


def _covert_mission_cols(ms: list) -> list:
    cols = []
    names = database.get_game_unit_names([b for m in ms for b in m["mandatory"]])
    for m in ms:
        parts = _covert_zone_parts(m["zone_id"])
        if not parts:
            continue
        lead = " + ".join(names.get(b, b) for b in m["mandatory"]) or "любые подходящие"
        same_planet = sum(1 for x in ms if x["planet"] == m["planet"]) > 1
        # На планете две миссии (Татуин) — различаем по первому обязательному персонажу.
        first = names.get(m["mandatory"][0], m["mandatory"][0]) if m["mandatory"] else lead
        cols.append({
            "mission": m, "parts": parts, "same_planet": same_planet,
            "label": f"{m['phase']} · {tb_covert.planet_ru(m['planet'])}" + (f" ({first})" if same_planet else ""),
            "lead": lead, "threshold": tb_covert.mission_label(m),
        })
    return cols


def covert_event(guild_id: int, event_id: int, with_skipped: bool) -> dict:
    """Таблица одной ТБ: {cols, players: [{name, member_id, cells: [won|failed|skipped|None],
    won, failed, skipped}], totals: [{won, failed, skipped}]}. with_skipped — отмечать «мог, но
    не пошёл» (только для последней ТБ)."""
    cols = _covert_mission_cols(tb_covert.missions())
    data = _load(guild_id, [event_id]).get(event_id, {})
    opened = [any(_covert_result(z, c["parts"]) for _n, z in data.values()) for c in cols]
    rosters, ships = {}, set()
    if with_skipped:
        codes = _ally_codes(guild_id)
        bulk = database.get_player_units_bulk([codes[pid] for pid in data if pid in codes])
        rosters = {pid: bulk[codes[pid]] for pid in data if pid in codes and bulk.get(codes[pid])}
        all_ids = {b for r in rosters.values() for b in r}
        ships = {b for b, t in database.get_unit_types(list(all_ids)).items() if t == "ship"}
    players = []
    for member_id, (name, zone_data) in data.items():
        cells = []
        for i, col in enumerate(cols):
            res = _covert_result(zone_data, col["parts"])
            if res is None and opened[i] and member_id in rosters:
                if tb_covert.player_status(col["mission"], rosters[member_id], ships)["status"] == "ready":
                    res = "skipped"
            cells.append(res)
        row = {"name": name, "member_id": member_id, "cells": cells,
               "won": cells.count("won"), "failed": cells.count("failed"), "skipped": cells.count("skipped")}
        if row["won"] or row["failed"] or row["skipped"]:
            players.append(row)
    players.sort(key=lambda p: (-p["failed"], -p["skipped"], -p["won"], p["name"].lower()))
    keep = [i for i, o in enumerate(opened) if o]
    cols = [cols[i] for i in keep]
    for p in players:
        p["cells"] = [p["cells"][i] for i in keep]
    totals = [{k: sum(1 for p in players if p["cells"][i] == k) for k in ("won", "failed", "skipped")}
              for i in range(len(cols))]
    return {"cols": cols, "players": players, "totals": totals, "with_skipped": with_skipped}


def covert_rates(guild_id: int, events: list) -> dict:
    """% побед в спецмиссиях по всем сохранённым ТБ. events — [(event_id, completed_at)]
    от старых к новым. {events, players: [{name, attempts, won, pct, per_event: [(won,
    attempts)|None], fails: [(подпись миссии, сколько раз)]}], missions: [{label, attempts, won, pct}]}."""
    event_ids = [e[0] for e in events]
    data = _load(guild_id, event_ids)
    cols = _covert_mission_cols(tb_covert.missions())
    by_member, mission_stats = {}, [{"attempts": 0, "won": 0} for _ in cols]
    for idx, event_id in enumerate(event_ids):
        for member_id, (name, zone_data) in data.get(event_id, {}).items():
            p = by_member.setdefault(member_id, {"name": name, "attempts": 0, "won": 0,
                                                 "per_event": [None] * len(event_ids), "fails": {}})
            p["name"] = name  # последнее известное имя
            won = attempts = 0
            for i, col in enumerate(cols):
                res = _covert_result(zone_data, col["parts"])
                if res is None:
                    continue
                attempts += 1
                mission_stats[i]["attempts"] += 1
                if res == "won":
                    won += 1
                    mission_stats[i]["won"] += 1
                else:
                    p["fails"][col["label"]] = p["fails"].get(col["label"], 0) + 1
            if attempts:
                p["per_event"][idx] = (won, attempts)
                p["attempts"] += attempts
                p["won"] += won
    roster = database.get_roster_by_player_id(guild_id)
    roster_names = database.get_roster_name_set(guild_id)
    players = []
    for member_id, p in by_member.items():
        if not p["attempts"]:
            continue
        if roster_names and member_id not in roster and p["name"].strip().lower() not in roster_names:
            continue  # выбывшие — не показываем, как в сравнении ТБ
        p["pct"] = p["won"] / p["attempts"] * 100
        p["failed"] = p["attempts"] - p["won"]
        p["fails"] = sorted(p["fails"].items(), key=lambda x: -x[1])
        players.append(p)
    players.sort(key=lambda p: (p["pct"], -p["attempts"], p["name"].lower()))
    missions = [{"label": col["label"], "threshold": col["threshold"], **st,
                 "pct": st["won"] / st["attempts"] * 100 if st["attempts"] else None}
                for col, st in zip(cols, mission_stats)]
    return {"events": events, "players": players, "missions": missions}


def strikes_event(guild_id: int, event_id: int) -> dict:
    """Бои одной ТБ: {zones: [{key, label, battles, waves}], players: [{name, cells: [{attempts,
    waves, full, lost}|None], attempts, waves, lost}]}. lost — недобитые волны, известны только
    при заходе во все бои планеты (иначе None)."""
    meta = strike_zones()
    data = _load(guild_id, [event_id]).get(event_id, {})
    zone_keys = set()
    for _name, zone_data in data.values():
        for phase, conflicts in zone_data.items():
            for conflict_key, actions in conflicts.items():
                if actions.get("strike_attempt", {}).get("_", 0) > 0:
                    zone_keys.add((phase, conflict_key))
    zones = []
    for phase, conflict_key in sorted(zone_keys, key=lambda k: _zone_sort_key(*k)):
        m = meta.get(f"{phase}/{conflict_key}") or {}
        zones.append({"key": (phase, conflict_key), "label": zone_planet(phase, conflict_key)[1],
                      "battles": m.get("battles"), "waves": m.get("waves")})
    players = []
    for member_id, (name, zone_data) in data.items():
        cells, total_a, total_w, total_lost = [], 0, 0, 0
        for z in zones:
            phase, conflict_key = z["key"]
            actions = zone_data.get(phase, {}).get(conflict_key, {})
            a = actions.get("strike_attempt", {}).get("_", 0)
            w = actions.get("strike_encounter", {}).get("_", 0)
            if not a:
                cells.append(None)
                continue
            lost = max(0, z["waves"] - w) if z["battles"] and z["waves"] and a >= z["battles"] else None
            cells.append({"attempts": a, "waves": w, "lost": lost, "full": lost == 0})
            total_a += a
            total_w += w
            total_lost += lost or 0
        if total_a:
            players.append({"name": name, "member_id": member_id, "cells": cells,
                            "attempts": total_a, "waves": total_w, "lost": total_lost})
    players.sort(key=lambda p: (-p["lost"], p["attempts"], p["name"].lower()))
    return {"zones": zones, "players": players}


# ---------------- Текстовые отчёты для /тб_отчет ----------------

COVERT_MARK = {"won": "✅", "failed": "❌", "skipped": "💤", None: "·"}


def covert_event_text(r: dict, title: str) -> str:
    lines = [title, ""]
    if not r["cols"]:
        return title + "\n\nВ эту ТБ в спецмиссии никто не заходил."
    lines.append("✅ прошёл · ❌ зашёл и проиграл" + (" · 💤 мог (по текущему составу), но не пошёл" if r["with_skipped"] else "")
                 + " · «·» — не заходил")
    lines.append("")
    for i, col in enumerate(r["cols"]):
        t = r["totals"][i]
        extra = f", не пошли {t['skipped']}" if r["with_skipped"] else ""
        lead = "" if col["same_planet"] and len(col["mission"]["mandatory"]) == 1 else f" — {col['lead']}"
        lines.append(f"{i + 1}. {col['label']}{lead} {col['threshold']}: прошли {t['won']}, проиграли {t['failed']}{extra}")
    lines.append("")
    width = max(len(p["name"]) for p in r["players"]) if r["players"] else 10
    header = " " * width + "  " + " ".join(f"{i + 1:>2}" for i in range(len(r["cols"])))
    lines.append(header)
    for p in r["players"]:
        lines.append(f"{p['name']:<{width}}  " + " ".join(f"{COVERT_MARK[c]:>2}" for c in p["cells"]))
    return "\n".join(lines)


def covert_rates_text(r: dict, title: str) -> str:
    lines = [title, "", "Игроки — сначала с худшим % побед. По ТБ: побед/заходов, от старых к новым.", ""]
    width = max((len(p["name"]) for p in r["players"]), default=10)
    for p in r["players"]:
        per = " ".join(f"{x[0]}/{x[1]}" if x else "—" for x in p["per_event"])
        lines.append(f"{p['name']:<{width}}  {p['pct']:5.1f}%  {p['won']}/{p['attempts']}   [{per}]")
        if p["fails"]:
            lines.append(" " * (width + 2) + "проигрыши: " + "; ".join(f"{label} ×{n}" if n > 1 else label for label, n in p["fails"]))
    lines += ["", "По миссиям:"]
    for m in r["missions"]:
        if m["attempts"]:
            lines.append(f"  {m['label']} ({m['threshold']}): {m['won']}/{m['attempts']} — {m['pct']:.0f}%")
    return "\n".join(lines)


def strikes_event_text(r: dict, title: str) -> str:
    if not r["zones"]:
        return title + "\n\nВ эту ТБ в бои никто не заходил."
    lines = [title, "", "В клетке: боёв/волн отбито. ✅ — все бои планеты, все волны; −N — недобито волн "
             "(видно только при заходе во все бои планеты: по одному бою Comlink данных не даёт).", ""]
    for i, z in enumerate(r["zones"]):
        extra = f" — {z['battles']} боёв, {z['waves']} волн" if z["battles"] else ""
        lines.append(f"{i + 1}. {z['label']}{extra}")
    lines.append("")
    width = max(len(p["name"]) for p in r["players"])
    lines.append(" " * width + "  " + " ".join(f"{i + 1:>7}" for i in range(len(r["zones"]))) + "   итого")
    for p in r["players"]:
        cells = []
        for c in p["cells"]:
            if c is None:
                cells.append(f"{'·':>7}")
            else:
                mark = "✅" if c["full"] else (f"−{c['lost']}" if c["lost"] else "")
                cells.append(f"{c['attempts']}/{c['waves']}{mark:>3}".rjust(7))
        lost = f", недобито {p['lost']}" if p["lost"] else ""
        lines.append(f"{p['name']:<{width}}  " + " ".join(cells) + f"   {p['attempts']} боёв, {p['waves']} волн{lost}")
    return "\n".join(lines)
