"""Persistent TW workspace, plan history and deterministic defense proposals.

Uses the existing defense tables; old plans need no conversion. No network calls.
"""
import copy
import hashlib
import json
import sqlite3
import time
import uuid
from collections import Counter
from contextlib import contextmanager

import database
import tw_def_engine as engine

PHASES = {"preparation": "Подготовка", "defense": "Расстановка защиты", "attack": "Атака", "finished": "Завершена"}


@contextmanager
def connect():
    conn = sqlite3.connect(database.DB_NAME, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS tw_workspaces (
            guild_id INTEGER NOT NULL, plan_id INTEGER NOT NULL, data TEXT NOT NULL,
            PRIMARY KEY (guild_id, plan_id));
        CREATE TABLE IF NOT EXISTS tw_plan_versions (
            id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER NOT NULL, plan_id INTEGER NOT NULL,
            fingerprint TEXT NOT NULL, state TEXT NOT NULL, actor TEXT NOT NULL, action TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT (datetime('now')));
        CREATE INDEX IF NOT EXISTS idx_tw_versions ON tw_plan_versions(guild_id, plan_id, id);
        CREATE TABLE IF NOT EXISTS tw_plan_leases (
            guild_id INTEGER NOT NULL, plan_id INTEGER NOT NULL, token TEXT NOT NULL, expires REAL NOT NULL,
            PRIMARY KEY (guild_id, plan_id));
        CREATE TABLE IF NOT EXISTS tw_plan_proposals (
            token TEXT PRIMARY KEY, guild_id INTEGER NOT NULL, plan_id INTEGER NOT NULL,
            revision TEXT NOT NULL, roster_hash TEXT NOT NULL, data TEXT NOT NULL, created REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS tw_strategy_templates (
            id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER NOT NULL, name TEXT NOT NULL,
            data TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS tw_plan_deliveries (
            guild_id INTEGER NOT NULL, plan_id INTEGER NOT NULL, ally_code TEXT NOT NULL,
            channel TEXT NOT NULL, fingerprint TEXT NOT NULL, status TEXT NOT NULL, detail TEXT NOT NULL,
            updated_at TEXT NOT NULL DEFAULT (datetime('now')),
            PRIMARY KEY (guild_id, plan_id, ally_code, channel));
    """)
    try:
        with conn:
            yield conn
    finally:
        conn.close()


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()


def workspace(guild_id, plan_id):
    with connect() as conn:
        row = conn.execute("SELECT data FROM tw_workspaces WHERE guild_id=? AND plan_id=?", (guild_id, plan_id)).fetchone()
    defaults = {"opponent": "", "opponent_name": "", "phase": "preparation", "result_start": "",
                "targets": [], "reserves": [], "orders": []}
    if row:
        defaults.update(json.loads(row[0]))
    return defaults


def save_workspace(guild_id, plan_id, data):
    if not database.get_tw_def_plan(guild_id, plan_id):
        raise ValueError("План не найден")
    with connect() as conn:
        conn.execute("INSERT OR REPLACE INTO tw_workspaces VALUES (?, ?, ?)",
                     (guild_id, plan_id, json.dumps(data, ensure_ascii=False)))


def state(guild_id, plan_id):
    return {"plan": database.get_tw_def_plan(guild_id, plan_id),
            "assignments": database.get_tw_def_assignments(guild_id, plan_id),
            "workspace": workspace(guild_id, plan_id), "library": database.list_tw_def_squads(guild_id)}


def revision(guild_id, plan_id):
    return digest(state(guild_id, plan_id))


def acquire(guild_id, plan_id):
    token = uuid.uuid4().hex
    with connect() as conn:
        conn.execute("DELETE FROM tw_plan_leases WHERE expires < ?", (time.time(),))
        try:
            conn.execute("INSERT INTO tw_plan_leases VALUES (?, ?, ?, ?)", (guild_id, plan_id, token, time.time() + 600))
        except sqlite3.IntegrityError:
            raise ValueError("План сейчас изменяет другой офицер. Повторите действие через несколько секунд.")
    return token


def release(guild_id, plan_id, token):
    with connect() as conn:
        conn.execute("DELETE FROM tw_plan_leases WHERE guild_id=? AND plan_id=? AND token=?", (guild_id, plan_id, token))


def remember(guild_id, plan_id, snapshot, actor, action):
    fingerprint = digest(snapshot)
    with connect() as conn:
        last = conn.execute("SELECT fingerprint FROM tw_plan_versions WHERE guild_id=? AND plan_id=? ORDER BY id DESC LIMIT 1",
                            (guild_id, plan_id)).fetchone()
        if last and last[0] == fingerprint:
            return
        conn.execute("INSERT INTO tw_plan_versions (guild_id, plan_id, fingerprint, state, actor, action) VALUES (?, ?, ?, ?, ?, ?)",
                     (guild_id, plan_id, fingerprint, json.dumps(snapshot, ensure_ascii=False), actor, action))
        conn.execute("DELETE FROM tw_plan_versions WHERE guild_id=? AND plan_id=? AND id NOT IN "
                     "(SELECT id FROM tw_plan_versions WHERE guild_id=? AND plan_id=? ORDER BY id DESC LIMIT 30)",
                     (guild_id, plan_id, guild_id, plan_id))


def versions(guild_id, plan_id):
    with connect() as conn:
        return [dict(r) for r in conn.execute("SELECT id, actor, action, created_at FROM tw_plan_versions "
                "WHERE guild_id=? AND plan_id=? ORDER BY id DESC LIMIT 30", (guild_id, plan_id))]


def restore(guild_id, plan_id, version_id):
    """Restore plan, assignments and workspace together, retaining delivery history."""
    with connect() as conn:
        row = conn.execute("SELECT state FROM tw_plan_versions WHERE guild_id=? AND plan_id=? AND id=?",
                           (guild_id, plan_id, version_id)).fetchone()
        if not row:
            raise ValueError("Версия не найдена")
        snapshot = json.loads(row[0])
        p = snapshot["plan"]
        conn.execute("UPDATE tw_def_plans SET name=?, squads_per_zone=?, fleets_per_zone=?, excluded_json=?, note=?, "
                     "updated_at=datetime('now') WHERE guild_id=? AND id=?",
                     (p["name"], p["squads_per_zone"], p["fleets_per_zone"], json.dumps(p["excluded"]), p["note"], guild_id, plan_id))
        conn.execute("DELETE FROM tw_def_assignments WHERE guild_id=? AND plan_id=?", (guild_id, plan_id))
        for a in snapshot["assignments"]:
            conn.execute("INSERT INTO tw_def_assignments (guild_id, plan_id, zone, squad_id, squad_name, ally_code, units_json, "
                         "datacron_id, created_by, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                         (guild_id, plan_id, a["zone"], a["squad_id"], a["squad_name"], a["ally_code"], json.dumps(a["units"]),
                          a.get("datacron_id"), a["created_by"], a["created_at"]))
        conn.execute("INSERT OR REPLACE INTO tw_workspaces VALUES (?, ?, ?)",
                     (guild_id, plan_id, json.dumps(snapshot["workspace"], ensure_ascii=False)))


def reserve_assignments(ws, roster, squads):
    """Reserve every alternative of the selected squad; no silent fallback into defense."""
    library = {s["id"]: s for s in squads}
    rows = []
    for r in ws["reserves"]:
        squad = library.get(r["squad_id"])
        if squad:
            _status, dc_id = engine.pick_datacron(roster, r["ally_code"], squad.get("datacron"),
                engine.used_datacrons_by_player(rows).get(r["ally_code"], set()))
            rows.append({"ally_code": r["ally_code"], "units": sorted(engine.squad_base_ids(squad)),
                         "zone": None, "squad_id": None, "datacron_id": dc_id, "reserve": True})
    return rows


def audit(plan, assignments, ws, roster, squads):
    library = {s["id"]: s for s in squads}
    reserves = reserve_assignments(ws, roster, squads)
    reserved = engine.used_units_by_player(reserves)
    reserved_dc = engine.used_datacrons_by_player(reserves)
    unit_uses, dc_uses = Counter(), Counter()
    for a in assignments:
        unit_uses.update((a["ally_code"], b) for b in a["units"])
        dc_uses.update((a["ally_code"], b) for b in (a.get("datacron_id") or "").split(",") if b)
    issues = []
    for a in assignments:
        reasons = []
        code = a["ally_code"]
        squad = library.get(a["squad_id"])
        zone = engine.ZONES_BY_KEY.get(a["zone"])
        if code not in roster.ally_codes:
            reasons.append("игрок больше не в текущем составе")
        if code in plan["excluded"]:
            reasons.append("игрок отмечен как неучаствующий")
        if not squad:
            reasons.append("пак удалён из библиотеки")
        elif not zone or squad["combat_type"] != zone["kind"]:
            reasons.append("тип отряда не соответствует зоне")
        elif len(a["units"]) != len(squad["slots"]):
            reasons.append("состав отличается от текущего шаблона")
        else:
            for base_id, slot in zip(a["units"], squad["slots"]):
                opts = [o for o in slot["options"] if o["base_id"] == base_id]
                if not opts:
                    reasons.append(f"{roster.unit_name(base_id)}: больше не входит в этот слот")
                elif all(engine.check_option(roster, code, o, squad["combat_type"]) is not None for o in opts):
                    reasons.append(f"{roster.unit_name(base_id)}: {engine.check_option(roster, code, opts[0], squad['combat_type'])}")
        if any(unit_uses[(code, b)] > 1 for b in a["units"]):
            reasons.append("один юнит назначен несколько раз")
        if set(a["units"]) & reserved.get(code, set()):
            reasons.append("использован юнит из резерва атаки")
        if squad and (squad.get("datacron") or {}).get("required"):
            ids = set(filter(None, (a.get("datacron_id") or "").split(",")))
            if ids & reserved_dc.get(code, set()):
                reasons.append("датакрон сохранён для атаки")
            if any(dc_uses[(code, b)] > 1 for b in ids):
                reasons.append("датакрон назначен несколько раз")
            # Restrict the check to the actual assigned datacrons, not another available one.
            own = roster.datacrons.get(code)
            if own is not None:
                restricted = copy.copy(roster)
                restricted.datacrons = {**roster.datacrons, code: [d for d in own if str(d.get("id")) in ids]}
            else:
                restricted = roster
            dc_state = engine.check_datacron(restricted, code, squad["datacron"])
            if not ids or dc_state != "ok":
                reasons.append(engine.DC_REASONS.get(dc_state, "обязательный датакрон не назначен"))
        if reasons:
            issues.append({"assignment_id": a["id"], "zone": a["zone"], "ally_code": code,
                           "name": roster.names.get(code, code), "squad": a["squad_name"], "reasons": reasons})
    for z in engine.ZONES:
        count = sum(a["zone"] == z["key"] for a in assignments)
        if count > engine.zone_capacity(plan, z["key"]):
            issues.append({"zone": z["key"], "name": z["label"], "squad": "Вместимость зоны",
                           "reasons": [f"Назначено {count}, вместимость {engine.zone_capacity(plan, z['key'])}"]})
    return issues


def roster_hash(roster):
    return digest({"codes": roster.ally_codes, "units": roster.units, "dc": roster.datacrons,
                   "requirements": sorted((str(k), v) for k, v in roster.dc_reqs.items()), "skills": roster.skills,
                   "stats_ready": roster.stat_calc is not None})


def propose(plan, assignments, ws, roster, squads):
    """Fill prioritized targets, scarce squads first; existing assignments are fixed."""
    library = {s["id"]: s for s in squads}
    working = list(assignments) + reserve_assignments(ws, roster, squads)
    added, shortages = [], []
    targets = sorted(ws["targets"], key=lambda t: (-t["priority"],
        engine.availability(roster, library[t["squad_id"]], working, set(plan["excluded"]))["can_now"]
        if t["squad_id"] in library else 0, t["zone"], t["squad_id"]))
    for target in targets:
        squad = library.get(target["squad_id"])
        zone = engine.ZONES_BY_KEY.get(target["zone"])
        if not squad or not zone or squad["combat_type"] != zone["kind"]:
            shortages.append({"zone": target["zone"], "squad": squad["name"] if squad else "Удалённый пак", "missing": target["count"],
                              "reason": "Проверьте шаблон стратегии: пак удалён или не подходит зоне."})
            continue
        placed = sum(a.get("zone") == target["zone"] and a.get("squad_id") == squad["id"] for a in working)
        need = max(0, target["count"] - placed)
        original_need = need
        failures = Counter()
        while need:
            if sum(a.get("zone") == zone["key"] for a in working) >= engine.zone_capacity(plan, zone["key"]):
                failures["зона заполнена"] += need
                break
            candidates = engine.evaluate_candidates(roster, squad, working, set(plan["excluded"]), squads)
            selected = engine.suggest(candidates, 1)
            if not selected:
                failures.update(c["reason"] or "нет доступных игроков" for c in candidates)
                break
            c = selected[0]
            dc_state, dc_id = engine.pick_datacron(roster, c["ally_code"], squad.get("datacron"),
                engine.used_datacrons_by_player(working).get(c["ally_code"], set()))
            row = {"zone": zone["key"], "squad_id": squad["id"], "squad_name": squad["name"],
                   "ally_code": c["ally_code"], "name": c["name"], "units": c["units"], "datacron_id": dc_id,
                   "reason": f"Других паков блокирует: {c['conflict']}; уже назначено игроку: {c['assigned']}"}
            working.append(row)
            added.append(row)
            need -= 1
        if need:
            shortages.append({"zone": zone["key"], "squad": squad["name"], "missing": need,
                              "reason": "; ".join(f"{reason} ({n})" for reason, n in failures.most_common(3)) or "Нет доступных игроков",
                              "wanted": original_need})
    return {"rows": added, "shortages": shortages}


def save_proposal(guild_id, plan_id, rev, roster, proposal):
    token = uuid.uuid4().hex
    with connect() as conn:
        conn.execute("DELETE FROM tw_plan_proposals WHERE created < ?", (time.time() - 3600,))
        conn.execute("INSERT INTO tw_plan_proposals VALUES (?, ?, ?, ?, ?, ?, ?)",
                     (token, guild_id, plan_id, rev, roster_hash(roster), json.dumps(proposal, ensure_ascii=False), time.time()))
    return token


def proposal(guild_id, plan_id, token):
    with connect() as conn:
        r = conn.execute("SELECT * FROM tw_plan_proposals WHERE token=? AND guild_id=? AND plan_id=? AND created>?",
                         (token, guild_id, plan_id, time.time() - 3600)).fetchone()
    return dict(r) if r else None


def templates(guild_id):
    with connect() as conn:
        return [dict(r) for r in conn.execute("SELECT id, name FROM tw_strategy_templates WHERE guild_id=? ORDER BY name", (guild_id,))]


def save_template(guild_id, name, plan, ws):
    data = {"targets": ws["targets"], "squads_per_zone": plan["squads_per_zone"], "fleets_per_zone": plan["fleets_per_zone"]}
    with connect() as conn:
        conn.execute("INSERT INTO tw_strategy_templates (guild_id, name, data) VALUES (?, ?, ?)",
                     (guild_id, name, json.dumps(data, ensure_ascii=False)))


def load_template(guild_id, template_id):
    with connect() as conn:
        row = conn.execute("SELECT data FROM tw_strategy_templates WHERE guild_id=? AND id=?", (guild_id, template_id)).fetchone()
    return json.loads(row[0]) if row else None


def delivery_rows(guild_id, plan_id):
    with connect() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM tw_plan_deliveries WHERE guild_id=? AND plan_id=?", (guild_id, plan_id))]


def record_delivery(guild_id, plan_id, row, channel, status, detail):
    with connect() as conn:
        conn.execute("INSERT OR REPLACE INTO tw_plan_deliveries VALUES (?, ?, ?, ?, ?, ?, ?, datetime('now'))",
                     (guild_id, plan_id, row["ally_code"], channel, digest(row["entries"]), status, detail))


def annotate_deliveries(guild_id, plan_id, rows, destinations):
    deliveries = {(r["ally_code"], r["channel"]): r for r in delivery_rows(guild_id, plan_id)}
    for row in rows:
        fingerprint = digest(row["entries"])
        entries = [deliveries.get((row["ally_code"], ch)) for ch in destinations]
        row["changed"] = not entries or any(not d or d["status"] not in ("sent", "queued") or d["fingerprint"] != fingerprint for d in entries)
        row["delivery_label"] = "Есть изменения или не отправлено" if row["changed"] else "Передано без изменений"
        row["delivery_states"] = {ch: bool(deliveries.get((row["ally_code"], ch)) and
            deliveries[(row["ally_code"], ch)]["status"] in ("sent", "queued") and
            deliveries[(row["ally_code"], ch)]["fingerprint"] == fingerprint) for ch in ("discord", "tg")}
    return rows
