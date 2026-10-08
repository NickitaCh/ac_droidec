# Деф на ВГ — чистая логика без веба/Discord: зоны карты, проверка требований деф-пака
# на ростере игрока, подбор конкретных юнитов в пак, автоподбор игроков в зону и текст
# расстановки для Discord. Используется вебом (web/routes/tw_defense.py), дальше — ботом
# (Discord-команда + форум паков), поэтому никаких FastAPI/disnake-импортов здесь нет.
#
# Модель пака (database.tw_def_squads.slots_json): список слотов, слот = упорядоченный
# список допустимых юнитов ("options") со своими требованиями. Первый подходящий вариант
# слота берётся основным, остальные — замены (как "By order" в HotUtils); пул "любые 2 из
# 5" = два слота с одинаковым списком. Один юнит игрока не может стоять в двух слотах
# одного пака и в двух паках одного плана — это и есть ограничение, которое не даёт
# HotUtils подбирать игроков автоматически, а здесь его решает перебор (match_squad).
from dataclasses import dataclass, field

import database
import stat_engine

# Карта ВГ — 10 зон. key хранится в tw_def_assignments.zone, label — подпись как в
# шаблонах гильдии в HotUtils (1 верх / 1 низ / ... / 4 флот). column/row — место на
# сетке карты (столбец 1 — передняя линия).
ZONES = [
    {"key": "1t", "label": "1 верх", "kind": "character", "column": 1, "row": 1},
    {"key": "1b", "label": "1 низ", "kind": "character", "column": 1, "row": 3},
    {"key": "2t", "label": "2 верх", "kind": "character", "column": 2, "row": 1},
    {"key": "2b", "label": "2 низ", "kind": "character", "column": 2, "row": 3},
    {"key": "3f", "label": "3 флот", "kind": "ship", "column": 3, "row": 1},
    {"key": "3c", "label": "3 центр", "kind": "character", "column": 3, "row": 2},
    {"key": "3b", "label": "3 низ", "kind": "character", "column": 3, "row": 3},
    {"key": "4f", "label": "4 флот", "kind": "ship", "column": 4, "row": 1},
    {"key": "4c", "label": "4 центр", "kind": "character", "column": 4, "row": 2},
    {"key": "4b", "label": "4 низ", "kind": "character", "column": 4, "row": 3},
]
ZONES_BY_KEY = {z["key"]: z for z in ZONES}

MAX_SLOTS = {"character": 5, "ship": 8}
SHIP_DEFAULT_MIN_STARS = 7

OPTION_INT_FIELDS = {"min_relic": (0, 10), "min_stars": (1, 7), "min_gear": (1, 13), "min_speed": (1, 999)}
OPTION_BOOL_FIELDS = ("zeta", "omicron", "ultimate")


def zone_capacity(plan: dict, zone_key: str) -> int:
    zone = ZONES_BY_KEY[zone_key]
    return plan["fleets_per_zone"] if zone["kind"] == "ship" else plan["squads_per_zone"]


