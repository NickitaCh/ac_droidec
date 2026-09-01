"""Оптимизатор модов (веб /mod-optimizer) — по загруженному CSV со всеми модами игрока
(тот же формат, что отдаёт HotUtils-экспорт, см. чат-сессию 2026-09-01) подбирает для
каждого персонажа выбранных плейтов лучший набор из 6 модов из ОБЩЕГО пула (включая уже
надетые на других персонажах — пользователь явно разрешил трогать чужие моды, лишь бы
итоговые сеты были целыми, не "сломанными"), стараясь закрыть нормы stat_requirements.
Релик/шестерня/уровень берутся РЕАЛЬНЫЕ (из player_unit_cache/Comlink), не проецируются на
релик плейта — пользователь прямо просил "с учётом что они не в нужных реликах", т.е. что
достижимо ПРЯМО СЕЙЧАС, а не после гипотетического апа.

ГЛОБАЛЬНАЯ оптимизация (не жадная по персонажам — пользователь явно попросил 2026-09-01
"не жадный, а оптимальный, чтобы как можно больше плейтов прошло"): распределение общего
пула модов между десятками персонажей — это задача упаковки множеств (set packing),
NP-полная в общем случае, поэтому решается не перебором всего на свете, а сведением к
целочисленному линейному программированию (ILP, PuLP+CBC):
  1. generate_candidate_builds() для каждого персонажа перебирает ~50 валидных "форм" сетов
     (2+2+2 / 4+2, см. _target_multisets) и для каждой — не только лучший по эвристике набор
     конкретных модов, но и по паре альтернатив на каждой позиции (ALT_PER_SLOT) — это даёт
     солверу МАНЁВР: если лучший мод нужен другому персонажу, есть чем заменить, не проваливая
     всю сборку. Итоговые статы каждого кандидата — всегда настоящий stat_engine.calc_final_stats
     (тот же расчёт, что у /статы); эвристика используется только чтобы набрать короткий список
     кандидатов, не для итоговых цифр в отчёте.
  2. optimize_plates() строит ILP: одна бинарная переменная на (персонаж, кандидат-билд),
     "не больше одного билда на персонажа" + "каждый мод — не больше чем в одном выбранном
     билде сразу" (эксклюзивность пула), плюс derived-переменная на каждый плейт с
     ограничением platepass[p] <= charpass[c] для каждого персонажа c этого плейта
     (стандартный ILP-приём для AND: максимизация сама выставит platepass=1, когда это не
     нарушает ограничения — т.е. когда ВСЕ персонажи плейта прошли). Целевая функция —
     лексикографический приоритет одним линейным выражением через разные порядки весов:
     сначала число прошедших плейтов целиком (10000), затем число прошедших персонажей по
     отдельности (100), затем опциональные требования (1), и последним тай-брейком —
     минимизация суммарного дефицита у непройденных обязательных статов (0.001).
Если персонаж встречается в НЕСКОЛЬКИХ выбранных плейтах — у него физически один реальный
мод-билд в игре, поэтому его требования СЛИВАЮТСЯ (_merge_thresholds — берётся более строгий
порог там, где плейты расходятся), а не считаются дважды с разными наборами модов.

Это оптимум В ПРЕДЕЛАХ сгенерированных кандидатов на персонажа, не буквально по всей
астрономической вселенной комбинаций модов — на практике "хороший" билд под конкретное
сочетание сетов почти всегда среди топ-нескольких вариантов на каждый слот, но это не
строго доказанный глобальный максимум, а оптимум relative к тому, что было предложено ILP."""

import csv
import io
import itertools

import pulp

import stat_engine

