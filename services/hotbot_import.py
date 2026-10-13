"""Импорт плейтов /статы из сообщения HotBot «templates test» (тред «Гайд по АС Боту»,
TODO Nod 2026-10-12: «парс плейтов через чтение сообщения хб по плейту»).

HotBot отвечает на проверку шаблона несколькими сообщениями: заголовок первого эмбеда
«❌ <игрок>: <шаблон>», поля — разделы шаблона («✅ ЛЕЯ», продолжения «✅ ЛЕЯ...»), в
значении поля блок ```md``` по строке на требование:
    ✅ R2-D2 Relic Level[9] is >= 9
    ❌ (Optional)R2-D2 Speed[288] is not >= 300
    ✅ (Optional)Leia Organa Arrow Primary[Speed] is Speed, Health
    ✅ (Optional)Leia Organa [Health, Speed] Mod Set Speed, Health      (Only … / Of … or …)
    ✅ Jedi Knight Revan Speed[307] is > Hermit Yoda Speed [306]
    ✅ First Order Officer Speed[306] is >= General Hux Speed [303+ 1]
В квадратных скобках — текущее значение игрока, нам не нужно. Поле «Failed Test Summary»
дублирует строки и пропускается.

Раздел сопоставляется с плейтом по имени (решение пользователя 2026-10-13: другое имя —
другой плейт, создаём новый), с возможностью указать плейт вручную. Требования, которые есть
у нас, но нет в HotBot, только показываются — удаляются лишь отмеченные вручную на сайте.
Шаблон из нескольких разделов можно собрать в модульный плейт с именем шаблона.

Логика без Discord/веб-зависимостей: бот (cogs/stat_requirements.py) и сайт
(web/routes/stat_plates.py) вызывают fetch_messages → build_plan → apply_plan."""

import os
import re

import aiohttp

import database
import stat_engine
from cogs.datacron_requirements import PRIORITY_OPTIONAL, PRIORITY_REQUIRED
from cogs.stat_requirements import (
    STAT_MOD_PRIMARY, STAT_OMICRON, STAT_SET, STAT_SET_ANY, STAT_SET_ONLY, MOD_SLOT_LABELS,
    mod_primary_allowed, parse_id_list,
)

DISCORD_API = "https://discord.com/api/v10"
LINK_RE = re.compile(r"discord(?:app)?\.com/channels/(\d+|@me)/(\d+)/(\d+)")
SUMMARY_FIELD = "Failed Test Summary"

# Числовые статы HotBot → stat_name плейта. «Critical Chance» без уточнения решается по
# плейту (если у персонажа уже есть Special — Special, иначе Physical), см. _resolve_crit.
_STATS = {
    "Speed": "Speed", "Health": "Health", "Protection": "Protection",
    "Physical Damage": "Physical Damage", "Special Damage": "Special Damage",
    "Armor": "Armor", "Resistance": "Resistance", "Potency": "Potency", "Tenacity": "Tenacity",
    "Critical Damage": "Critical Damage", "Health Steal": "Health Steal",
    "Physical Critical Chance": "Physical Critical Chance", "Special Critical Chance": "Special Critical Chance",
    "Physical Critical Avoidance": "Physical Critical Avoidance", "Special Critical Avoidance": "Special Critical Avoidance",
    "Physical Accuracy": "Physical Accuracy", "Special Accuracy": "Special Accuracy",
    "Critical Chance": "Critical Chance",
}
_SLOTS = {label: key for key, label in (("square", "Square"), ("arrow", "Arrow"), ("diamond", "Diamond"),
                                        ("triangle", "Triangle"), ("circle", "Circle"), ("cross", "Cross"))}
_PRIMARY_IDS = {opt["label"].split(" +")[0]: opt["unit_stat"]
                for opts in stat_engine.MOD_PRIMARY_OPTIONS.values() for opt in opts}
_SET_IDS = {name: int(set_id) for set_id, name in stat_engine.MOD_SET_IDS.items()}
_SET_NAMES = {v: k for k, v in _SET_IDS.items()}
_PRIMARY_NAMES = {v: k for k, v in _PRIMARY_IDS.items()}

