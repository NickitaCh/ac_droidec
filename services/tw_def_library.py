"""Portable defense libraries: validate a preview before importing into the current guild."""
import json

import database
import tw_def_engine as engine

MAX_BYTES = 1024 * 1024
MAX_SQUADS = 200


def export_library(guild_id: int) -> dict:
    return {"version": 1, "squads": [
        {k: s.get(k) for k in ("name", "combat_type", "slots", "note")}
        for s in database.list_tw_def_squads(guild_id)
    ]}


def preview(raw: str, guild_id: int) -> list[dict]:
    if len(raw.encode("utf-8")) > MAX_BYTES:
        raise ValueError("Файл больше 1 МБ.")
    try:
        data = json.loads(raw)
    except ValueError:
        raise ValueError("Не удалось прочитать JSON библиотеки.") from None
    if not isinstance(data, dict) or data.get("version") != 1:
        raise ValueError("Нужна библиотека деф-паков версии 1, экспортированная с сайта.")
    squads = data.get("squads")
    if not isinstance(squads, list) or not 1 <= len(squads) <= MAX_SQUADS:
        raise ValueError("В библиотеке должно быть от 1 до 200 паков.")
    existing = {s["name"].casefold() for s in database.list_tw_def_squads(guild_id)}
    result, seen = [], set()
    for i, squad in enumerate(squads):
        if not isinstance(squad, dict):
            raise ValueError(f"Пак {i + 1}: неверный формат.")
        name = str(squad.get("name") or "").strip()
        kind, slots = squad.get("combat_type"), squad.get("slots")
        if not name or len(name) > 150 or kind not in engine.MAX_SLOTS or not isinstance(slots, list):
            raise ValueError(f"Пак {i + 1}: проверьте название, тип и слоты.")
        base_ids = [str(o.get("base_id")) for s in slots if isinstance(s, dict)
                    for o in (s.get("options") or []) if isinstance(o, dict) and o.get("base_id")]
        normalized, errors = engine.normalize_slots(slots, kind, database.get_unit_types(base_ids),
                                                     database.get_omicron_capable(base_ids))
        if errors:
            raise ValueError(f"{name}: {'; '.join(errors)}")
        names = database.get_game_unit_names(base_ids)
        labels = [" / ".join((names.get(o["base_id"]) or o["base_id"]) +
                              (f" — {engine.option_requirement_label(o, kind)}" if engine.option_requirement_label(o, kind) else "")
                              for o in slot["options"]) for slot in normalized]
        if name.casefold() in seen:
            raise ValueError(f"Повтор названия в файле: {name}.")
        seen.add(name.casefold())
        result.append({"index": i, "name": name, "combat_type": kind, "slots": normalized,
                       "note": str(squad.get("note") or "")[:4000], "exists": name.casefold() in existing,
                       "slot_labels": labels})
    return result


def import_selected(raw: str, guild_id: int, selected: set[int], user: str) -> int:
    rows = preview(raw, guild_id)
    selected_rows = [s for s in rows if s["index"] in selected and not s["exists"]]
    conn, cursor = database._tw_def_connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        with conn:
            count = 0
            for s in selected_rows:
                # Recheck within the write transaction: a repeated submission never duplicates names.
                cursor.execute("SELECT name FROM tw_def_squads WHERE guild_id = ?", (guild_id,))
                if s["name"].casefold() in {r[0].casefold() for r in cursor.fetchall()}:
                    continue
                cursor.execute("""INSERT INTO tw_def_squads
                    (guild_id, name, combat_type, slots_json, note, created_by, updated_by, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, datetime('now'))""",
                    (guild_id, s["name"], s["combat_type"], json.dumps(s["slots"], ensure_ascii=False),
                     s["note"], user, user))
                count += 1
        return count
    finally:
        conn.close()
