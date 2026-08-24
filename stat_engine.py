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
    """Копия юнита с подменённым уровнем реликвии (game-tier = уровень + 2). Любой релик-тир
    в игре требует шестерню G13 (relic.currentTier не бывает > 0 на юните ниже G13), поэтому
    шестерня (unit["currentTier"] — верхнеуровневый ключ, отдельный от relic["currentTier"])
    тоже поднимается до 13 — иначе прогноз считался на реальной, часто более низкой, шестерне
    игрока и занижал статы (Health/Speed) относительно того, чем юнит реально будет на целевом
    релике. Найдено 2026-08-23 в #ac-тестовая на Эзре Бриджере: /статы давало ~81к HP на релик 8,
    C3PO — ~100к. Моды и уровень остаются реальными — не трогаются; конкретные детали шмота G13
    (equipment) при этом не подставляются — их набор у игрока неизвестен, статы досчитываются
    только по табличному бонусу тира шестерни, не по конкретным деталям."""
    projected = copy.deepcopy(unit)
    relic = projected.setdefault("relic", {})
    relic["currentTier"] = target_relic_level + 2
    projected["currentTier"] = 13
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
# сборка модов для персонажа, которого никто ещё не прокачал.
#
# ПЕРВИЧНЫЕ статы модов (primary) на 6★ уровне 15 — это фиксированные игровые константы
# (не роллы), но Comlink их нигде не отдаёт отдельной таблицей (GameDataBuilder тянет
# только statProgression/equipment/statModSet/table/xpTable/relicTierDefinition/units/
# skill — ни один из них не содержит значений primary-статов). Поэтому 2026-08-19 значения
# ниже (MOD_PRIMARY_OPTIONS) добыты эмпирически: просканированы реальные равные 6★/ур.15
# моды всей гильдии из player_unit_cache (91576 модов) — для каждой пары (форма слота,
# primary-стат) взято реальное значение unscaledDecimalValue из настоящего экипированного
# мода игрока (формат equippedStatMod, тот же, что даёт Comlink для реальных игроков и уже
# используется в /статы). Значение оказалось идентично на тысячах образцов по каждой паре
# (детерминированная константа, не ролл) — подтверждено кросс-проверкой (Defense+20%
# повторяется на Arrow/Triangle/Circle с одним и тем же unscaledDecimalValue). Список
# статов на слот — ТОЛЬКО то, что реально встретилось хотя бы раз в гильдии; теоретически
# легальные, но никем не отфармленные комбинации (например Arrow/Triangle умеют больше
# опций в игре) сюда не добавлены — не гадаем про то, чего не увидели своими глазами.
#
# unscaled_value передаётся в equippedStatMod.primaryStat.stat.unscaledDecimalValue
# напрямую, без пересчёта — это ровно то, что тот же код StatCalc уже читает для реальных
# модов игроков (см. calc_final_stats), поэтому Health%/Protection%/Offense%/Defense%
# (проценты от БАЗОВОГО стата персонажа) корректно масштабируются под конкретного
# персонажа сами, без ручного пересчёта с нашей стороны — в отличие от плоских
# статов (Speed/Potency/Tenacity/крит.), которые для любого персонажа одинаковы.
MOD_PRIMARY_OPTIONS = {
    "square": [
        {"label": "Offense +8.5%", "unit_stat": 48, "unscaled_value": "8500000"},
    ],
    "arrow": [
        {"label": "Speed +32", "unit_stat": 5, "unscaled_value": "3200000000"},
        {"label": "Offense +8.5%", "unit_stat": 48, "unscaled_value": "8500000"},
        {"label": "Defense +20%", "unit_stat": 49, "unscaled_value": "20000000"},
        {"label": "Accuracy +30%", "unit_stat": 52, "unscaled_value": "30000000"},
        {"label": "Critical Avoidance +35%", "unit_stat": 54, "unscaled_value": "35000000"},
        {"label": "Health +16%", "unit_stat": 55, "unscaled_value": "16000000"},
        {"label": "Protection +24%", "unit_stat": 56, "unscaled_value": "24000000"},
    ],
    "diamond": [
        {"label": "Defense +20%", "unit_stat": 49, "unscaled_value": "20000000"},
    ],
    "triangle": [
        {"label": "Critical Damage +42%", "unit_stat": 16, "unscaled_value": "42000000"},
        {"label": "Offense +8.5%", "unit_stat": 48, "unscaled_value": "8500000"},
        {"label": "Defense +20%", "unit_stat": 49, "unscaled_value": "20000000"},
        {"label": "Critical Chance +20%", "unit_stat": 53, "unscaled_value": "20000000"},
        {"label": "Health +16%", "unit_stat": 55, "unscaled_value": "16000000"},
        {"label": "Protection +24%", "unit_stat": 56, "unscaled_value": "24000000"},
    ],
    "cross": [
        {"label": "Health +16%", "unit_stat": 55, "unscaled_value": "16000000"},
        {"label": "Protection +24%", "unit_stat": 56, "unscaled_value": "24000000"},
    ],
    "circle": [
        {"label": "Potency +30%", "unit_stat": 17, "unscaled_value": "30000000"},
        {"label": "Tenacity +35%", "unit_stat": 18, "unscaled_value": "35000000"},
        {"label": "Offense +8.5%", "unit_stat": 48, "unscaled_value": "8500000"},
        {"label": "Defense +20%", "unit_stat": 49, "unscaled_value": "20000000"},
        {"label": "Health +16%", "unit_stat": 55, "unscaled_value": "16000000"},
        {"label": "Protection +24%", "unit_stat": 56, "unscaled_value": "24000000"},
    ],
}