_NUM = r"-?[\d.,]+K?"
_RELIC_RE = re.compile(rf"^Relic Level\[[^\]]*\] is (?:not )?(>=|<=|>|<|=) ({_NUM})$")
_PRIMARY_RE = re.compile(r"^(Square|Arrow|Diamond|Triangle|Circle|Cross) Primary\[[^\]]*\] is (?:not )?(.+)$")
_SET_RE = re.compile(r"^\[[^\]]*\] Mod Set (Only |Of )?(.+)$")
_COMPARE_RE = re.compile(r"^(.+?)\[[^\]]*\] is (?:not )?(>=|<=|>|<|=) (.+?) ?\[([^\]]*)\]$")
_STAT_RE = re.compile(rf"^(.+?)\[[^\]]*\] is (?:not )?(>=|<=|>|<|=) ({_NUM})$")
_OFFSET_RE = re.compile(rf"^\s*{_NUM}\s*(?:([+-])\s*({_NUM}))?\s*$")


class HotbotImportError(Exception):
    """Понятная пользователю ошибка (неверная ссылка, нет доступа, не HotBot)."""


# ---------------------------------------------------------------------------
# Чтение сообщений
# ---------------------------------------------------------------------------

def parse_link(link: str) -> tuple[str, str]:
    m = LINK_RE.search(link or "")
    if not m:
        raise HotbotImportError("Нужна ссылка на сообщение Discord (ПКМ по сообщению HotBot → «Копировать ссылку»).")
    return m.group(2), m.group(3)


async def fetch_messages(link: str, token: str | None = None) -> list[dict]:
    """Первое сообщение HotBot по ссылке + его продолжения (ответы на него от того же
    автора) — эмбеды всех по порядку."""
    channel_id, message_id = parse_link(link)
    token = token or os.environ.get("DISCORD_TOKEN")
    if not token:
        raise HotbotImportError("DISCORD_TOKEN не настроен на сервере.")
    headers = {"Authorization": f"Bot {token}", "User-Agent": "DiscordBot (https://swgoh-sng.ru, 1.0)"}
    async with aiohttp.ClientSession(headers=headers, timeout=aiohttp.ClientTimeout(total=15)) as session:
        async with session.get(f"{DISCORD_API}/channels/{channel_id}/messages/{message_id}") as resp:
            if resp.status in (403, 404):
                raise HotbotImportError("Бот не видит это сообщение — нет доступа к каналу или сообщение удалено.")
            resp.raise_for_status()
            first = await resp.json()
        async with session.get(f"{DISCORD_API}/channels/{channel_id}/messages",
                               params={"after": message_id, "limit": 30}) as resp:
            resp.raise_for_status()
            after = await resp.json()
    author = first["author"]["id"]
    rest = [
        m for m in sorted(after, key=lambda m: int(m["id"]))
        if m["author"]["id"] == author and (
            (m.get("message_reference") or {}).get("message_id") == message_id
            or (m.get("interaction_metadata") or {}).get("original_response_message_id") == message_id
        )
    ]
    return [first] + rest


# ---------------------------------------------------------------------------
# Разбор
# ---------------------------------------------------------------------------

def _num(text: str) -> float:
    text = text.replace(",", "").strip()
    return float(text[:-1]) * 1000 if text.endswith("K") else float(text)


def _split_names(text: str) -> list[str]:
    return [p.strip() for p in re.split(r",| or ", text) if p.strip()]


def parse_messages(messages: list[dict]) -> dict:
    """→ {"title", "player", "sections": [{"name", "lines": [str]}]} (строки без ```)."""
    embeds = [e for m in messages for e in m.get("embeds") or []]
    if not embeds or not any(e.get("fields") for e in embeds):
        raise HotbotImportError("В сообщении нет таблицы требований HotBot (нужен ответ на «templates test»).")
    title = (embeds[0].get("title") or "").strip()
    player, _, template = re.sub(r"^[✅❌⚠️]\s*", "", title).rpartition(": ")
    sections = []
    for embed in embeds:
        for field in embed.get("fields") or []:
            name = re.sub(r"^[✅❌⚠️]\s*", "", field.get("name") or "").strip()
            if SUMMARY_FIELD in name:
                continue
            name = re.sub(r"\.{3}$", "", name).strip()
            lines = [ln.strip() for ln in (field.get("value") or "").replace("```md", "").replace("```", "").splitlines()]
            lines = [ln for ln in lines if ln]
            if sections and sections[-1]["name"] == name:
                sections[-1]["lines"].extend(lines)
            else:
                sections.append({"name": name, "lines": lines})
    return {"title": template.strip() or title, "player": player.strip(), "sections": sections}