# HotUtils slot-shape enum (см. reverse-engineering сессии 2026-09-01) — совпадает с
# библиотечной конвенцией swgoh_comlink (definitionId[2] = слот 1-6, см. calculator.py
# _calculate_mod_stats: 1=Square...6=Cross) и с stat_engine.MOD_SLOT_KEYS/MOD_SLOT_DEFS.
SHAPES = ["Square", "Arrow", "Diamond", "Triangle", "Circle", "Cross"]
SLOT_NAME_TO_ID = {name: i + 1 for i, name in enumerate(SHAPES)}

# swgoh_comlink.helpers.MOD_SET_IDS — {id_str: name}, имена совпадают буква-в-букву с тем,
# что HotUtils отдаёт (сверено эмпирически 2026-09-01 при разборе мод-экспорта).
from swgoh_comlink.helpers import MOD_SET_IDS  # noqa: E402

# HotUtils-экспорт пишет сеты БЕЗ пробела ("CriticalChance"/"CriticalDamage" — сверено на
# реальном CSV 2026-09-01), а swgoh_comlink.helpers.MOD_SET_IDS — с пробелом ("Critical
# Chance"). Нормализуем пробелы с обеих сторон, чтобы не зависеть от конкретного написания.
SET_NAME_TO_ID = {name.replace(" ", ""): int(sid) for sid, name in MOD_SET_IDS.items()}


def _lookup_set_id(name: str):
    return SET_NAME_TO_ID.get((name or "").replace(" ", ""))


TWO_PIECE_SETS = [sid for sid, count in stat_engine.MOD_SET_PIECE_COUNT.items() if count == 2]
FOUR_PIECE_SETS = [sid for sid, count in stat_engine.MOD_SET_PIECE_COUNT.items() if count == 4]

# CSV-имя стата (см. web/mods_export.csv, колонка primary_stat/secondaryN) -> (flat unitStatId,
# %-вариант unitStatId). CSV сам не различает флэт/% в имени (обе формы дают одно и то же имя
# через HotUtils-функцию _h(id, percent=false) — различие только в значении: "%" в конце строки
# значения означает %-вариант). None там, где такой формы у стата в игре не бывает.
STAT_NAME_TO_IDS = {
    "Health": (1, 55),
    "Protection": (28, 56),
    "Offense": (41, 48),
    "Defense": (42, 49),
    "Speed": (5, None),
    "Potency": (None, 17),
    "Tenacity": (None, 18),
    "Crit Chance": (None, 53),
    "Crit Dmg": (None, 16),
    "Crit Avoid": (None, 54),
    "Accuracy": (None, 52),
}

# unitStatId мода (сырой, "generic" — Offense/Defense/CritChance без разделения Physical/Special,
# см. swgoh_comlink calculator.py::_calculate_mod_stats — библиотека сама фанает id 41/42/48/49/53/54
# в оба финальных стата) -> какие ИТОГОВЫЕ имена статов (те же, что в stat_requirements.stat_name,
# см. cogs/stat_requirements.py::STAT_CHOICES) этот мод-стат потенциально двигает. Используется
# только для ЭВРИСТИЧЕСКОГО ранжирования кандидатов-модов, не для реального расчёта.
PROXY_HELPS = {
    1: ["Health"], 55: ["Health"],
    28: ["Protection"], 56: ["Protection"],
    5: ["Speed"],
    41: ["Physical Damage", "Special Damage"], 48: ["Physical Damage", "Special Damage"],
    42: ["Armor", "Resistance"], 49: ["Armor", "Resistance"],
    16: ["Critical Damage"],
    17: ["Potency"],
    18: ["Tenacity"],
    53: ["Physical Critical Chance", "Special Critical Chance"],
    54: ["Physical Critical Avoidance", "Special Critical Avoidance"],
    52: ["Physical Accuracy", "Special Accuracy"],
}

# Сколько альтернативных модов держать на каждую пару (форма,сет) при генерации кандидатов —
# компромисс между манёвром для ILP (больше = меньше шанс, что персонажа "заблокируют" по
# редкому слоту) и временем счёта (каждая альтернатива — либо новый реальный calc_final_stats,
# либо минимум лишняя запись в модели солвера). См. generate_candidate_builds.
ALT_PER_SLOT = 20
MAX_CANDIDATES_PER_CHAR = 60


