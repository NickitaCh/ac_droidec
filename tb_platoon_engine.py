# Общая логика "кто может донатить в этот слот взвода ТБ" — единая точка правды для
# ручной расстановки (web/routes/guild_dashboard.py::tb_platoons) и автозаполнения
# (tb_platoon_autofill.py). Раньше это было размазано прямо в теле tb_platoons() — вынесено
# сюда, когда автозаполнению потребовалась та же логика (см. план "Автозаполнение взводов
# ТБ + фильтры" от 2026-08-29), чтобы не дублировать правила в двух местах.
#
# Лимит "не больше N юнитов на планету от игрока" — ПО ЭТАПУ ПРОСМОТРА (round_num, как он
# записан при первом назначении слота, см. database.py::set_tb_platoon_assignment), не по
# планете целиком: та же планета, растянутая на 2 этапа, даёт игроку лимит заново на каждом
# этапе (прямое уточнение пользователя 2026-08-29 — иначе первый заполненный этап "съедал"
# бы весь лимит и для второго).
import tb_platoon_data

MAX_UNITS_PER_PLANET_PER_ROUND = 10


def visible_assignment(assignment, round_num: int):
    """"Round-aware" видимость (см. database.py::set_tb_platoon_assignment, 2026-08-30): донат
    считается сделанным на этапе round_num, только если assignment["round_num"] <= round_num —
    донат, который автозаполнение отложило на более поздний этап многоэтапной планеты (см.
    tb_platoon_autofill.py — "держим" теперь не убирает донора, а переносит его зачёт на
    последний этап планеты), на текущем более раннем этапе показывается как ещё пустой слот.
    Используется и веб-страницей (отображение/дедуп), и экспортом в HotUtils/EchoBase —
    единая точка правды, чтобы обе стороны согласованно решали "виден ли донат на этом этапе"."""
    if assignment is None:
        return None
    if assignment.get("round_num") is not None and assignment["round_num"] > round_num:
        return None
    return assignment

# Корабли не имеют реликвии в игре вообще (relic-система — только для персонажей) —
# донат-требование для них другое: 7★ (максимум звёздности), проверено по прямому запросу
# пользователя 2026-08-29. До этой правки relic-порог применялся ко всем юнитам без
# разбора — у корабля stat_engine.get_current_relic_level всегда возвращает 0 (нет ключа
# "relic" в player_unit), поэтому корабли ВСЕГДА проваливали relic-проверку и были
# полностью недоступны для автозаполнения/ручной расстановки — реальный баг, не только
# отсутствие фичи. is_ship берётся из database.get_unit_types (game_units.unit_type,
# заполняется из Comlink combatType в services/units_sync.py).
SHIP_MIN_STARS = 7


def compute_round_counts(assignments: dict, round_num: int) -> dict:
    """(ally_code, planet) -> сколько слотов этот игрок уже занимает на этой планете, где
    round_num назначения (см. database.py::get_tb_platoon_assignments) совпадает с
    переданным round_num. assignments — то, что отдаёт database.get_tb_platoon_assignments
    (ключ (planet, operation, slot_index))."""
    counts: dict = {}
    for (planet, _operation, _slot_index), assignment in assignments.items():
        if assignment.get("round_num") != round_num:
            continue
        key = (assignment["ally_code"], planet)
        counts[key] = counts.get(key, 0) + 1
    return counts


