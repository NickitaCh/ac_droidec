import asyncio
import copy

from swgoh_comlink.StatCalc import StatCalc
from swgoh_comlink.StatCalc.data_builder import GameDataBuilder
from swgoh_comlink.helpers import MOD_SET_IDS


async def build_stat_calc(comlink) -> StatCalc:
    """Тянет statProgression/relicTierDefinition/equipment/... из Comlink и строит
    локальный калькулятор статов — тяжёлый блокирующий сетевой вызов, всегда через to_thread."""
    game_data = await asyncio.to_thread(lambda: GameDataBuilder(comlink).build())
    return StatCalc(game_data=game_data)


def project_unit_relic(unit: dict, target_relic_level: int) -> dict:
    """Копия юнита с подменённым уровнем реликвии (game-tier = уровень + 2);
    моды, шмот, уровень и редкость остаются реальными — не трогаются."""
    projected = copy.deepcopy(unit)
    relic = projected.setdefault("relic", {})
    relic["currentTier"] = target_relic_level + 2
    return projected


def get_current_relic_level(unit: dict) -> int:
    tier = (unit.get("relic") or {}).get("currentTier", 0)
    return max(0, tier - 2)


# StatCalc отдаёт статы-проценты (крит/устойч./эффект./броня и т.п.) долей (0.35), а не
# игровым процентом (35), как их пишут в HotUtils и как их вбивают в /статы_требования.
# Проверено эмпирически на реальном игроке (Armor=0.686 при показанных игрой ~68.6%).
PERCENT_STATS = frozenset({
    "Armor", "Resistance", "Potency", "Tenacity", "Critical Damage",
    "Physical Critical Chance", "Special Critical Chance", "Critical Chance",
    "Critical Avoidance", "Physical Critical Avoidance", "Special Critical Avoidance",
    "Accuracy", "Physical Accuracy", "Special Accuracy", "Health Steal",
    "Dodge Chance", "Deflection Chance",
})


def calc_final_stats(stat_calc: StatCalc, unit: dict) -> dict:
    """{"Health": ..., "Speed": ..., "Physical Damage": ..., ...} — не мутирует переданный unit.
    Статы из PERCENT_STATS домножены на 100, чтобы совпадать с игровым % (см. PERCENT_STATS)."""
    result = stat_calc.calc_char_stats(copy.deepcopy(unit))
    final = result["stats"]["final"]
    return {k: (v * 100 if k in PERCENT_STATS else v) for k, v in final.items()}


# Для веб-only "Конструктора" (/mod-builder, web/routes/stat_builder.py): гипотетическая
# сборка для персонажа, которого никто ещё не прокачал. StatCalc умеет синтетические моды
# вида {"set": id, "primaryStat": {...}, "secondaryStat": [...]}, но значения primary/
# secondary роллов нигде не подгружаются — GameDataBuilder не тянет у Comlink таблицу с
# этими значениями (только statProgression/equipment/statModSet/table/xpTable/
# relicTierDefinition/units/skill), а хардкодить общеизвестные "стандартные" роллы — это
# угадывание, которого проект избегает (см. память feedback_swgoh_statcalc_quirks).
# Поэтому здесь моделируется только точно посчитываемая часть — бонус сета (реальные
# данные modSetData/_mod_set_data): передаём калькулятору {"set": id} без primary/
# secondary — _calculate_mod_stats (swgoh_comlink) корректно считает комплекты сетов и
# добавляет их бонус, вклад от primary/secondary остаётся нулевым (безопасно, код не
# падает на stat=None). Вклад от самих роллов пользователь оценивает вручную —
# apply_manual_stat_totals ниже.
def build_hypothetical_unit(base_id: str, relic_level: int, set_counts: dict) -> dict:
    """set_counts: {mod_set_id (int, см. MOD_SET_IDS): количество надетых модов этого сета}.
    7★/85/шмот13 — как _build_synthetic_unit в cogs/stat_requirements.py (тот же смысл,
    не переиспользуется напрямую: тот вариант принципиально без модов)."""
    mods = [{"set": set_id} for set_id, count in set_counts.items() for _ in range(count)]
    return {
        "defId": base_id,
        "rarity": 7,
        "level": 85,
        "gear": 13,
        "equipped": [],
        "skills": [],
        "mods": mods,
        "relic": {"currentTier": relic_level + 2},
    }


def apply_manual_stat_totals(final_stats: dict, manual_totals: dict) -> dict:
    """final_stats — результат calc_final_stats. manual_totals — {stat_name: value},
    введённая пользователем оценка суммарного вклада 6 модов (primary+secondary роллы)
    по этому стату, в тех же единицах, что final_stats (проценты — в игровых %, не долях)."""
    result = dict(final_stats)
    for name, value in manual_totals.items():
        result[name] = result.get(name, 0) + value
    return result