class ModCsvError(Exception):
    pass


def _parse_stat_cell(name: str, value_raw: str):
    """'Offense','5.88%' -> {'unit_stat_id':48,'unscaled':5880000,'value':5.88,'is_percent':True}.
    'Speed','30' -> unit_stat_id=5, unscaled=3000000000, value=30.0, is_percent=False. Возвращает
    None на пустой ячейке (нижние secondary-слоты мода ниже 5★ пустые — это ОЖИДАЕМО, не ошибка)."""
    name = (name or "").strip()
    value_raw = (value_raw or "").strip()
    if not name or not value_raw:
        return None
    is_percent = value_raw.endswith("%")
    value = float(value_raw[:-1] if is_percent else value_raw)
    ids = STAT_NAME_TO_IDS.get(name)
    if ids is None:
        raise ModCsvError(f"Неизвестное название стата в файле: «{name}»")
    unit_stat_id = ids[1] if is_percent else ids[0]
    if unit_stat_id is None:
        raise ModCsvError(f"У стата «{name}» не бывает {'%-варианта' if is_percent else 'плоского варианта'}, а в файле он такой")
    # Обратное преобразование к HotUtils-формуле форматирования (см. сессию 2026-09-01):
    # display = trunc(statValueDecimal/1e4) для флэт-статов, statValueDecimal/100 для %-статов;
    # unscaledDecimalValue = statValueDecimal*10000 (эмпирически подтверждено на реальном моде).
    # Точность на ~4-м знаке теряется (statValueDecimal в исходном JSON уже целый) — на порогах
    # плейтов (круглые числа) это ни на что не влияет.
    unscaled = round(value * 1_000_000) if is_percent else round(value * 100_000_000)
    return {"unit_stat_id": unit_stat_id, "unscaled": unscaled, "value": value, "is_percent": is_percent, "name": name}


def parse_mods_csv(raw_bytes: bytes) -> list[dict]:
    """Разбирает CSV (те же колонки, что build_and_download в browser-сессии 2026-09-01, см.
    /mod-optimizer шаблон за подсказкой формата) в список мод-записей. Бросает ModCsvError с
    понятным текстом на первой же нераспознанной строке/стате — лучше честно отказать, чем
    молча посчитать неправильно."""
    try:
        text = raw_bytes.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise ModCsvError("Файл не в UTF-8 — экспортируйте заново со страницы HotUtils.")
    reader = csv.DictReader(io.StringIO(text))
    required_cols = {"id", "level", "set", "slot", "locked", "equipped_to", "primary_stat", "primary_value"}
    missing = required_cols - set(reader.fieldnames or [])
    if missing:
        raise ModCsvError(f"В файле нет колонок: {', '.join(sorted(missing))} — это точно экспорт модов с /mod-optimizer?")

    mods = []
    for i, row in enumerate(reader, start=2):  # 2 = с учётом строки заголовка
        set_name = (row.get("set") or "").strip()
        slot_name = (row.get("slot") or "").strip()
        set_id = _lookup_set_id(set_name)
        slot_id = SLOT_NAME_TO_ID.get(slot_name)
        if set_id is None or slot_id is None:
            raise ModCsvError(f"Строка {i}: не распознан сет «{set_name}» или слот «{slot_name}».")
        try:
            primary = _parse_stat_cell(row.get("primary_stat"), row.get("primary_value"))
        except ModCsvError as e:
            raise ModCsvError(f"Строка {i} (primary): {e}")
        if primary is None:
            raise ModCsvError(f"Строка {i}: у мода нет primary-стата.")
        secondaries = []
        for n in (1, 2, 3, 4):
            try:
                sec = _parse_stat_cell(row.get(f"secondary{n}"), row.get(f"secondary{n}_value"))
            except ModCsvError as e:
                raise ModCsvError(f"Строка {i} (secondary{n}): {e}")
            if sec is not None:
                sec["rolls"] = int(row.get(f"secondary{n}_rolls") or 0)
                secondaries.append(sec)

        equipped_to = (row.get("equipped_to") or "").strip() or None
        mods.append({
            "id": row.get("id"),
            "level": int(row.get("level") or 15),
            "rarity": int(row.get("rarity_dots") or 6),
            "tier": int(row.get("tier_pips") or 1),  # calculator.py::_calc_char_gp требует mod["tier"] (KeyError без него)
            "set_id": set_id,
            "set_name": MOD_SET_IDS[str(set_id)],  # каноничное написание с пробелом — для отображения
            "slot_id": slot_id,
            "slot_name": slot_name,
            "locked": (row.get("locked") or "").strip().lower() == "true",
            "equipped_to": equipped_to,
            "primary": primary,
            "secondaries": secondaries,
        })
    if not mods:
        raise ModCsvError("Файл пуст — ни одного мода не распознано.")
    return mods


