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


def calc_base_stats(stat_calc: StatCalc, unit: dict) -> dict:
    """Статы юнита ДО модов (без сетов/primary) — {"Health": ..., "Armor": <Defense-рейтинг>,
    ...}, не мутирует unit. В отличие от calc_final_stats НЕ домножает PERCENT_STATS на 100 —
    id 8/9 (Armor/Resistance) тут переведены обратно в сырой Defense-рейтинг (round-trip через
    _armor_pct_to_defense/_defense_to_armor_pct — тот же приём, что и в apply_manual_stat_totals
    ниже), не итоговый %. Нужна для apply_manual_stat_totals/required_manual_contribution —
    %-вторички считаются от ЭТОЙ базы, не от final.

    Реализовано через calc_final_stats на копии юнита БЕЗ единого мода (equippedStatMod=[]),
    а НЕ через result["stats"]["base"] — swgoh_comlink.StatCalc собирает ответ в "game style"
    формате (_FIXED_GAME_STYLE=True — хардкод-константа класса в самой библиотеке, не
    настраивается), при котором calc_char_stats вообще не возвращает промежуточный подсловарь
    "base" (только "final") — прямое чтение result["stats"]["base"] падало KeyError на любом
    реальном обращении к /mod-builder (прод-инцидент, найден 2026-08-24 сразу после того как
    эта функция была впервые добавлена — см. git log)."""
    modless_unit = copy.deepcopy(unit)
    modless_unit["equippedStatMod"] = []
    base = calc_final_stats(stat_calc, modless_unit)
    for name in NONLINEAR_DEFENSE_STATS:
        if name in base:
            base[name] = _armor_pct_to_defense(base[name])
    return base


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
#
# ИСПРАВЛЕНО 2026-08-24: circle/cross были перепутаны местами. Оригинальная (2026-08-19)
# версия сознательно ПЕРЕВЕРНУЛА нумерацию слотов библиотеки (`swgoh_comlink.helpers.
# MOD_SLOTS`: 6=Circle, 7=Plus/Cross) на 6=Cross, 7=Circle, обосновав это тем, что "Potency+
# Tenacity — Circle-эксклюзив, вне разумных сомнений". Это предположение оказалось неверным
# (или как минимум не тем швом, где на самом деле проходит граница слотов) — офицер
# гильдии (NicolozZ, тред "Гайд по АС Боту" в #ас-задачи) реально протестировал инструмент
# против настоящих модов в игре и сообщил "круг и крест местами перепутаны", независимо
# подтверждено пользователем в этой же сессии. Фикс — вернулись к нумерации библиотеки
# (не перепроверяли сырые данные повторным сканом player_unit_cache, т.к. единственное,
# что изменилось — это ЯРЛЫК слота 6 vs 7, сами пары (слот, primary-стат) из скана
# 2026-08-19 не тронуты). Если этот вывод когда-нибудь понадобится перепроверить — не
# доверять "общеизвестным" таблицам Circle/Cross из памяти, сверяться с реальными данными.
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
    "circle": [
        {"label": "Health +16%", "unit_stat": 55, "unscaled_value": "16000000"},
        {"label": "Protection +24%", "unit_stat": 56, "unscaled_value": "24000000"},
    ],
    "cross": [
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


def build_hypothetical_unit(base_id: str, relic_level: int, set_counts: dict, primary_picks: list | None = None, rarity: int = 7) -> dict:
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
    Звёздность (rarity) настраиваема (1-7★, по умолчанию 7★ — как раньше, до 2026-08-24)
    отдельным параметром формы, наравне с релик-уровнем; 85/шмот13 — как _build_synthetic_unit
    в cogs/stat_requirements.py (тот же смысл, не переиспользуется напрямую: тот вариант
    принципиально без модов)."""
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
        "rarity": rarity,
        "level": 85,
        "gear": 13,
        "equipped": [],
        "skills": [],
        "equippedStatMod": mods,
        "relic": {"currentTier": relic_level + 2},
    }


# Armor/Resistance — НЕ независимые % статы, а нелинейное производное от внутреннего
# рейтинга "Defense" (swgoh_comlink/StatCalc/calculator.py::_convert_flat_def_to_percent):
# armor% = defense / (level_effect + defense), level_effect = level * 7.5 для персонажей.
# build_hypothetical_unit всегда строит юнита на 85 уровне (см. выше) => level_effect = 637.5.
# Формула/константа 637.5 сверены с пользователем (2026-08-24) и совпали один в один с тем,
# что реально использует StatCalc.
#
# На модах "Defense" (unitStatId 49, тот же, что у %-primary "Defense +20%" в
# MOD_PRIMARY_OPTIONS) — это процент от БАЗОВОГО Defense-рейтинга (id 8/9), а не от уже
# отображаемой Брони/Сопротивления и не независимая "броня сама по себе" — подтверждено
# чтением исходников _calculate_mod_stats: id 49 обрабатывается ТЕМ ЖЕ способом, что и id
# 48/55/56/57 (Offense%/Health%/Protection%/Speed%, см. PERCENT_OF_BASE_STATS ниже) —
# `base_stats[8] * value`, просто база тут — Defense-рейтинг, а не сам финальный стат
# (тот появляется только ПОСЛЕ одной итоговой нелинейной конвертации). Поэтому ввод для
# Armor/Resistance — это тоже "% от базы", но базой служит Defense-рейтинг ДО этой
# конвертации, а не отображаемый %, и итог требует round-trip через
# _armor_pct_to_defense/_defense_to_armor_pct вместо прямого умножения (первая версия
# 2026-08-24 ошибочно трактовала ввод как независимый армор%, конвертируемый в Defense и
# складываемый — поправлено по уточнению пользователя тем же днём).
_ARMOR_LEVEL_EFFECT = 85 * 7.5
NONLINEAR_DEFENSE_STATS = frozenset({"Armor", "Resistance"})


def _armor_pct_to_defense(pct: float) -> float:
    pct = min(pct, 99.999)
    return (pct * _ARMOR_LEVEL_EFFECT) / (100 - pct)


def _defense_to_armor_pct(defense: float) -> float:
    return (defense * 100) / (defense + _ARMOR_LEVEL_EFFECT)


# Health/Protection/Physical Damage/Special Damage — на модах тоже реально встречаются как
# %-вторички (Health%/Protection%/Offense%, тот же unitStatId, что у %-primary в
# MOD_PRIMARY_OPTIONS: 55/56/48), а не только флэт-числом. Подтверждено чтением исходников
# swgoh_comlink/StatCalc/calculator.py::_calculate_mod_stats: вклад id 48/55/56/57
# (Offense%/Health%/Protection%/Speed%) считается как процент от БАЗОВОГО (до модов)
# значения соответствующего стата, а не от уже посчитанного финала — но т.к. со всех 6
# модов такие % сначала суммируются в одну цифру и только потом одним движением
# применяются к базе (не компаундятся друг на друга), результат эквивалентен применению
# процента к уже готовому final_stats ДО текущей ручной вторички (после primary-статов,
# которые в этом инструменте уже посчитаны отдельно через сам StatCalc) — тот же принцип,
# что уже используется для Armor/Resistance выше. Speed сюда не входит: у модов
# технически есть unitStatId для Speed% (57), но в игре им ни разу не пользуются — Speed
# реально катается только флэт-числом, поэтому остаётся вводом в штуках.
PERCENT_OF_BASE_STATS = frozenset({"Health", "Protection", "Physical Damage", "Special Damage"})


def apply_manual_stat_totals(final_stats: dict, manual_totals: dict, base_stats: dict) -> dict:
    """final_stats — результат calc_final_stats (сеты+primary+relic+звёздность, БЕЗ ручных
    вторичек). base_stats — результат calc_base_stats ТОГО ЖЕ юнита (статы без единого мода) —
    именно от неё, а не от final_stats, считается %-вклад вторичек ниже (см. why). manual_totals
    — {stat_name: value}, введённая пользователем оценка суммарного вклада ВТОРИЧНЫХ статов
    модов (единственное, что действительно нельзя посчитать точно — вторички рандомны, у них
    нет фиксированной таблицы значений, в отличие от primary-статов выше). Все статы, кроме
    Speed, вводятся в игровых % (см. PERCENT_OF_BASE_STATS/NONLINEAR_DEFENSE_STATS выше и
    PERCENT_STATS для родных %-статов StatCalc) — Speed остаётся числом (штуками), т.к.
    вторичка Speed% в игре не встречается.

    Почему процент берётся от base_stats, а не от final_stats (баг, найденный в #ас-задачи
    2026-08-24 NicolozZ — Кайло считало 180к HP вместо верных 174к): игра сама складывает ВСЕ
    %-источники (primary/сет/вторички) в один коэффициент и применяет его к сырой базе ОДИН
    раз — final = raw_base * (1 + primary_pct + сет_pct + вторичка_pct). final_stats уже равен
    raw_base * (1 + primary_pct + сет_pct); если умножить его ЕЩЁ раз на (1 + вторичка_pct), это
    компаундит (final*(1+в) = raw_base*(1+p+s)*(1+в) != raw_base*(1+p+s+в)) и завышает итог.
    Эквивалентно: правильная прибавка от вторички — raw_base * вторичка_pct (плюсом к
    final_stats), не final_stats * вторичка_pct.

    Armor/Resistance — особый случай (NONLINEAR_DEFENSE_STATS): вклад — процент от
    базового Defense-рейтинга (не от самой Брони/Сопротивления), пересчитывается через
    промежуточный "Defense"-рейтинг тем же линейным сложением (не умножением).
    Health/Protection/Physical Damage/Special Damage (PERCENT_OF_BASE_STATS): вклад —
    процент от СЫРОГО (до модов) значения стата, переводится в штуки и прибавляется
    к final_stats."""
    result = dict(final_stats)
    for name, value in manual_totals.items():
        if name in NONLINEAR_DEFENSE_STATS:
            raw_base_defense = base_stats.get(name, 0)
            current_defense = _armor_pct_to_defense(result.get(name, 0))
            result[name] = _defense_to_armor_pct(current_defense + raw_base_defense * (value / 100))
        elif name in PERCENT_OF_BASE_STATS:
            raw_base = base_stats.get(name, 0)
            result[name] = result.get(name, 0) + raw_base * (value / 100)
        else:
            result[name] = result.get(name, 0) + value
    return result


def required_manual_contribution(base_value: float, target_value: float, stat_name: str, raw_base: float) -> float:
    """Обратная функция к apply_manual_stat_totals для ОДНОГО стата: сколько нужно набрать
    в вторичках (в тех же единицах, что и manual_totals там — % для всего, кроме Speed),
    чтобы поднять stat_name с base_value (final_stats ДО apply_manual_stat_totals — сеты+
    primary+relic+звёздность) до target_value. raw_base — calc_base_stats того же стата
    (статы БЕЗ единого мода) — та же база, от которой считается %-вклад в
    apply_manual_stat_totals (см. её докстринг за подробным разбором бага); базовая
    для % и raw_base для % — разные числа, если на юните уже есть %-primary/сет-бонусы,
    поэтому нужны оба параметра. Если target_value уже достигнут на одной базе — возвращает
    0 (а не отрицательное число)."""
    if target_value <= base_value:
        return 0.0
    if stat_name in NONLINEAR_DEFENSE_STATS:
        raw_base_defense = raw_base
        if raw_base_defense <= 0:
            return 0.0
        current_defense = _armor_pct_to_defense(base_value)
        target_defense = _armor_pct_to_defense(target_value)
        return (target_defense - current_defense) / raw_base_defense * 100
    if stat_name in PERCENT_OF_BASE_STATS:
        if raw_base <= 0:
            return 0.0
        return (target_value - base_value) / raw_base * 100
    return target_value - base_value