def compute_used_pairs(assignments: dict, planets_this_round: set, name_to_base_id: dict, round_num: int) -> dict:
    """(ally_code, base_id) -> (planet, operation, slot_index) для всех уже занятых слотов
    на планетах, показанных на текущем этапе просмотра/заполнения (planets_this_round) —
    в рамках ОДНОГО этапа игрок не может задонатить одного и того же юнита дважды (см.
    комментарий у web/routes/guild_dashboard.py::tb_platoons). name_to_base_id (имя юнита
    -> base_id) передаётся готовым — и веб-роут, и автозаполнение уже строят его сами
    (один batch-запрос на всех юнитов сразу, дважды резолвить не нужно).

    round_num — юниты у игрока обновляются КАЖДЫЙ этап (прямое уточнение пользователя
    2026-09-01: "в рамках этапа юзать одного юнита можно только один раз, но на следующем
    этапе... юниты обновляются... игрок может поставить юнитов во взводы ещё раз") — донат
    блокирует повторное использование того же (игрок, юнит) только В ТОМ ЖЕ round_num, что и
    сейчас просматривается; донат, зафиксированный на ДРУГОМ этапе (раньше или позже — оба
    исключены точным сравнением), для текущего этапа не в счёт. Раньше здесь стояла отсечка
    "<=" (донат из ПРОШЛОГО этапа тоже блокировал), из-за чего второй слот того же юнита на
    многоэтапной планете (её слоты хранятся под одним и тем же ключом на все её этапы, см.
    database.py::set_tb_platoon_assignment) навсегда считался недостижимым, хотя донор к
    этому моменту уже давно освободился — симметрично compute_round_counts, которая уже была
    сделана точно per-round по той же причине."""
    pairs: dict = {}
    for (planet, operation, slot_index), assignment in assignments.items():
        if planet not in planets_this_round:
            continue
        if assignment.get("round_num") is not None and assignment["round_num"] != round_num:
            continue
        unit_list = tb_platoon_data.ROTE_PLATOON_SUGGESTIONS.get((planet, operation)) or []
        if slot_index >= len(unit_list):
            continue
        base_id = name_to_base_id.get(unit_list[slot_index])
        if not base_id:
            continue
        pairs[(assignment["ally_code"], base_id)] = (planet, operation, slot_index)
    return pairs


def slot_candidates(
    *, owners: list, base_id: str, here: tuple, used_pairs: dict,
    min_relic, round_num: int, planet: str, filter_rules, round_counts: dict,
    is_ship: bool = False, max_per_planet_per_round: int = MAX_UNITS_PER_PLANET_PER_ROUND,
) -> list:
    """owners — [{"ally_code","name","relic","stars"}, ...] (уже топ-N по релику/★ — см.
    PLATOON_CANDIDATES_LIMIT в вызывающем коде, эта функция кандидатов не обрезает).
    here — (planet, operation, slot_index) слота, для которого считаем список — нужен,
    чтобы used_pairs не помечал "занят в другом месте" для СВОЕГО ЖЕ слота (переназначение).
    filter_rules — tb_platoon_filters.ParsedRules или None (без фильтров). is_ship — юнит в
    этом слоте корабль (database.get_unit_types) — тогда порог SHIP_MIN_STARS★ вместо
    min_relic, релик у кораблей не проверяется вообще (см. комментарий у SHIP_MIN_STARS).

    Возвращает owners, дополненные:
      meets_min — релик (или ★ для кораблей) проходит порог этапа
      used_elsewhere — тот же (игрок, юнит) уже стоит в ДРУГОМ слоте этого этапа
      used_at_label — где именно (человекочитаемо: планета/операция/слот), когда
               used_elsewhere True, иначе None — прямой запрос пользователя 2026-09-01
               ("надо писать ещё где именно он стоит"), раньше used_pairs знал локацию
               (planet, operation, slot_index), но наружу отдавался только булевый флаг
      excluded_by_filter — игрок исключён правилом "exclude player", ИЛИ сам юнит/его
               категория (флот/пешка) исключены правилом "exclude unit"/"exclude category"
               (объединено в один флаг 2026-09-01 — юнит исключён одинаково для ВСЕХ
               кандидатов слота, поэтому раньше это патчилось отдельно в вызывающем коде
               web/routes/guild_dashboard.py и tb_platoon_autofill.py; теперь единая точка
               правды здесь, дублирующие патчи убраны)
      at_cap — игрок уже занимает max_per_planet_per_round слотов на этой планете в этом
               round_num (лимит НЕ по планете целиком, см. модульный комментарий)
      count_here — сколько слотов на этой планете/round_num у игрока УЖЕ занято (для
                   сортировки при автозаполнении — размазывать нагрузку)
      is_ship — прокинуто как есть, для отображения/сортировки по вызывающей стороне."""
    unit_or_category_excluded = filter_rules is not None and (
        filter_rules.is_unit_excluded(base_id)
        or filter_rules.is_category_excluded("ship" if is_ship else "character")
    )
    result = []
    for o in owners:
        ally_code = o["ally_code"]
        used_at = used_pairs.get((ally_code, base_id))
        used_elsewhere = used_at is not None and used_at != here
        used_at_label = None
        if used_elsewhere:
            at_planet, at_operation, at_slot_index = used_at
            used_at_label = f"{at_planet}, операция {at_operation}, слот {at_slot_index + 1}"
        count_here = round_counts.get((ally_code, planet), 0)
        meets_min = o.get("stars", 0) >= SHIP_MIN_STARS if is_ship else (min_relic is not None and o["relic"] >= min_relic)
        result.append({
            **o,
            "meets_min": meets_min,
            "used_elsewhere": used_elsewhere,
            "used_at_label": used_at_label,
            "excluded_by_filter": unit_or_category_excluded or (filter_rules is not None and filter_rules.is_player_excluded(ally_code)),
            "at_cap": count_here >= max_per_planet_per_round,
            "count_here": count_here,
            "is_ship": is_ship,
            "is_priority": filter_rules is not None and filter_rules.is_player_priority(ally_code),
        })
    return result