def _to_equipped_stat_mod(mod: dict) -> dict:
    """swgoh_comlink равнодушен ко 2-й цифре definitionId (rarity) — читает только [0]=сет и
    [2]=слот (см. calculator.py::_calculate_mod_stats), но держим её реальной для аккуратности.
    "tier" (пипсы 1-5) обязателен отдельным полем — calc_char_stats падает KeyError без него
    (используется в _calc_char_gp для подсчёта GP, найдено на живом деплое 2026-09-01)."""
    def _stat_entry(s):
        return {"stat": {"unitStatId": s["unit_stat_id"], "unscaledDecimalValue": s["unscaled"]}}
    return {
        "definitionId": f"{mod['set_id']}{mod['rarity']}{mod['slot_id']}",
        "level": mod["level"],
        "tier": mod["tier"],
        "primaryStat": _stat_entry(mod["primary"]),
        "secondaryStat": [_stat_entry(s) for s in mod["secondaries"]],
    }


def _target_multisets():
    """Все валидные 6-модовые "формы" (см. докстринг файла — только целые сеты): три 2-piece
    сета (с повторами — тот же сет дважды = 4 мода этого сета, тоже валидно) или один 4-piece +
    один 2-piece. ~50 комбинаций суммарно."""
    combos = []
    for combo in itertools.combinations_with_replacement(TWO_PIECE_SETS, 3):
        multiset = []
        for sid in combo:
            multiset += [sid] * 2
        combos.append(multiset)
    for four in FOUR_PIECE_SETS:
        for two in TWO_PIECE_SETS:
            combos.append([four] * 4 + [two] * 2)
    return combos


def _score_mod(mod: dict, unmet_weight: dict) -> float:
    score = 0.0
    for stat in [mod["primary"]] + mod["secondaries"]:
        for final_name in PROXY_HELPS.get(stat["unit_stat_id"], []):
            w = unmet_weight.get(final_name)
            if w:
                score += stat["value"] * w
    # Тай-брейк при равном эвристическом счёте: больше прокачанных вторичек — лучше мод.
    score += 0.001 * sum(s.get("rolls", 0) for s in mod["secondaries"])
    return score


def _unmet_weights(base_stats: dict, thresholds: list[dict]) -> dict:
    """{final_stat_name: вес} для статов, которые ЕЩЁ не закрыты голой базой (шестерня+релик+
    звёздность, без модов) — вес = 1/порог, чтобы разномасштабные статы (тысячи HP vs единицы %)
    вносили сопоставимый вклад в эвристический скор. ">="-требования, уже закрытые базой (редко,
    но бывает у перекачанных персонажей), и "<="/"="-требования в подбор не включаем — добавление
    модов их не улучшает (или не является целью подбора)."""
    weights = {}
    for req in thresholds:
        name, operator, threshold = req["stat_name"], req["operator"], req["threshold"]
        if operator != ">=" or name == "Relic":
            continue
        current = base_stats.get(name)
        if current is not None and current >= threshold:
            continue
        weights[name] = 1.0 / max(abs(threshold), 1e-6)
    return weights


