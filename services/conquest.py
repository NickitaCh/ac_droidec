"""Калькулятор Завоевания (веб /conquest): игрок отмечает подвиги, страница складывает
ключ-карты и показывает достигнутый сундук и что в нём (осколки персонажей и прочее).

Всё из игровых данных, без входа в аккаунт (проверено 2026-10-21 на CONQUEST_VOL25):
- текущее Завоевание — событие getEvents с type 14 и conquestId, instance[].startTime/endTime;
- подвиги — event.feat[].challengeId → challenge (флаг CHALLENGE): reward type 22 = ключ-карты,
  в id сложность (_I_DIFF — лёгкое, _II_ — обычное, _III_ — сложное) и сектор (_S0…_S4);
  EVENT_ — общие подвиги, SECTOR_/BOSS_/MINIBOSS_ — подвиги сектора, его босса и мини-босса;
- сундуки — conquestDefinition[id].conquestDifficulty[].rankRewardPreview: rankStart = сколько
  ключ-карт нужно (rankEnd 0 — последний сундук), содержимое — mysteryBox previewItem.

Ключ-карты игра даёт и за бои, не только за подвиги (на сложном подвиги дают 334 из 630
на последний сундук), а сколько именно за бои — в данных нет, поэтому это поле вписывает
сам игрок. Синхронизировать отметки с игрой без EA-входа нельзя — отметки ручные."""

import asyncio
import json
import re
import time

import database

STATE_KEY = "conquest_current"  # bot_state guild 0 — пишет units_sync
EVENT_TYPE = 14
CHALLENGE_FLAG = 8388608
CONQUEST_FLAG = 549755813888
MYSTERY_BOX_FLAG = 1073741824
MATERIAL_FLAG = 16384
KEYCARD_REWARD_TYPE = 22
DIFFICULTIES = [("easy", "I"), ("normal", "II"), ("hard", "III")]
DIFF_NAME_KEYS = {"easy": "DIFF_EASY", "normal": "DIFF_NORMAL", "hard": "DIFF_HARD"}
KIND_LABELS = {"event": "Общий", "sector": "Сектор", "boss": "Босс", "miniboss": "Мини-босс"}
_FEAT_RE = re.compile(r"_(EVENT|SECTOR|BOSS|MINIBOSS)_.*_(I{1,3})_DIFF(?:_S(\d))?$")
_COLOR_RE = re.compile(r"\[[0-9A-Fa-f]{6}\]|\[-\]|\[/?[a-z]\]")


def _clean(text: str) -> str:
    return _COLOR_RE.sub("", (text or "").replace("\\n", " ")).strip()


def current_event(events: list, now_ms: int | None = None) -> tuple[dict, dict] | None:
    """(событие, instance) Завоевания, которое идёт или начнётся следующим."""
    now_ms = now_ms if now_ms is not None else int(time.time() * 1000)
    best = None
    for e in events or []:
        if e.get("type") != EVENT_TYPE or not e.get("conquestId"):
            continue
        for inst in e.get("instance") or []:
            end = int(inst.get("endTime") or 0)
            if end <= now_ms:
                continue
            if best is None or int(inst.get("startTime") or 0) < int(best[1].get("startTime") or 0):
                best = (e, inst)
    return best


def _item_name(item_id: str, loc: dict, materials: dict, boxes: dict, unit_names: dict) -> str:
    if item_id.startswith("unitshard_"):
        bid = item_id[len("unitshard_"):]
        return f"Осколки: {unit_names.get(bid, bid)}"
    if item_id == "GRIND":
        return "Кредиты"
    if item_id in materials:
        return loc.get(materials[item_id], item_id)
    if item_id in boxes:
        return loc.get(boxes[item_id], item_id)
    return loc.get(f"SHARED_CURRENCY_{item_id}", item_id)