def _match_unit(text: str, names: list[str], by_name: dict) -> tuple[str | None, str]:
    for name in names:
        if text.startswith(name + " ") or text == name:
            return by_name[name], text[len(name):].strip()
    return None, text


def parse_line(line: str, names: list[str], by_name: dict) -> dict | str:
    """Строка HotBot → требование (dict) либо причина, почему не переносится (str)."""
    body = re.sub(r"^[✅❌⚠️]\s*", "", line)
    priority = PRIORITY_REQUIRED
    if body.startswith("(Optional)"):
        priority, body = PRIORITY_OPTIONAL, body[len("(Optional)"):].strip()
    char, rest = _match_unit(body, names, by_name)
    if char is None:
        return "персонаж не найден в справочнике"
    item = {"line": body, "char": char, "priority": priority}

    if m := _RELIC_RE.match(rest):
        if m.group(1) not in (">=", "<=", "="):
            return f"оператор {m.group(1)} для реликвии не поддерживается"
        return {**item, "kind": "num", "stat": "Relic", "op": m.group(1), "value": _num(m.group(2)), "value_text": m.group(2)}
    if m := _PRIMARY_RE.match(rest):
        slot = _SLOTS[m.group(1)]
        ids = []
        for name in _split_names(m.group(2)):
            sid = _PRIMARY_IDS.get(name)
            if sid is None or not any(o["unit_stat"] == sid for o in stat_engine.MOD_PRIMARY_OPTIONS.get(slot, [])):
                return f"основа «{name}» недопустима для слота"
            if sid not in ids:
                ids.append(sid)
        return {**item, "kind": "pri", "stat": STAT_MOD_PRIMARY, "slot": slot, "ids": ids}
    if m := _SET_RE.match(rest):
        names_ = _split_names(m.group(2))
        ids = [_SET_IDS.get(n) for n in names_]
        if None in ids:
            return "неизвестный сет"
        mode = (m.group(1) or "").strip()
        if mode == "Only":
            return {**item, "kind": "setlist", "stat": STAT_SET_ONLY, "ids": list(dict.fromkeys(ids))}
        if mode == "Of" and len(set(ids)) > 1:
            return {**item, "kind": "setlist", "stat": STAT_SET_ANY, "ids": list(dict.fromkeys(ids))}
        # «Mod Set A, B» — нужен каждый сет (повтор имени = больше комплектов);
        # «Of A» с одним сетом — то же, что обычный сет (как при импорте из HotUtils).
        counts = {}
        for sid in ids:
            counts[sid] = counts.get(sid, 0) + 1
        return {**item, "kind": "sets", "sets": counts}
    if m := _COMPARE_RE.match(rest):
        stat_name, op, target, bracket = m.groups()
        other, other_stat = _match_unit(target, names, by_name)
        if other is not None and other_stat == stat_name:
            stat = _STATS.get(stat_name)
            if stat is None:
                return f"стат «{stat_name}» не поддерживается"
            off = _OFFSET_RE.match(bracket)
            offset = _num(off.group(2)) if off and off.group(2) else 0.0
            if off and off.group(1) == "-":
                offset = -offset
            if offset < 0:
                return "отрицательный отрыв в сравнении не поддерживается"
            return {**item, "kind": "cmp", "stat": stat, "op": op, "compare": other, "value": offset}
    if m := _STAT_RE.match(rest):
        stat_name, op, value = m.groups()
        stat = _STATS.get(stat_name)
        if stat is None:
            return f"«{stat_name}» не поддерживается плейтами"
        if op not in (">=", "<=", "="):
            return f"оператор {op} для числового порога не поддерживается"
        return {**item, "kind": "num", "stat": stat, "op": op, "value": _num(value), "value_text": value}
    return "формат строки не распознан"


def _expand(items: list[dict]) -> list[dict]:
    """«Mod Set A, B» → отдельное требование на каждый сет (как хранит плейт)."""
    out = []
    for it in items:
        if it["kind"] == "sets":
            for sid, count in it["sets"].items():
                out.append({**it, "kind": "set", "stat": STAT_SET, "set_id": sid, "value": float(count)})
        else:
            out.append(it)
    return out


# ---------------------------------------------------------------------------
# Сравнение с плейтом
# ---------------------------------------------------------------------------

def _bucket(priority: str) -> str:
    return "req" if priority == PRIORITY_REQUIRED else "opt"