# ---------------------------------------------------------------------------
# Валидация пака из формы
# ---------------------------------------------------------------------------
def normalize_slots(raw_slots, combat_type: str, known_units: dict, omicron_capable=None) -> tuple[list, list[str]]:
    """raw_slots — JSON из редактора; known_units — {base_id: unit_type} (game_units);
    omicron_capable — base_id с омикроном в игре: у остальных флаг "омикрон" снимается,
    иначе требование выглядело бы строгим, а проверялось бы впустую.
    Возвращает (чистые слоты, ошибки). Пустые варианты/слоты выбрасываются молча."""
    errors: list[str] = []
    slots = []
    if not isinstance(raw_slots, list):
        return [], ["Некорректный формат слотов."]
    for slot_index, raw_slot in enumerate(raw_slots, 1):
        raw_options = (raw_slot or {}).get("options") if isinstance(raw_slot, dict) else None
        options = []
        for raw in raw_options or []:
            if not isinstance(raw, dict) or not raw.get("base_id"):
                continue
            base_id = str(raw["base_id"])
            unit_type = known_units.get(base_id)
            if unit_type is None:
                errors.append(f"Слот {slot_index}: неизвестный юнит {base_id}.")
                continue
            if unit_type != combat_type:
                kind = "корабль" if unit_type == "ship" else "персонаж"
                errors.append(f"Слот {slot_index}: {base_id} — {kind}, не подходит к типу пака.")
                continue
            opt = {"base_id": base_id}
            for key, (lo, hi) in OPTION_INT_FIELDS.items():
                value = raw.get(key)
                if value in (None, ""):
                    continue
                try:
                    value = int(value)
                except (TypeError, ValueError):
                    errors.append(f"Слот {slot_index}: некорректное значение {key}.")
                    continue
                if not lo <= value <= hi:
                    errors.append(f"Слот {slot_index}: {key} должен быть от {lo} до {hi}.")
                    continue
                opt[key] = value
            for key in OPTION_BOOL_FIELDS:
                if raw.get(key):
                    opt[key] = True
            if opt.get("omicron") and omicron_capable is not None and base_id not in omicron_capable:
                opt.pop("omicron")
            if combat_type == "ship":
                for key in ("min_relic", "min_gear", "zeta", "omicron", "ultimate", "min_speed"):
                    opt.pop(key, None)
            options.append(opt)
        if options:
            slots.append({"options": options})
    if not slots:
        errors.append("Добавьте хотя бы один юнит.")
    if len(slots) > MAX_SLOTS[combat_type]:
        errors.append(f"Слотов не больше {MAX_SLOTS[combat_type]}.")
    return slots, errors


def squad_base_ids(squad: dict) -> set[str]:
    return {opt["base_id"] for slot in squad["slots"] for opt in slot["options"]}


def option_requirement_label(opt: dict, combat_type: str) -> str:
    """Короткая подпись требований варианта: "R7, 7★, зеты, омик, ульта, ск 300+"."""
    parts = []
    if opt.get("min_relic") is not None:
        parts.append(f"R{opt['min_relic']}")
    elif opt.get("min_gear") is not None:
        parts.append(f"G{opt['min_gear']}")
    if opt.get("min_stars") is not None and (combat_type == "ship" or opt["min_stars"] < 7):
        parts.append(f"{opt['min_stars']}★")
    if opt.get("zeta"):
        parts.append("зеты")
    if opt.get("omicron"):
        parts.append("омикрон")
    if opt.get("ultimate"):
        parts.append("ульта")
    if opt.get("min_speed"):
        parts.append(f"ск {opt['min_speed']}+")
    return ", ".join(parts)


# ---------------------------------------------------------------------------
# Ростер гильдии под набор паков
# ---------------------------------------------------------------------------
@dataclass
class Roster:
    ally_codes: list[str]
    names: dict[str, str]
    units: dict[str, dict[str, dict]]          # ally_code -> base_id -> rosterUnit
    unit_names: dict[str, str]
    unit_types: dict[str, str]
    skills: dict                               # skill_id -> (zeta_tier, omicron_tier, omicron_mode)
    stat_calc: object = None
    last_sync: str | None = None
    _speed_cache: dict = field(default_factory=dict)

    def unit_name(self, base_id: str) -> str:
        return self.unit_names.get(base_id) or base_id

    def speed(self, ally_code: str, base_id: str) -> float | None:
        if self.stat_calc is None:
            return None
        key = (ally_code, base_id)
        if key not in self._speed_cache:
            unit = self.units.get(ally_code, {}).get(base_id)
            try:
                self._speed_cache[key] = stat_engine.calc_final_stats(self.stat_calc, unit).get("Speed") if unit else None
            except Exception:
                self._speed_cache[key] = None
        return self._speed_cache[key]


def squads_need_speed(squads: list[dict]) -> bool:
    return any(opt.get("min_speed") for s in squads for slot in s["slots"] for opt in slot["options"])


