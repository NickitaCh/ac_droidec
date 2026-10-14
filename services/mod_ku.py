"""МодКу — рейтинг качества модов аккаунта (запрос пользователя 2026-10-14, по мотивам спора
в TG-чате «Хвастайся» о формулах HotBot и Revan). Веб /mod-ku и команда /мод-ку.

Смысл числа: «сколько скорости в среднем даёт один мод на G13-персонаже» (атака
пересчитана в скорость). Считаются только вторички надетых модов:
  очки скорости = (скорость − 10) / 5, ниже 10 — 0          (15 → 1, 20 → 2, 25 → 3, 30 → 4)
  очки атаки    = (атака% × 3.5 − 10) / 5, ниже ~2.9% — 0     (4% → 0.8, 6% → 2.2, 8.5% → 4)
  мод = очки скорости + вес атаки × очки атаки
  МодКу = 10 + 5 × (сумма очков) / (число слотов)
Слоты — 6 на каждого G13-персонажа, минус стрелки с основой «скорость» (на них скорость в
допах не выпадает). Пустой слот даёт 0, поэтому МодКу не зависит от размера аккаунта, а
«выдроченные» 5 персонажей при пустом остальном складе рейтинг не вытянут.

1% атаки ≈ 3.5 скорости: максимум допа атаки 8.5% и скорости 30 — оба за 5 попаданий.

Вес атаки — два режима (решение пользователя 2026-10-14):
  standard — 0.5 у всех персонажей (атака нужна примерно половине ростера);
  plates   — по плейтам гильдии: 1 у персонажа, чьи плейты требуют урон/крит.урон/основу
             или сет атаки, 0 у персонажа, который в плейтах есть, но атака ему не нужна,
             0.5 у персонажа без плейтов.
Режим plates доступен только гильдиям с флагом mod_ku_plates (сейчас только АС); у
остальных выбора нет вообще, всегда standard."""

import database
from services import feature_flags
from services.mod_search import decode_mod

SPEED_STAT_ID = 5
OFFENSE_PCT_STAT_ID = 48
ATTACK_TO_SPEED = 3.5
BASE = 10
STEP = 5
DEFAULT_ATTACK_WEIGHT = 0.5

MODES = {
    "plates": "С учётом плейтов гильдии",
    "standard": "Обычный",
}
_MODE_KEY = "mod_ku:mode"

# Требования плейтов, по которым персонажу «нужна атака».
_OFFENSE_STATS = {"Physical Damage", "Special Damage", "Critical Damage",
                  "Physical Critical Chance", "Special Critical Chance"}
_OFFENSE_PRIMARY_IDS = {48, 16}  # основа: атака %, крит. урон %
_OFFENSE_SET_IDS = {"2", "6"}    # сет атаки, сет крит. урона


def speed_points(speed: float) -> float:
    return max(0.0, speed - BASE) / STEP


def attack_points(offense_pct: float) -> float:
    return max(0.0, offense_pct * ATTACK_TO_SPEED - BASE) / STEP


def to_rating(points: float, slots: int) -> float:
    return BASE + STEP * points / slots if slots else 0.0


# ---- режимы ----
def plates_mode_available(guild_id: int) -> bool:
    return feature_flags.is_enabled(guild_id, "mod_ku_plates")


def get_mode(guild_id: int) -> str:
    if not plates_mode_available(guild_id):
        return "standard"
    raw = database.get_bot_state(_MODE_KEY, guild_id=guild_id)
    return raw if raw in MODES else "plates"


def set_mode(guild_id: int, mode: str) -> None:
    if mode not in MODES or not plates_mode_available(guild_id):
        raise ValueError("Режим недоступен для этой гильдии.")
    database.set_bot_state(_MODE_KEY, mode, guild_id=guild_id)


def plate_attack_weights(guild_id: int) -> dict:
    """{base_id: вес атаки} по плейтам гильдии — см. docstring модуля."""
    import sqlite3
    conn = sqlite3.connect(database.DB_NAME)
    try:
        rows = conn.execute(
            "SELECT character_key, stat_name, threshold_value, skill_id FROM stat_requirements WHERE guild_id = ?",
            (guild_id,),
        ).fetchall()
    except sqlite3.OperationalError:
        rows = []
    finally:
        conn.close()
    weights = {}
    for base_id, stat_name, value, skill_id in rows:
        offensive = (
            stat_name in _OFFENSE_STATS
            or (stat_name == "ModPrimary" and int(value or 0) in _OFFENSE_PRIMARY_IDS)
            or (stat_name == "ModSet" and str(skill_id) in _OFFENSE_SET_IDS)
        )
        if offensive:
            weights[base_id] = 1.0
        else:
            weights.setdefault(base_id, 0.0)
    return weights


def attack_weights_for(guild_id: int, mode: str) -> dict | None:
    return plate_attack_weights(guild_id) if mode == "plates" else None