def _row_key(row) -> tuple | None:
    _id, _plate, char, stat, _op, thr, prio, *_rest = row
    skill_id, slot, compare = row[11], row[12], row[13]
    if stat == STAT_OMICRON:
        return None
    if stat == STAT_MOD_PRIMARY:
        return ("pri", char, slot)
    if stat == STAT_SET:
        return ("set", char, int(skill_id or thr))
    if stat in (STAT_SET_ANY, STAT_SET_ONLY):
        return (stat, char)
    if compare:
        return ("cmp", char, stat, compare, _bucket(prio))
    return ("num", char, stat, _bucket(prio))


def _item_key(it: dict) -> tuple:
    kind = it["kind"]
    if kind == "pri":
        return ("pri", it["char"], it["slot"])
    if kind == "set":
        return ("set", it["char"], it["set_id"])
    if kind == "setlist":
        return (it["stat"], it["char"])
    if kind == "cmp":
        return ("cmp", it["char"], it["stat"], it["compare"], _bucket(it["priority"]))
    return ("num", it["char"], it["stat"], _bucket(it["priority"]))


def _value_equal(ours: float, it: dict) -> bool:
    text = it.get("value_text") or ""
    if text.endswith("K"):  # HotBot округляет до 0.1K — 15523 и «15.5K» одно и то же
        return round(ours / 1000, 1) == round(it["value"] / 1000, 1)
    return abs(ours - it["value"]) < 0.01


def _diff_fields(row, it: dict) -> dict | None:
    """Что поменять в строке плейта, чтобы она совпала с HotBot; None — совпадает."""
    op, thr, prio, skill_id = row[4], row[5], row[6], row[11]
    changes = {}
    if _bucket(prio) != _bucket(it["priority"]):
        changes["priority"] = it["priority"]
    kind = it["kind"]
    if kind in ("num", "cmp"):
        if op != it["op"]:
            changes["operator"] = it["op"]
        if not _value_equal(thr, it):
            changes["threshold_value"] = it["value"]
    elif kind == "pri":
        if set(mod_primary_allowed(thr, skill_id)) != set(it["ids"]):
            changes["threshold_value"] = float(it["ids"][0])
            changes["skill_id"] = ",".join(map(str, it["ids"])) if len(it["ids"]) > 1 else None
    elif kind == "set":
        if max(int(thr or 1), 1) != int(it["value"]):
            changes["threshold_value"] = it["value"]
    elif kind == "setlist":
        if set(parse_id_list(skill_id)) != set(it["ids"]):
            changes["skill_id"] = ",".join(map(str, it["ids"]))
    return changes or None


def _resolve_crit(items: list[dict], rows: list) -> None:
    special = {r[2] for r in rows if r[3] == "Special Critical Chance"}
    for it in items:
        if it.get("stat") == "Critical Chance":
            it["stat"] = "Special Critical Chance" if it["char"] in special else "Physical Critical Chance"


def describe_item(it: dict) -> str:
    star = "" if it["priority"] == PRIORITY_REQUIRED else "*"
    name = database.get_game_unit_name(it["char"]) or it["char"]
    kind = it["kind"]
    if kind == "num":
        text = f"{it['stat']} {it['op']} {it['value']:g}"
    elif kind == "cmp":
        other = database.get_game_unit_name(it["compare"]) or it["compare"]
        text = f"{it['stat']} {it['op']} {other}" + (f" +{it['value']:g}" if it["value"] else "")
    elif kind == "pri":
        text = f"{MOD_SLOT_LABELS.get(it['slot'], it['slot'])}: " + " / ".join(_PRIMARY_NAMES.get(i, str(i)) for i in it["ids"])
    elif kind == "set":
        text = f"Сет {_SET_NAMES.get(it['set_id'], it['set_id'])}" + (f" ×{int(it['value'])}" if it["value"] > 1 else "")
    else:
        label = "Только сеты" if it["stat"] == STAT_SET_ONLY else "Хотя бы один сет"
        text = f"{label}: " + " / ".join(_SET_NAMES.get(i, str(i)) for i in it["ids"])
    return f"{name} — {text}{star}"