def load_roster(guild_id: int, squads: list[dict], stat_calc=None, extra_base_ids=()) -> Roster:
    """Ростер текущего состава гильдии (user_mapping) только по юнитам, упомянутым в паках
    (+ extra_base_ids — юниты из уже сделанных назначений, для показа релика)."""
    mappings = database.get_all_user_mappings(guild_id)
    ally_codes = [ally_code for _discord_id, ally_code, _name in mappings]
    names = database.get_player_names(guild_id)
    for _discord_id, ally_code, ingame_name in mappings:
        if ingame_name:
            names[ally_code] = ingame_name
    base_ids = set(extra_base_ids)
    for s in squads:
        base_ids |= squad_base_ids(s)
    base_ids = sorted(base_ids)
    units: dict[str, dict[str, dict]] = {}
    for row in database.get_player_unit_owners_bulk(ally_codes, base_ids) if base_ids else []:
        units.setdefault(row["ally_code"], {})[row["base_id"]] = row["unit"]
    return Roster(
        ally_codes=sorted(ally_codes, key=lambda c: (names.get(c) or c).lower()),
        names=names,
        units=units,
        unit_names=database.get_game_unit_names(base_ids) if base_ids else {},
        unit_types=database.get_unit_types(base_ids) if base_ids else {},
        skills=database.get_all_skill_tier_info(),
        stat_calc=stat_calc,
        last_sync=database.get_player_units_last_sync(ally_codes),
    )


# ---------------------------------------------------------------------------
# Проверка одного юнита и подбор пака
# ---------------------------------------------------------------------------
def unit_relic(unit: dict) -> int:
    return stat_engine.get_current_relic_level(unit)


def unit_badge(unit: dict | None, unit_type: str) -> str:
    """"R7" / "G12" / "7★" — для портретов в списках."""
    if not unit:
        return ""
    if unit_type == "ship":
        return f"{unit.get('currentRarity', 0)}★"
    relic = unit_relic(unit)
    if (unit.get("currentTier") or 0) >= 13 and relic > 0:
        return f"R{relic}"
    return f"G{unit.get('currentTier') or 0}"


def _skills_ok(unit: dict, skills: dict, *, zeta: bool, omicron: bool) -> str | None:
    tiers = {s.get("id"): s.get("tier") or 0 for s in unit.get("skill") or []}
    if zeta:
        for skill_id, tier in tiers.items():
            zeta_tier = (skills.get(skill_id) or (None, None, None))[0]
            if zeta_tier is not None and tier < zeta_tier:
                return "не все зеты"
    if omicron:
        own = [(sid, skills.get(sid)) for sid in tiers if (skills.get(sid) or (None, None, None))[1] is not None]
        tw_own = [(sid, info) for sid, info in own if info[2] == "ВГ"]
        pool = tw_own or own
        if not pool:
            return None  # у юнита в игре нет омикрона — требование не к чему применить
        if not any(tiers[sid] >= info[1] for sid, info in pool):
            return "нет омикрона" + (" ВГ" if tw_own else "")
    return None


def check_option(roster: Roster, ally_code: str, opt: dict, combat_type: str) -> str | None:
    """None — юнит игрока проходит требования варианта, иначе короткая причина."""
    unit = roster.units.get(ally_code, {}).get(opt["base_id"])
    if unit is None:
        return "нет юнита"
    min_stars = opt.get("min_stars")
    if combat_type == "ship" and min_stars is None:
        min_stars = SHIP_DEFAULT_MIN_STARS
    stars = unit.get("currentRarity") or 0
    if min_stars is not None and stars < min_stars:
        return f"{stars}★ < {min_stars}★"
    if combat_type == "ship":
        return None
    gear = unit.get("currentTier") or 0
    if opt.get("min_relic") is not None:
        relic = unit_relic(unit)
        if gear < 13 or relic < opt["min_relic"]:
            have = f"R{relic}" if gear >= 13 else f"G{gear}"
            return f"{have} < R{opt['min_relic']}"
    if opt.get("min_gear") is not None and gear < opt["min_gear"]:
        return f"G{gear} < G{opt['min_gear']}"
    reason = _skills_ok(unit, roster.skills, zeta=bool(opt.get("zeta")), omicron=bool(opt.get("omicron")))
    if reason:
        return reason
    if opt.get("ultimate") and not unit.get("purchasedAbilityId"):
        return "нет ульты"
    if opt.get("min_speed"):
        speed = roster.speed(ally_code, opt["base_id"])
        if speed is None:
            return "скорость не посчитана"
        if speed < opt["min_speed"]:
            return f"скорость {int(speed)} < {opt['min_speed']}"
    return None