# ---- расчёт ----
def compute(units: dict, attack_weights: dict | None = None) -> dict | None:
    """units — {base_id: сырой rosterUnit} (player_unit_cache). attack_weights — {base_id: вес}
    для режима plates, None — у всех DEFAULT_ATTACK_WEIGHT. None, если нет G13-персонажей."""
    slots = speed_total = attack_total = 0.0
    chars = covered = 0
    speed_hist: dict[int, int] = {}
    attack_hist: dict[int, int] = {}
    speed_ge = {15: 0, 20: 0, 25: 0}
    offense_ge = {4: 0, 6: 0}
    per_char = []

    for base_id, unit in units.items():
        if unit.get("currentTier") != 13:
            continue
        chars += 1
        weight = DEFAULT_ATTACK_WEIGHT if attack_weights is None else attack_weights.get(base_id, DEFAULT_ATTACK_WEIGHT)
        char_slots = 6
        char_points = 0.0
        slow_slots = 0
        speed_slots_seen = 0
        for raw in unit.get("equippedStatMod") or []:
            mod = decode_mod(raw)
            if not mod:
                continue
            if mod["slot_key"] == "arrow" and mod["primary"] and mod["primary"]["stat_id"] == SPEED_STAT_ID:
                char_slots -= 1
                continue
            speed_slots_seen += 1
            speed = sum(s["value"] for s in mod["secondaries"] if s["stat_id"] == SPEED_STAT_ID)
            offense = sum(s["value"] for s in mod["secondaries"] if s["stat_id"] == OFFENSE_PCT_STAT_ID)
            sp, ap = speed_points(speed), attack_points(offense)
            speed_total += sp
            attack_total += weight * ap
            char_points += sp + weight * ap
            if speed < 15:
                slow_slots += 1
            if speed >= 10:
                speed_hist[int(speed)] = speed_hist.get(int(speed), 0) + 1
            for t in speed_ge:
                if speed >= t:
                    speed_ge[t] += 1
            if offense > 0:
                attack_hist[int(offense)] = attack_hist.get(int(offense), 0) + 1
            for t in offense_ge:
                if offense >= t:
                    offense_ge[t] += 1
        empty = char_slots - speed_slots_seen
        if slow_slots == 0 and empty <= 0:
            covered += 1
        slots += char_slots
        per_char.append({
            "base_id": base_id, "rating": to_rating(char_points, char_slots),
            "empty": max(0, empty), "slow": slow_slots, "attack_weight": weight,
        })

    if not chars:
        return None
    per_char.sort(key=lambda c: c["rating"])
    return {
        "modku": round(to_rating(speed_total + attack_total, slots), 2),
        "speed_part": round(STEP * speed_total / slots, 2),
        "attack_part": round(STEP * attack_total / slots, 2),
        "coverage": round(100 * covered / chars),
        "chars": chars,
        "slots": int(slots),
        "speed_ge": speed_ge,
        "offense_ge": offense_ge,
        "speed_hist": [
            {"speed": v, "count": n, "points": round(n * speed_points(v), 1)}
            for v, n in sorted(speed_hist.items(), reverse=True)
        ],
        "attack_hist": [{"pct": v, "count": n} for v, n in sorted(attack_hist.items(), reverse=True)],
        "weakest": per_char[:10],
    }


def compute_guild(guild_id: int, mode: str | None = None) -> dict:
    """Рейтинг всей гильдии по кэшу (без Comlink): {mode, rows: [{ally_code, name, ...compute}]},
    отсортировано по МодКу. Игроки без данных идут в конец с result=None."""
    mode = mode or get_mode(guild_id)
    weights = attack_weights_for(guild_id, mode)
    roster = database.get_all_user_mappings(guild_id)
    units_by_ally = database.get_player_units_bulk([code for _, code, _ in roster])
    rows = []
    for _discord_id, ally_code, name in roster:
        result = compute(units_by_ally.get(ally_code, {}), weights)
        rows.append({"ally_code": ally_code, "name": name, "result": result})
    rows.sort(key=lambda r: -(r["result"]["modku"] if r["result"] else -1))
    for place, row in enumerate(rows, 1):
        row["place"] = place
    return {"mode": mode, "rows": rows}


EXPLANATION = [
    "Каждый мод на G13-персонаже получает очки за скорость и % атаки в допах.",
    "Скорость: до 10 — ноль, дальше +1 очко за каждые 5 скорости (15 → 1, 20 → 2, 25 → 3, 30 → 4).",
    "Атака: 1% атаки = 3.5 скорости, дальше так же (4% → 0.8, 6% → 2.2, 8.5% → 4). "
    "Атака идёт с весом ½ — она нужна не всем.",
    "Очки делятся на все слоты G13-персонажей (пустой слот — ноль) и переводятся обратно в скорость.",
    "МодКу 16 = средний мод на твоих G13 даёт столько же, сколько мод с 16 скорости в допах.",
]
EXPLANATION_PLATES = (
    "Режим «С учётом плейтов»: атака идёт с весом 1 у персонажей, которым плейты гильдии "
    "требуют урон, крит. урон, основу или сет атаки, и не считается у тех, кому по плейтам "
    "атака не нужна. Персонажи без плейтов — вес ½."
)