# Сколько модов сета нужно для бонуса — из реальных данных игры (modSetData/statModSet,
# добыто 2026-08-19 через comlink.get_game_data(items=StatMod), поле setCount), не угадано.
MOD_SET_PIECE_COUNT = {
    1: 2,  # Health
    2: 4,  # Offense
    3: 2,  # Defense
    4: 4,  # Speed
    5: 2,  # Critical Chance
    6: 4,  # Critical Damage
    7: 2,  # Potency
    8: 2,  # Tenacity
}


def build_hypothetical_unit(base_id: str, relic_level: int, set_counts: dict, primary_picks: list | None = None) -> dict:
    """set_counts: {mod_set_id (int, см. MOD_SET_IDS): количество модов этого сета}.
    primary_picks: список словарей из MOD_PRIMARY_OPTIONS (или None на пустых позициях) —
    один на физический мод. Сет и primary комбинируются позиционно в один и тот же список
    из 6 equippedStatMod-объектов, но что именно с чем совпало, не важно для итога: подсчёт
    бонуса сета (по count) и вклад primary-статов (по unscaledDecimalValue) в
    _calculate_mod_stats (swgoh_comlink) полностью независимы и просто складываются —
    поэтому 6 слотов в форме и 8 счётчиков сетов ниже могут быть независимыми виджетами,
    без привязки "какой сет реально в каком слоте".
    tier=1 — не влияет на статы (только на GP, которую эта фича не показывает), нужен
    просто чтобы calc_char_stats не упал на KeyError при попутном подсчёте GP.
    Позиция без сета получает definitionId с первой цифрой "0" — такого setId не существует
    в modSetData, _calculate_mod_stats тихо не начисляет бонус (не падает), ровно то, что
    нужно для "выбран primary, но сет для этого мода ещё не назначен".
    7★/85/шмот13 — как _build_synthetic_unit в cogs/stat_requirements.py (тот же смысл,
    не переиспользуется напрямую: тот вариант принципиально без модов)."""
    primary_picks = list(primary_picks or [])
    slot_sets = [set_id for set_id, count in set_counts.items() for _ in range(count)][:6]
    n = min(max(len(primary_picks), len(slot_sets)), 6)

    mods = []
    for i in range(n):
        set_digit = slot_sets[i] if i < len(slot_sets) else 0
        mod = {"definitionId": f"{set_digit}61", "level": 15, "tier": 1}
        pick = primary_picks[i] if i < len(primary_picks) else None
        if pick:
            mod["primaryStat"] = {"stat": {"unitStatId": pick["unit_stat"], "unscaledDecimalValue": pick["unscaled_value"]}}
        mods.append(mod)

    return {
        "defId": base_id,
        "rarity": 7,
        "level": 85,
        "gear": 13,
        "equipped": [],
        "skills": [],
        "equippedStatMod": mods,
        "relic": {"currentTier": relic_level + 2},
    }


def apply_manual_stat_totals(final_stats: dict, manual_totals: dict) -> dict:
    """final_stats — результат calc_final_stats. manual_totals — {stat_name: value},
    введённая пользователем оценка суммарного вклада ВТОРИЧНЫХ статов модов (единственное,
    что действительно нельзя посчитать точно — вторички рандомны, у них нет фиксированной
    таблицы значений, в отличие от primary-статов выше), в тех же единицах, что
    final_stats (проценты — в игровых %, не долях)."""
    result = dict(final_stats)
    for name, value in manual_totals.items():
        result[name] = result.get(name, 0) + value
    return result