@dataclass
class SquadMatch:
    units: list[str] | None                    # выбранные base_id по слотам или None
    reason: str | None = None                  # почему не собирается
    blocked_by_plan: bool = False              # собрался бы, если бы не юниты, уже занятые в плане
    power: int = 0                             # сумма реликов (★ для флота) — для сортировки


def match_squad(roster: Roster, ally_code: str, squad: dict, blocked: set[str] = frozenset()) -> SquadMatch:
    """Подбирает юниты игрока в слоты пака: каждому слоту — первый по порядку подходящий
    вариант, не занятый ни другим слотом этого пака, ни другим паком плана (blocked).
    Перебор с возвратом — слотов ≤ 8, вариантов обычно 1-5, это мгновенно."""
    combat_type = squad["combat_type"]
    slots = squad["slots"]
    viable: list[list[str]] = []
    first_failure = None
    blocked_hit = False
    for slot in slots:
        ok_ids = []
        slot_reason = None
        for opt in slot["options"]:
            reason = check_option(roster, ally_code, opt, combat_type)
            if reason is None:
                if opt["base_id"] in blocked:
                    blocked_hit = True
                    slot_reason = slot_reason or f"{roster.unit_name(opt['base_id'])} уже в другом паке"
                    continue
                ok_ids.append(opt["base_id"])
            elif slot_reason is None:
                slot_reason = f"{roster.unit_name(opt['base_id'])}: {reason}"
        if not ok_ids and first_failure is None:
            first_failure = slot_reason or "нет подходящего юнита"
        viable.append(ok_ids)

    if first_failure is None:
        chosen: list[str] = []

        def dfs(i: int) -> bool:
            if i == len(viable):
                return True
            for base_id in viable[i]:
                if base_id in chosen:
                    continue
                chosen.append(base_id)
                if dfs(i + 1):
                    return True
                chosen.pop()
            return False

        if dfs(0):
            units = roster.units.get(ally_code, {})
            if combat_type == "ship":
                power = sum((units.get(b) or {}).get("currentRarity", 0) for b in chosen)
            else:
                power = sum(unit_relic(units[b]) for b in chosen if b in units)
            return SquadMatch(units=list(chosen), power=power)
        first_failure = "не хватает разных юнитов на все слоты"

    if blocked_hit:
        # Проверяем, собрался бы пак без учёта плана — тогда это "занят", а не "не может".
        free = match_squad(roster, ally_code, squad, frozenset())
        if free.units is not None:
            return SquadMatch(units=None, reason=first_failure, blocked_by_plan=True)
    return SquadMatch(units=None, reason=first_failure)


# ---------------------------------------------------------------------------
# Состояние плана и кандидаты
# ---------------------------------------------------------------------------
def used_units_by_player(assignments: list[dict]) -> dict[str, set[str]]:
    used: dict[str, set[str]] = {}
    for a in assignments:
        used.setdefault(a["ally_code"], set()).update(a["units"])
    return used