def parse(event: dict, inst: dict, challenges: list, conquest_defs: list, mystery_boxes: list,
          materials: list, loc: dict, unit_names: dict) -> dict:
    """Состояние для страницы — см. STATE_KEY."""
    by_id = {c.get("id"): c for c in challenges}
    material_keys = {m["id"]: m.get("nameKey") for m in materials if m.get("id")}
    box_keys = {b["id"]: b.get("titleKey") for b in mystery_boxes if b.get("id")}
    box_items = {b["id"]: b.get("previewItem") or [] for b in mystery_boxes if b.get("id")}
    roman_to_key = {roman: key for key, roman in DIFFICULTIES}

    feats = {key: [] for key, _ in DIFFICULTIES}
    for f in event.get("feat") or []:
        c = by_id.get(f.get("challengeId"))
        m = _FEAT_RE.search(f.get("challengeId") or "")
        if not c or not m:
            continue
        keycards = sum(int(r.get("minQuantity") or 0) for r in c.get("reward") or [] if r.get("type") == KEYCARD_REWARD_TYPE)
        if not keycards:
            continue
        kind = m.group(1).lower()
        feats[roman_to_key[m.group(2)]].append({
            "id": c["id"], "kind": kind,
            "sector": int(m.group(3)) + 1 if m.group(3) is not None else None,
            "name": _clean(loc.get(c.get("nameKey"), c.get("nameKey") or "")),
            "desc": _clean(loc.get(c.get("descKey"), c.get("descKey") or "")),
            "keycards": keycards,
        })

    conquest = next((d for d in conquest_defs if d.get("id") == event.get("conquestId")), {})
    crates_by_diff = {}
    for diff in conquest.get("conquestDifficulty") or []:
        name_key = diff.get("nameKey")
        key = next((k for k, nk in DIFF_NAME_KEYS.items() if nk == name_key), None)
        if key is None:
            continue
        crates = []
        for n, r in enumerate(sorted(diff.get("rankRewardPreview") or [], key=lambda x: x.get("rankStart") or 0), 1):
            items = []
            for reward in r.get("detailedReward") or r.get("primaryReward") or []:
                for it in box_items.get(reward.get("id"), []):
                    qty = it.get("maxQuantity") or it.get("minQuantity") or 1
                    items.append([it["id"], _item_name(it["id"], loc, material_keys, box_keys, unit_names), qty])
            crates.append({"n": n, "min": int(r.get("rankStart") or 0), "items": items})
        crates_by_diff[key] = {"crates": crates, "min_gp": int(diff.get("minGp") or 0)}

    difficulties = []
    for key, _roman in DIFFICULTIES:
        info = crates_by_diff.get(key) or {"crates": [], "min_gp": 0}
        fs = sorted(feats[key], key=lambda f: (f["sector"] or 0, ["event", "sector", "miniboss", "boss"].index(f["kind"])))
        difficulties.append({
            "key": key, "name": loc.get(DIFF_NAME_KEYS[key], key), "min_gp": info["min_gp"],
            "feats": fs, "crates": info["crates"],
        })
    return {"id": event.get("conquestId"), "event_id": event.get("id"),
            "start": int(inst.get("startTime") or 0), "end": int(inst.get("endTime") or 0),
            "difficulties": difficulties}


async def sync(comlink, loc: dict, units: list | None = None) -> bool:
    """Обновляет STATE_KEY. False — Завоевания сейчас нет (старое состояние не трогаем).
    units — канонические юниты игры: с ними к подвигам дописывается «Кем пройти?»."""
    raw = await asyncio.to_thread(comlink.get_events)
    events = raw.get("gameEvent") if isinstance(raw, dict) else raw
    found = current_event(events or [])
    if not found:
        return False
    event, inst = found

    def _data(flag):
        return comlink.get_game_data(items=str(flag), include_pve_units=False)

    challenges = (await asyncio.to_thread(_data, CHALLENGE_FLAG)).get("challenge") or []
    conquest_defs = (await asyncio.to_thread(_data, CONQUEST_FLAG)).get("conquestDefinition") or []
    boxes = [b for b in (await asyncio.to_thread(_data, MYSTERY_BOX_FLAG)).get("mysteryBox") or []
             if b.get("id", "").startswith("mysterybox_conquest")]
    materials = (await asyncio.to_thread(_data, MATERIAL_FLAG)).get("material") or []
    shard_ids = {it["id"][len("unitshard_"):] for b in boxes for it in b.get("previewItem") or []
                 if it.get("id", "").startswith("unitshard_")}
    unit_names = database.get_game_unit_names(list(shard_ids))
    state = parse(event, inst, challenges, conquest_defs, boxes, materials, loc, unit_names)
    if not any(d["feats"] for d in state["difficulties"]):
        return False
    if units:
        try:
            from services import conquest_who
            await conquest_who.annotate(comlink, state, units, loc)
        except Exception as e:  # подсказка не должна ломать сам калькулятор
            print(f"⚠️ [Завоевание] «Кем пройти?» не посчитано: {e!r}")
    database.set_bot_state(STATE_KEY, json.dumps(state, ensure_ascii=False), guild_id=0)
    return True


def state() -> dict | None:
    raw = database.get_bot_state(STATE_KEY, guild_id=0)
    return json.loads(raw) if raw else None


def evaluate(diff: dict, done: set, battle_keycards: int) -> dict:
    """{feat_keycards, total, crate (достигнутый или None), next (следующий или None),
    need (сколько до следующего)}."""
    feat_keycards = sum(f["keycards"] for f in diff["feats"] if f["id"] in done)
    total = feat_keycards + max(0, battle_keycards)
    reached = [c for c in diff["crates"] if total >= c["min"]]
    crate = reached[-1] if reached else None
    nxt = next((c for c in diff["crates"] if total < c["min"]), None)
    return {"feat_keycards": feat_keycards, "total": total, "crate": crate, "next": nxt,
            "need": nxt["min"] - total if nxt else 0,
            "max_feats": sum(f["keycards"] for f in diff["feats"])}