def describe_row(row) -> str:
    _id, _plate, char, stat, op, thr, prio = row[:7]
    skill_id, slot, compare = row[11], row[12], row[13]
    it = {"char": char, "priority": prio, "stat": stat}
    if stat == STAT_OMICRON:
        name = database.get_game_unit_name(char) or char
        return f"{name} — омикрон {skill_id}" + ("" if prio == PRIORITY_REQUIRED else "*")
    if stat == STAT_MOD_PRIMARY:
        it.update(kind="pri", slot=slot, ids=mod_primary_allowed(thr, skill_id))
    elif stat == STAT_SET:
        it.update(kind="set", set_id=int(skill_id or thr), value=float(max(int(thr or 1), 1)))
    elif stat in (STAT_SET_ANY, STAT_SET_ONLY):
        it.update(kind="setlist", ids=parse_id_list(skill_id))
    elif compare:
        it.update(kind="cmp", op=op, compare=compare, value=thr or 0.0)
    else:
        it.update(kind="num", op=op, value=thr)
    return describe_item(it)


def default_plate(section_name: str, title: str, single: bool, plates: list[str]) -> str:
    """Плейт по имени раздела (шаблон из одного раздела — по имени шаблона); регистр не важен."""
    wanted = title if single and title else section_name
    return next((p for p in plates if p.lower() == wanted.lower()), wanted)


def build_plan(parsed: dict, guild_id: int, mapping: dict | None = None) -> dict:
    """Разница между HotBot и плейтами гильдии. mapping: {раздел: плейт} — ручной выбор,
    иначе default_plate. Ничего не пишет."""
    mapping = mapping or {}
    by_name = database.get_game_unit_en_names()
    names = sorted(by_name, key=len, reverse=True)
    plates = database.get_all_stat_requirement_plates(guild_id=guild_id)
    single = len(parsed["sections"]) == 1
    all_rows = database.get_all_stat_requirements(guild_id=guild_id)
    plate_chars = {}
    for r in all_rows:
        plate_chars.setdefault(r[1], set()).add(r[2])
    sections = []
    for s_idx, section in enumerate(parsed["sections"]):
        plate = (mapping.get(section["name"]) or "").strip() or default_plate(section["name"], parsed["title"], single, plates)
        exists = plate in plates
        modular = exists and database.is_stat_plate_modular(plate, guild_id=guild_id)
        rows = [r for r in all_rows if r[1] == plate] if exists and not modular else []
        items, skipped = [], []
        for line in section["lines"]:
            parsed_line = parse_line(line, names, by_name)
            if isinstance(parsed_line, str):
                skipped.append({"line": re.sub(r"^[✅❌⚠️]\s*", "", line), "reason": parsed_line})
            else:
                items.append(parsed_line)
        items = _expand(items)
        _resolve_crit(items, rows)

        pool = {}
        for row in rows:
            key = _row_key(row)
            if key is not None:
                pool.setdefault(key, []).append(row)
        add, change, same, seen = [], [], 0, set()
        for i_idx, it in enumerate(items):
            key = _item_key(it)
            if key in seen and it["kind"] != "num":
                continue  # дубль одной и той же основы/сета внутри HotBot
            seen.add(key)
            item_id = f"{s_idx}.{i_idx}"
            candidates = pool.get(key) or []
            if not candidates:
                add.append({"id": item_id, "text": describe_item(it), "item": it})
                continue
            row = candidates.pop(0)
            fields = _diff_fields(row, it)
            if fields is None:
                same += 1
            else:
                change.append({"id": item_id, "text": describe_item(it), "old_text": describe_row(row),
                               "req_id": row[0], "fields": fields, "item": it})
        only_ours = [{"id": f"del.{r[0]}", "req_id": r[0], "text": describe_row(r)}
                     for rows_ in pool.values() for r in rows_]
        # Подсказка: существующий плейт с теми же персонажами (раздел «ЛЕЯ» ≈ AC_ORGANA).
        chars = {it["char"] for it in items}
        suggest = None
        if chars:
            best = max(plate_chars.items(), key=lambda kv: (len(kv[1] & chars), -len(kv[1])), default=(None, set()))
            if best[0] and best[0] != plate and len(best[1] & chars) * 2 >= len(chars):
                suggest = {"plate": best[0], "overlap": len(best[1] & chars), "total": len(chars)}
        sections.append({
            "name": section["name"], "plate": plate, "plate_exists": exists, "plate_modular": modular, "suggest": suggest,
            "add": add, "change": change, "same": same, "only_ours": only_ours, "skipped": skipped,
        })

    parent = None
    if len(sections) > 1 and parsed["title"]:
        name = parsed["title"]
        exists = name in plates
        modular = exists and database.is_stat_plate_modular(name, guild_id=guild_id)
        current = {sp for _id, sp, ck in database.get_stat_plate_components(name, guild_id=guild_id) if not ck} if modular else set()
        missing = [s["plate"] for s in sections if s["plate"] not in current and not s["plate_modular"] and s["plate"] != name]
        parent = {"name": name, "exists": exists, "modular": modular, "missing": list(dict.fromkeys(missing)),
                  "usable": not exists or modular}
    return {"title": parsed["title"], "player": parsed["player"], "sections": sections, "parent": parent,
            "plates": sorted(p for p in plates if not database.is_stat_plate_modular(p, guild_id=guild_id))}