def squad_counts_by_player(assignments: list[dict]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for a in assignments:
        counts[a["ally_code"]] = counts.get(a["ally_code"], 0) + 1
    return counts


def evaluate_candidates(roster: Roster, squad: dict, assignments: list[dict], excluded=frozenset(),
                        library: list[dict] | None = None) -> list[dict]:
    """Все игроки ростера относительно пака в контексте плана:
    status "ok" (можно поставить), "used" (юниты уже заняты в плане), "excluded",
    "no" (не проходит требования). Для "ok" — units/power и conflict: сколько других
    паков библиотеки игрок сейчас может поставить, но потеряет, если взять этот —
    чем меньше, тем "дешевле" отдать игрока под этот пак (используется автоподбором)."""
    used = used_units_by_player(assignments)
    counts = squad_counts_by_player(assignments)
    others = [s for s in (library or []) if s["id"] != squad.get("id") and s["combat_type"] == squad["combat_type"]]
    result = []
    for ally_code in roster.ally_codes:
        entry = {
            "ally_code": ally_code,
            "name": roster.names.get(ally_code) or ally_code,
            "assigned": counts.get(ally_code, 0),
            "units": [],
            "reason": None,
            "power": 0,
            "conflict": 0,
        }
        if ally_code in excluded:
            entry["status"] = "excluded"
            entry["reason"] = "не участвует в этой ВГ"
            result.append(entry)
            continue
        blocked = used.get(ally_code, set())
        match = match_squad(roster, ally_code, squad, blocked)
        if match.units is not None:
            entry["status"] = "ok"
            entry["units"] = match.units
            entry["power"] = match.power
            if others:
                after = blocked | set(match.units)
                for other in others:
                    if match_squad(roster, ally_code, other, blocked).units is not None \
                            and match_squad(roster, ally_code, other, after).units is None:
                        entry["conflict"] += 1
        elif match.blocked_by_plan:
            entry["status"] = "used"
            entry["reason"] = match.reason
        else:
            entry["status"] = "no"
            entry["reason"] = match.reason
        result.append(entry)
    return result


def pick_order_key(c: dict):
    """Автоподбор: сначала те, кому этот пак меньше всего мешает поставить другие паки
    библиотеки, затем у кого меньше паков в плане (размазываем деф по гильдии), затем
    сильнее (сумма реликов)."""
    return (c["conflict"], c["assigned"], -c["power"], c["name"].lower())


def suggest(candidates: list[dict], count: int) -> list[dict]:
    ok = sorted((c for c in candidates if c["status"] == "ok"), key=pick_order_key)
    return ok[:max(0, count)]


def availability(roster: Roster, squad: dict, assignments: list[dict] = (), excluded=frozenset()) -> dict:
    """Сколько игроков может поставить пак: всего по требованиям и прямо сейчас с учётом плана."""
    used = used_units_by_player(assignments)
    can_total = 0
    can_now = 0
    for ally_code in roster.ally_codes:
        if ally_code in excluded:
            continue
        if match_squad(roster, ally_code, squad).units is None:
            continue
        can_total += 1
        if match_squad(roster, ally_code, squad, used.get(ally_code, set())).units is not None:
            can_now += 1
    return {"can_total": can_total, "can_now": can_now}


# ---------------------------------------------------------------------------
# Текст расстановки
# ---------------------------------------------------------------------------
def group_zone(assignments: list[dict], zone_key: str, names: dict) -> list[dict]:
    """[{squad_name, players: [name...], count}] по зоне, паки в порядке первого добавления."""
    groups: dict[str, dict] = {}
    for a in assignments:
        if a["zone"] != zone_key:
            continue
        g = groups.setdefault(a["squad_name"], {"squad_name": a["squad_name"], "players": [], "count": 0})
        g["players"].append(names.get(a["ally_code"]) or a["ally_code"])
        g["count"] += 1
    for g in groups.values():
        g["players"].sort(key=str.lower)
    return list(groups.values())


def format_plan_by_zone(plan: dict, assignments: list[dict], names: dict) -> str:
    lines = [f"**Деф ВГ: {plan['name']}**"]
    for zone in ZONES:
        groups = group_zone(assignments, zone["key"], names)
        if not groups:
            continue
        total = sum(g["count"] for g in groups)
        lines.append("")
        lines.append(f"__**{zone['label']}**__ ({total})")
        for g in groups:
            lines.append(f"**{g['squad_name']}** ×{g['count']}: {', '.join(g['players'])}")
    return "\n".join(lines)


def format_plan_by_player(plan: dict, assignments: list[dict], names: dict) -> str:
    by_player: dict[str, list[str]] = {}
    zone_order = {z["key"]: i for i, z in enumerate(ZONES)}
    for a in sorted(assignments, key=lambda a: (zone_order.get(a["zone"], 99), a["squad_name"])):
        label = ZONES_BY_KEY.get(a["zone"], {}).get("label", a["zone"])
        by_player.setdefault(a["ally_code"], []).append(f"{label} — {a['squad_name']}")
    lines = [f"**Деф ВГ: {plan['name']}** — по игрокам"]
    for ally_code in sorted(by_player, key=lambda c: (names.get(c) or c).lower()):
        lines.append(f"**{names.get(ally_code) or ally_code}**: " + "; ".join(by_player[ally_code]))
    return "\n".join(lines)