def _compare(current: float, operator: str, threshold: float) -> bool:
    if operator == ">=":
        return current >= threshold
    if operator == "<=":
        return current <= threshold
    return current == threshold


def generate_candidate_builds(stat_calc, real_unit: dict, thresholds: list[dict], pool: list[dict]) -> list[dict]:
    """thresholds: [{'stat_name','operator','threshold','priority'}, ...] (без строки Relic).
    pool: ВЕСЬ пул, доступный для этого персонажа (глобальный, минус уже разобранное другими —
    исключение по локальности решает вызывающий optimize_plates). Возвращает до
    MAX_CANDIDATES_PER_CHAR словарей: {'mod_ids': frozenset, 'mods': [...], 'final_stats': {...},
    'full_pass': bool, 'required_matched': int, 'optional_matched': int, 'deficit': float}, лучшие
    (по full_pass, потом required_matched, потом optional_matched, потом -deficit) первыми — но
    ILP-солвер сам решает, какой из них реально использовать (может быть не первый в списке,
    если его моды нужнее другому персонажу)."""
    base_stats = stat_engine.calc_base_stats(stat_calc, real_unit)
    unmet_weight = _unmet_weights(base_stats, thresholds)
    total_required = sum(1 for r in thresholds if r["priority"] == "required")

    pool_by_shape_set: dict[tuple[int, int], list[dict]] = {}
    for m in pool:
        pool_by_shape_set.setdefault((m["slot_id"], m["set_id"]), []).append(m)
    ranked: dict[tuple[int, int], list[dict]] = {
        key: sorted(cands, key=lambda m: -_score_mod(m, unmet_weight))[:ALT_PER_SLOT]
        for key, cands in pool_by_shape_set.items()
    }

    seen_mod_id_sets: set[frozenset] = set()
    raw_candidates: list[dict] = []

    def evaluate(assignment):
        """assignment: [(shape_id, set_id, mod), ...] длиной 6."""
        mods_used = [m for _, _, m in assignment]
        mod_ids = frozenset(m["id"] for m in mods_used)
        if mod_ids in seen_mod_id_sets:
            return
        seen_mod_id_sets.add(mod_ids)

        unit = dict(real_unit)
        unit["equippedStatMod"] = [_to_equipped_stat_mod(m) for m in mods_used]
        final_stats = stat_engine.calc_final_stats(stat_calc, unit)

        required_matched = sum(1 for r in thresholds if r["priority"] == "required"
                                and final_stats.get(r["stat_name"]) is not None
                                and _compare(final_stats[r["stat_name"]], r["operator"], r["threshold"]))
        optional_matched = sum(1 for r in thresholds if r["priority"] != "required"
                                and final_stats.get(r["stat_name"]) is not None
                                and _compare(final_stats[r["stat_name"]], r["operator"], r["threshold"]))
        deficit = 0.0
        for r in thresholds:
            cur = final_stats.get(r["stat_name"])
            if cur is None or r["operator"] != ">=" or cur >= r["threshold"]:
                continue
            deficit += (r["threshold"] - cur) / max(abs(r["threshold"]), 1e-6)

        raw_candidates.append({
            "mod_ids": mod_ids, "mods": mods_used, "final_stats": final_stats,
            "full_pass": required_matched == total_required,
            "required_matched": required_matched, "optional_matched": optional_matched, "deficit": deficit,
        })

    for multiset in _target_multisets():
        best_perm, best_perm_score = None, float("-inf")
        seen_perms = set()
        for perm in itertools.permutations(multiset):
            if perm in seen_perms:
                continue
            seen_perms.add(perm)
            total, feasible = 0.0, True
            for shape_idx, set_id in enumerate(perm):
                cands = ranked.get((shape_idx + 1, set_id))
                if not cands:
                    feasible = False
                    break
                total += _score_mod(cands[0], unmet_weight)
            if feasible and total > best_perm_score:
                best_perm_score, best_perm = total, perm
        if best_perm is None:
            continue  # этой формы не собрать из доступного пула вообще

        base_assignment = [(shape_idx + 1, set_id, ranked[(shape_idx + 1, set_id)][0]) for shape_idx, set_id in enumerate(best_perm)]
        evaluate(base_assignment)

        # k-сдвиг: k-й по рангу мод СРАЗУ на всех 6 позициях, не по одной — иначе соседние
        # "альтернативы" отличаются одним модом из шести и всё равно конфликтуют друг с другом
        # почти целиком, когда два персонажа с похожими нормами реально претендуют на одну и ту
        # же форму сетов (сама причина, по которой жадная версия проваливала второго персонажа
        # даже при обилии модов в пуле — см. чат-сессию 2026-09-01, тест на двух одинаковых
        # персонажах). k-сдвиг даёт СЛАБО ПЕРЕСЕКАЮЩИЕСЯ билды — то, что реально нужно ILP для
        # разруливания конкуренции за один и тот же тип сборки между несколькими персонажами.
        for k in range(1, ALT_PER_SLOT):
            shifted, feasible = [], True
            for shape_idx, set_id in enumerate(best_perm):
                cands = ranked.get((shape_idx + 1, set_id), [])
                if k >= len(cands):
                    feasible = False
                    break
                shifted.append((shape_idx + 1, set_id, cands[k]))
            if feasible:
                evaluate(shifted)

        # Точечные однопозиционные свопы вокруг лучшего билда — доводка на случай, когда
        # контендится только один конкретный слот, а не вся форма сетов целиком.
        for pos in range(6):
            shape_id, set_id, _ = base_assignment[pos]
            for alt in ranked.get((shape_id, set_id), [])[1:3]:
                alt_assignment = list(base_assignment)
                alt_assignment[pos] = (shape_id, set_id, alt)
                evaluate(alt_assignment)

    raw_candidates.sort(key=lambda c: (c["full_pass"], c["required_matched"], c["optional_matched"], -c["deficit"]), reverse=True)
    return raw_candidates[:MAX_CANDIDATES_PER_CHAR]