def is_eligible(candidate: dict) -> bool:
    """Слот реально можно отдать этому кандидату — используется автозаполнением
    (tb_platoon_autofill.py) для отсева перед выбором победителя. Ручная расстановка на
    вебе использует эти же флаги напрямую в шаблоне (дизейблит кнопку, но не скрывает
    кандидата — офицер должен видеть, ПОЧЕМУ кандидат недоступен)."""
    return (
        candidate["meets_min"]
        and not candidate["used_elsewhere"]
        and not candidate["excluded_by_filter"]
        and not candidate["at_cap"]
    )


def pick_best_candidate(candidates: list, bundle_preferred_codes: frozenset = frozenset(), co_located_codes: frozenset = frozenset()):
    """Выбирает донора для автозаполнения из аннотированного списка (slot_candidates()).
    Приоритет: bundle-предпочтение (tb_platoon_filters.py::ParsedRules.bundles — тот же
    игрок, которому уже отдан юнит-триггер, см. tb_platoon_autofill.py) > приоритетный
    игрок (tb_platoon_filters.py::ParsedRules.priority_player_codes, правило "priority
    player [...]" — по прямому запросу пользователя 2026-08-31 "чтобы по возможности ему
    выдавалось максимальное кол-во взводов": приоритетный игрок обходит по очереди ЛЮБОГО
    неприоритетного кандидата независимо от релика/★, и НЕ штрафуется count_here —
    размазывание нагрузки специально отключено для него, чтобы не терять слоты в пользу
    менее нужных доноров) > кластеризация по операции (co_located_codes — игроки, у
    которых в ЭТОЙ ЖЕ (планета, операция) уже есть назначение в этом прогоне; передаётся
    готовым из tb_platoon_autofill.py, единого источника правды по assignments нет здесь —
    добавлено 2026-09-01 по прямому запросу пользователя "чтобы по возможности у большего
    количества игроков взводы были в одной операции", а не размазаны по разным операциям
    одной планеты; ставится ВЫШЕ релика намеренно — кандидат уже прошёл порог meets_min,
    так что "чуть меньше релик, но тот же игрок и та же операция" не хуже по факту, а
    удобнее в исполнении) > максимальный релик (★ для кораблей — у них нет реликвии,
    сортировка по звёздности вместо неё) > меньше всего уже занятых слотов на этой планете
    в этом этапе (размазывает нагрузку вместо того, чтобы наваливать на одного и того же
    топ-донора). Возвращает None, если подходящих кандидатов нет."""
    eligible = [c for c in candidates if is_eligible(c)]
    if not eligible:
        return None
    eligible.sort(key=lambda c: (
        0 if c["ally_code"] in bundle_preferred_codes else 1,
        0 if c.get("is_priority") else 1,
        0 if c["ally_code"] in co_located_codes else 1,
        -(c["stars"] if c["is_ship"] else c["relic"]),
        c["count_here"],
    ))
    return eligible[0]
