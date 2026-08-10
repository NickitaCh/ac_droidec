import asyncio
import copy

from swgoh_comlink.StatCalc import StatCalc
from swgoh_comlink.StatCalc.data_builder import GameDataBuilder


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