def _merge_thresholds(rows_by_plate: dict) -> list[dict]:
    """Персонаж встречается в нескольких выбранных плейтах -> у него один реальный мод-билд в
    игре, поэтому требования из всех плейтов сливаются в одно (строже там, где расходятся),
    а не считаются отдельно под каждый плейт со своим набором модов. rows_by_plate:
    {plate_name: [(id, plate_name, character_key, stat_name, operator, threshold, priority,
    raw_text, comment, created_by, created_at), ...]}."""
    merged: dict[tuple[str, str], dict] = {}
    for rows in rows_by_plate.values():
        for r in rows:
            stat_name, operator, threshold, priority = r[3], r[4], r[5], r[6]
            if stat_name == "Relic":
                continue
            key = (stat_name, operator)
            cur = merged.get(key)
            if cur is None:
                merged[key] = {"stat_name": stat_name, "operator": operator, "threshold": threshold, "priority": priority}
                continue
            if operator == ">=":
                cur["threshold"] = max(cur["threshold"], threshold)
            elif operator == "<=":
                cur["threshold"] = min(cur["threshold"], threshold)
            if priority == "required":
                cur["priority"] = "required"
    return list(merged.values())


async def _get_unit_for_player(comlink, ally_code: str, base_id: str, force_refresh: bool):
    """Копия cogs/stat_requirements.py::_get_unit_for_player — та версия завязана на bot.comlink,
    здесь comlink передаётся явно (веб-процесс не имеет доступа к объекту bot, тот же паттерн,
    что services/stat_forecast.py::_bot_stand_in)."""
    import database
    from services import activity_diff

    if force_refresh:
        units = await activity_diff.fetch_player_units(comlink, ally_code)
        if units:
            database.upsert_player_units(ally_code, units)
        unit = units.get(base_id) if units else None
        if unit:
            return unit, "только что"
    cached = database.get_player_unit(ally_code, base_id)
    if cached:
        return cached
    return None, None