def default_selection(plan: dict) -> set:
    """Что отмечено по умолчанию: добавления и изменения; удаления — нет."""
    return {x["id"] for s in plan["sections"] if not s["plate_modular"] for x in s["add"] + s["change"]}


def _row_raw_text(it: dict) -> str:
    return f"HotBot: {it['line']}"


def apply_plan(plan: dict, selected: set, guild_id: int, actor: str, build_parent: bool = False) -> dict:
    """Применяет отмеченное. → счётчики {"added", "changed", "deleted", "created_plates", "parent"}."""
    result = {"added": 0, "changed": 0, "deleted": 0, "created_plates": [], "parent": None}
    known = set(database.get_all_stat_requirement_plates(guild_id=guild_id))
    for s in plan["sections"]:
        if s["plate_modular"]:
            continue
        adds = [x for x in s["add"] if x["id"] in selected]
        if adds and s["plate"] not in known:
            database.create_stat_plate(s["plate"], f"Импорт из HotBot: {plan['title']}", actor, guild_id=guild_id)
            known.add(s["plate"])
            result["created_plates"].append(s["plate"])
        for x in adds:
            it = x["item"]
            kind = it["kind"]
            kwargs = {}
            if kind == "num":
                stat, op, thr = it["stat"], it["op"], it["value"]
            elif kind == "cmp":
                stat, op, thr = it["stat"], it["op"], it["value"]
                kwargs["compare_character_key"] = it["compare"]
            elif kind == "pri":
                stat, op, thr = STAT_MOD_PRIMARY, "=", float(it["ids"][0])
                kwargs["mod_slot"] = it["slot"]
                kwargs["skill_id"] = ",".join(map(str, it["ids"])) if len(it["ids"]) > 1 else None
            elif kind == "set":
                stat, op, thr = STAT_SET, ">=", it["value"]
                kwargs["skill_id"] = str(it["set_id"])
            else:
                stat, op, thr = it["stat"], ">=", 1.0
                kwargs["skill_id"] = ",".join(map(str, it["ids"]))
            database.add_stat_requirement(s["plate"], it["char"], stat, op, thr, it["priority"],
                                          _row_raw_text(it), None, actor, guild_id=guild_id, **kwargs)
            result["added"] += 1
        for x in s["change"]:
            if x["id"] in selected:
                database.update_stat_requirement_fields(x["req_id"], guild_id, raw_text=_row_raw_text(x["item"]), **x["fields"])
                result["changed"] += 1
        for x in s["only_ours"]:
            if x["id"] in selected:
                database.delete_stat_requirement(x["req_id"], guild_id=guild_id)
                result["deleted"] += 1

    parent = plan.get("parent")
    if build_parent and parent and parent["usable"]:
        if not parent["exists"]:
            database.create_stat_plate(parent["name"], "Импорт из HotBot (модульный)", actor, guild_id=guild_id, modular=True)
        existing = [(sp, ck) for _id, sp, ck in database.get_stat_plate_components(parent["name"], guild_id=guild_id)]
        wanted = [p for p in parent["missing"] if p in known or p in set(database.get_all_stat_requirement_plates(guild_id=guild_id))]
        if wanted or not parent["exists"]:
            ok, error = database.set_stat_plate_components(parent["name"], existing + [(p, "") for p in wanted], actor, guild_id=guild_id)
            result["parent"] = parent["name"] if ok else f"ошибка: {error}"
    return result


def plan_counts(plan: dict) -> dict:
    return {
        "add": sum(len(s["add"]) for s in plan["sections"] if not s["plate_modular"]),
        "change": sum(len(s["change"]) for s in plan["sections"] if not s["plate_modular"]),
        "same": sum(s["same"] for s in plan["sections"]),
        "only_ours": sum(len(s["only_ours"]) for s in plan["sections"]),
        "skipped": sum(len(s["skipped"]) for s in plan["sections"]),
    }