def _build_ilp(candidates_by_char: dict, plate_members: dict, char_owned: dict):
    """Возвращает (prob, y, charpass_expr, platepass) — см. докстринг файла за объяснением
    модели. Чистая функция от уже посчитанных кандидатов, без комлинка/БД — отдельно, чтобы
    можно было unit-тестировать на моках без реального StatCalc."""
    prob = pulp.LpProblem("mod_optimizer", pulp.LpMaximize)
    y = {}
    for base_id, cands in candidates_by_char.items():
        for idx in range(len(cands)):
            y[(base_id, idx)] = pulp.LpVariable(f"y_{base_id}_{idx}", cat="Binary")
        if cands:
            prob += pulp.lpSum(y[(base_id, idx)] for idx in range(len(cands))) <= 1

    mod_usage: dict[str, list] = {}
    for base_id, cands in candidates_by_char.items():
        for idx, cand in enumerate(cands):
            for mid in cand["mod_ids"]:
                mod_usage.setdefault(mid, []).append(y[(base_id, idx)])
    for vars_ in mod_usage.values():
        if len(vars_) > 1:
            prob += pulp.lpSum(vars_) <= 1

    charpass_expr = {
        base_id: pulp.lpSum(y[(base_id, idx)] for idx, cand in enumerate(cands) if cand["full_pass"])
        for base_id, cands in candidates_by_char.items()
    }

    platepass = {}
    for plate, members in plate_members.items():
        if not members or any(not char_owned.get(m) for m in members):
            continue  # хотя бы один персонаж плейта не открыт — плейт физически не может пройти целиком
        pv = pulp.LpVariable(f"plate_{plate}", cat="Binary")
        for base_id in members:
            prob += pv <= charpass_expr[base_id]
        platepass[plate] = pv

    W_PLATE, W_CHAR, W_OPT, W_DEF = 10000, 100, 1, 0.001
    objective = pulp.lpSum(W_PLATE * v for v in platepass.values())
    objective += pulp.lpSum(W_CHAR * expr for expr in charpass_expr.values())
    for base_id, cands in candidates_by_char.items():
        for idx, cand in enumerate(cands):
            objective += y[(base_id, idx)] * (W_OPT * cand["optional_matched"] - W_DEF * cand["deficit"])
    prob += objective
    return prob, y, charpass_expr, platepass


async def optimize_plates(comlink, stat_calc, plate_names: list[str], ally_code: str, all_mods: list[dict], guild_id: int = 1, force_refresh: bool = False) -> list[dict]:
    """Главная точка входа для веб-роута. Собирает персонажей всех выбранных плейтов (с
    объединёнными требованиями, если персонаж встречается в нескольких — см.
    _merge_thresholds), генерирует кандидатов-билдов на каждого (generate_candidate_builds) и
    решает ОДНИМ ILP, кому что достаётся (см. докстринг файла). Возвращает список словарей —
    один на персонажа, в порядке "как отдаёт БД" (порядок больше не влияет на результат, в
    отличие от старой жадной версии — влияет только на порядок вывода)."""
    import asyncio
    import database

    plate_members: dict[str, list[str]] = {}
    rows_by_char: dict[str, dict] = {}
    for plate in plate_names:
        chars = database.get_stat_requirement_characters(plate, guild_id=guild_id)
        plate_members[plate] = chars
        for base_id in chars:
            rows_by_char.setdefault(base_id, {})[plate] = database.get_stat_requirements(plate, base_id, guild_id=guild_id)

    char_info = {}
    for base_id, rows_by_plate in rows_by_char.items():
        thresholds = _merge_thresholds(rows_by_plate)
        relic_vals = [int(r[5]) for rows in rows_by_plate.values() for r in rows if r[3] == "Relic"]
        unit, updated_at = await _get_unit_for_player(comlink, ally_code, base_id, force_refresh)
        char_info[base_id] = {
            "char_name": database.get_game_unit_name(base_id) or base_id,
            "plates": sorted(rows_by_plate.keys()),
            "unit": unit, "owned": unit is not None,
            "thresholds": thresholds,
            "required_relic": max(relic_vals) if relic_vals else None,
            "current_relic": stat_engine.get_current_relic_level(unit) if unit else None,
            "updated_at": updated_at,
        }

    pool = [m for m in all_mods if not m["locked"]]

    def _gen_all():
        return {
            base_id: generate_candidate_builds(stat_calc, info["unit"], info["thresholds"], pool)
            for base_id, info in char_info.items() if info["owned"]
        }

    candidates_by_char = await asyncio.to_thread(_gen_all)

    char_owned = {base_id: info["owned"] for base_id, info in char_info.items()}
    prob, y, charpass_expr, platepass = _build_ilp(candidates_by_char, plate_members, char_owned)
    await asyncio.to_thread(prob.solve, pulp.PULP_CBC_CMD(msg=False, timeLimit=90))
    solved_ok = pulp.LpStatus[prob.status] in ("Optimal", "Not Solved")  # "Not Solved" = уткнулись в timeLimit, но CBC отдаёт лучшее найденное

    plates_passed = {p for p, v in platepass.items() if pulp.value(v) and pulp.value(v) > 0.5}

    results = []
    for base_id, info in char_info.items():
        stat_thresholds = info["thresholds"]
        if not info["owned"]:
            results.append({
                "plate": ", ".join(info["plates"]), "base_id": base_id, "char_name": info["char_name"], "owned": False,
                "current_relic": None, "required_relic": info["required_relic"], "ok": False,
                "passed": [], "failed": stat_thresholds, "used_mods": [],
                "reason": "Юнит не открыт у игрока либо ещё не синхронизирован.",
            })
            continue

        cands = candidates_by_char.get(base_id, [])
        chosen = None
        for idx, cand in enumerate(cands):
            var = y.get((base_id, idx))
            if var is not None and (pulp.value(var) or 0) > 0.5:
                chosen = cand
                break

        if chosen is None:
            passed, failed, mods_used, reason = [], stat_thresholds, [], (
                "В общем пуле не нашлось модов нужных сетов/форм для этого персонажа." if not cands
                else "Солвер не выделил этому персонажу ни одного билда — все подходящие моды нужнее другим персонажам плейтов (не хватает свободных модов на всех)."
            )
        else:
            final_stats = chosen["final_stats"]
            passed, failed = [], []
            for r in stat_thresholds:
                cur = final_stats.get(r["stat_name"])
                ok = cur is not None and _compare(cur, r["operator"], r["threshold"])
                (passed if ok else failed).append({**r, "current": cur})
            mods_used = chosen["mods"]
            reason = None

        results.append({
            "plate": ", ".join(info["plates"]), "base_id": base_id, "char_name": info["char_name"], "owned": True,
            "current_relic": info["current_relic"], "required_relic": info["required_relic"],
            "relic_below": info["required_relic"] is not None and info["current_relic"] < info["required_relic"],
            "ok": chosen is not None, "passed": passed, "failed": failed,
            "used_mods": mods_used, "reason": reason, "updated_at": info["updated_at"],
        })

    return results, plates_passed, solved_ok
