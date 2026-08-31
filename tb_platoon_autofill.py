# Автозаполнение донат-слотов взводов ТБ по всему сохранённому плану сразу (все
# распознанные этапы/планеты, не только один открытый этап) — см. план "Автозаполнение
# взводов ТБ + фильтры" от 2026-08-29. Использует ту же логику кандидатов на слот, что и
# ручная расстановка на /tb/platoons (tb_platoon_engine.py) — единая точка правды для
# relic-порога, дедупа "юнит+игрок не дважды за этап", фильтров (tb_platoon_filters.py) и
# лимита 10/planet/round.
#
# Уже занятые слоты (в т.ч. перенесённые с прошлого этапа — см. database.py::
# _ensure_tb_platoon_assignments_table) НЕ трогаются — автозаполнение добавляет донатов
# только в пустые слоты.
import dataclasses

import database
import stat_engine
import tb_plan_reader
import tb_platoon_data
import tb_platoon_engine
import tb_platoon_filters


@dataclasses.dataclass
class SlotOutcome:
    round_num: int
    planet: str
    operation: int
    slot_index: int
    unit: str
    reason: str  # "unit_not_resolved" / "unit_excluded" / "no_owner" / "no_eligible_owner" / "held_back"


@dataclasses.dataclass
class AutofillResult:
    total_slots: int = 0
    already_filled: int = 0
    newly_filled: int = 0
    held_back: int = 0
    unfilled: list = dataclasses.field(default_factory=list)
    by_round: dict = dataclasses.field(default_factory=dict)  # round_num -> {"total": int, "filled": int}

    @property
    def filled_slots(self) -> int:
        return self.already_filled + self.newly_filled


async def autofill_plan(guild_id: int, plan_id: int, dry_run: bool = False) -> AutofillResult:
    """dry_run=True считает результат, не записывая в БД (не используется пока нигде — для
    предпросмотра, если понадобится позже; логика не завязана на сайд-эффекты)."""
    plan = database.get_tb_saved_plan(plan_id)
    if not plan or plan["guild_id"] != guild_id:
        raise ValueError("План не найден")

    entries, fetch_error = await tb_plan_reader.fetch_plan_planets(plan)
    if fetch_error:
        raise RuntimeError(fetch_error)

    by_round: dict = {}
    seen = set()
    for e in entries:
        key = (e["round"], e["planet"] or f"raw:{e['raw']}")
        if key in seen:
            continue
        seen.add(key)
        by_round.setdefault(e["round"], []).append(e)
    planets_by_round = {rn: {e["planet"] for e in es if e["planet"]} for rn, es in by_round.items()}

    # Планета, растянутая на 2+ этапа, встречается в нескольких round_entries — донат-слоты
    # у неё физически ОДНИ и те же (см. database.py::_ensure_tb_platoon_assignments_table),
    # поэтому для сборки списка ещё пустых слотов планета берётся один раз, а не по разу на
    # каждый этап, где она показана (иначе один и тот же слот попал бы в список дважды под
    # разными round_num). round_num, под которым заполняется НОВЫЙ слот такой планеты — её
    # ПЕРВОЕ появление в плане (тот же принцип, что и "round_num — этап первого назначения"
    # у самих записей БД).
    planet_first_round: dict = {}
    ordered_planets: list = []
    for round_num in sorted(by_round):
        for e in by_round[round_num]:
            if not e["planet"]:
                continue
            if e["planet"] not in planet_first_round:
                planet_first_round[e["planet"]] = round_num
                ordered_planets.append(e["planet"])

    filter_rules, _errors = tb_platoon_filters.parse_rules(database.get_tb_platoon_filter_rules(guild_id), guild_id)
    hold_flags = database.get_tb_platoon_holds(guild_id, plan_id)

    unit_names = {
        name
        for planet in ordered_planets
        for operation in range(1, 7)
        for name in (tb_platoon_data.ROTE_PLATOON_SUGGESTIONS.get((planet, operation)) or [])
    }
    name_to_base_id = database.resolve_unit_display_names(list(unit_names)) if unit_names else {}

    mappings = database.get_all_user_mappings(guild_id)
    ally_codes = [ally_code for _discord_id, ally_code, _name in mappings]
    player_name_by_ally = {ally_code: name for _discord_id, ally_code, name in mappings}
    base_ids = sorted({bid for bid in name_to_base_id.values() if bid})
    # Корабли не имеют реликвии (см. tb_platoon_engine.SHIP_MIN_STARS) — донат-требование
    # для них 7★, не порог реликвии этапа.
    unit_types = database.get_unit_types(base_ids) if base_ids else {}
    owners_raw = database.get_player_unit_owners_bulk(ally_codes, base_ids) if base_ids else []
    owners_by_base_id: dict = {}
    for row in owners_raw:
        owners_by_base_id.setdefault(row["base_id"], []).append({
            "ally_code": row["ally_code"],
            "name": player_name_by_ally.get(row["ally_code"], row["ally_code"]),
            "relic": stat_engine.get_current_relic_level(row["unit"]),
            "stars": row["unit"].get("currentRarity", 0),
        })
    for base_id, owners in owners_by_base_id.items():
        is_ship = unit_types.get(base_id) == "ship"
        owners.sort(key=lambda o: -(o["stars"] if is_ship else o["relic"]))
        # Автозаполнению нужен ПОЛНЫЙ пул кандидатов, без обрезки в отличие от веб-страницы
        # (там PLATOON_CANDIDATES_LIMIT=20 — чисто UI-потолок на число кнопок) — иначе юнит
        # с 25 живыми владельцами ошибочно посчитается дефицитным на шаге сортировки.

    assignments = database.get_tb_platoon_assignments(guild_id, plan_id)
    # unified — существующие назначения + те, что решит сделать автозаполнение в этом
    # прогоне; пишутся в БД сразу по мере выбора (если не dry_run), чтобы used_pairs/
    # round_counts на следующих шагах уже видели их.
    unified = dict(assignments)

    all_slots = []
    for planet in ordered_planets:
        round_num = planet_first_round[planet]
        for operation in range(1, 7):
            unit_list = tb_platoon_data.ROTE_PLATOON_SUGGESTIONS.get((planet, operation)) or []
            for slot_index, unit_name in enumerate(unit_list):
                key = (planet, operation, slot_index)
                if key in unified:
                    continue
                all_slots.append({
                    "round_num": round_num, "planet": planet, "operation": operation,
                    "slot_index": slot_index, "unit_name": unit_name,
                    "base_id": name_to_base_id.get(unit_name),
                })

    result = AutofillResult(total_slots=len(all_slots) + len(assignments), already_filled=len(assignments))

    def eligible_count(slot: dict) -> int:
        base_id = slot["base_id"]
        if not base_id or filter_rules.is_unit_excluded(base_id):
            return -1  # обрабатываем первыми — заведомо не заполнимо, не тратим время на пересчёт позже
        owners = owners_by_base_id.get(base_id, [])
        round_num = slot["round_num"]
        used_pairs = tb_platoon_engine.compute_used_pairs(unified, planets_by_round[round_num], name_to_base_id, round_num)
        round_counts = tb_platoon_engine.compute_round_counts(unified, round_num)
        min_relic = tb_platoon_data.ROTE_MIN_RELIC_BY_PLANET.get(slot["planet"])
        candidates = tb_platoon_engine.slot_candidates(
            owners=owners, base_id=base_id, here=(slot["planet"], slot["operation"], slot["slot_index"]),
            used_pairs=used_pairs, min_relic=min_relic, round_num=round_num, planet=slot["planet"],
            filter_rules=filter_rules, round_counts=round_counts, is_ship=unit_types.get(base_id) == "ship",
        )
        return sum(1 for c in candidates if tb_platoon_engine.is_eligible(c))

    # "Самое ограниченное — первым": дефицитность считается ОДИН раз по исходному
    # состоянию (до заполнения чего-либо в этом прогоне), а не пересчитывается на каждом
    # шаге — полный пересчёт used_pairs/round_counts после каждого заполненного слота дал
    # бы точнее результат, но на масштабе полного плана (~800-1200 слотов) это O(N²); текущий
    # компромисс — O(N log N + N·кандидатов) — редкие связки всё равно уходят вперёд частых.
    ranked = sorted(range(len(all_slots)), key=lambda i: (eligible_count(all_slots[i]), i))

    for i in ranked:
        slot = all_slots[i]
        base_id = slot["base_id"]
        round_num, planet, operation, slot_index = slot["round_num"], slot["planet"], slot["operation"], slot["slot_index"]
        here = (planet, operation, slot_index)

        if not base_id:
            result.unfilled.append(SlotOutcome(round_num, planet, operation, slot_index, slot["unit_name"], "unit_not_resolved"))
            continue
        if filter_rules.is_unit_excluded(base_id):
            result.unfilled.append(SlotOutcome(round_num, planet, operation, slot_index, slot["unit_name"], "unit_excluded"))
            continue

        owners = owners_by_base_id.get(base_id, [])
        used_pairs = tb_platoon_engine.compute_used_pairs(unified, planets_by_round[round_num], name_to_base_id, round_num)
        round_counts = tb_platoon_engine.compute_round_counts(unified, round_num)
        min_relic = tb_platoon_data.ROTE_MIN_RELIC_BY_PLANET.get(planet)
        candidates = tb_platoon_engine.slot_candidates(
            owners=owners, base_id=base_id, here=here, used_pairs=used_pairs,
            min_relic=min_relic, round_num=round_num, planet=planet,
            filter_rules=filter_rules, round_counts=round_counts, is_ship=unit_types.get(base_id) == "ship",
        )

        # bundle-предпочтение (tb_platoon_filters.py::ParsedRules.bundles): если этот юнит
        # входит в чей-то пул, и триггер этого пула уже кому-то назначен (в т.ч. в этом же
        # прогоне) — этому игроку отдаётся приоритет, чтобы не дробить пачку.
        bundle_preferred = set()
        for trigger_base_id, pool in filter_rules.bundles.items():
            if base_id not in pool:
                continue
            for (p2, op2, idx2), a2 in unified.items():
                unit_list2 = tb_platoon_data.ROTE_PLATOON_SUGGESTIONS.get((p2, op2)) or []
                if idx2 >= len(unit_list2):
                    continue
                if name_to_base_id.get(unit_list2[idx2]) == trigger_base_id:
                    bundle_preferred.add(a2["ally_code"])

        best = tb_platoon_engine.pick_best_candidate(candidates, frozenset(bundle_preferred))
        if best is None:
            reason = "no_owner" if not owners else "no_eligible_owner"
            result.unfilled.append(SlotOutcome(round_num, planet, operation, slot_index, slot["unit_name"], reason))
            continue

        unified[here] = {"ally_code": best["ally_code"], "assigned_by": "autofill", "round_num": round_num}
        if not dry_run:
            database.set_tb_platoon_assignment(
                guild_id, plan_id, round_num, planet, operation, slot_index, best["ally_code"], assigned_by="autofill",
            )
        result.newly_filled += 1

    # "Держим": планета, которая появляется в плане ещё и на более позднем этапе, ещё не
    # зачищена целиком — по прямому запросу пользователя 2026-08-29 ("на 3 этапе есть
    # Датомир и на 4 этапе есть Датомир — значит на 3 этапе взводы не нужно заполнять
    # полностью"), АВТОМАТИЧЕСКИ, без ручного тумблера. Плюс ручной флаг (database.py::
    # get_tb_platoon_holds) — для планеты на единственном этапе, которую офицер решил не
    # добивать по своим причинам. Определяется на уровне ПЛАНЕТЫ (не по каждому round_num
    # из planets_by_round отдельно) — слоты планеты общие на все этапы, где она показана
    # (см. database.py::_ensure_tb_platoon_assignments_table), у неё только ОДНО состояние
    # заполненности, а не своё на каждый этап; round_num для held-планеты — её первое
    # появление (planet_first_round), т.к. вся её заливка в основном проходе шла под этим
    # тегом.
    #
    # ВАЖНО (исправлено 2026-08-30 по живому багу): авто-держим применяется только пока
    # операция заполняется "с нуля" ОДНИМ прогоном. Если в операции уже стояло хоть что-то
    # ДО этого прогона (с предыдущего запуска автозаполнения или вручную), это значит
    # планета донабирается по частям на протяжении нескольких запусков — очередной запуск
    # (например, когда реально наступил следующий этап и пора её добивать) должен спокойно
    # дозаполнить остаток, а не снова придержать 1 слот. Живой пример пользователя: этап 3
    # придержан на 14/15 первым прогоном — верно; на этапе 4 повторный прогон должен был
    # добить оставшийся 1 юнит на операцию, а вместо этого снова срезал его обратно до
    # 14/15 — бага в том, что held-проверка была БЕЗУСЛОВНОЙ на каждый прогон. Ручной флаг
    # (не авто) — наоборот, держит планету СТАБИЛЬНО на всех прогонах, пока офицер сам его
    # не снимет — это осознанная постоянная инструкция, а не разовая "оставь один слот".
    last_round_of_planet: dict = {}
    for round_num, planet_set in planets_by_round.items():
        for planet in planet_set:
            last_round_of_planet[planet] = max(last_round_of_planet.get(planet, round_num), round_num)

    for planet in ordered_planets:
        round_num = planet_first_round[planet]
        auto_held = last_round_of_planet.get(planet, round_num) > round_num
        manually_held = bool(hold_flags.get((round_num, planet)))
        if not auto_held and not manually_held:
            continue
        # Для авто-держим (многоэтапная планета) — конкретный этап, на который переносится
        # зачёт придержанного слота: её ПОСЛЕДНЕЕ появление в плане. Упрощение: планета,
        # растянутая на 3+ этапа, всё равно откладывает ровно 1 слот сразу на последний
        # этап (не размазывает постепенно по промежуточным) — пока не встречалось реальных
        # случаев длиннее 2 этапов, пересмотреть при необходимости.
        defer_to_round = last_round_of_planet.get(planet, round_num) if auto_held else None
        for operation in range(1, 7):
            unit_list = tb_platoon_data.ROTE_PLATOON_SUGGESTIONS.get((planet, operation)) or []
            if not unit_list:
                continue
            slots_here = [(planet, operation, idx) for idx in range(len(unit_list))]
            if any(k not in unified for k in slots_here):
                continue  # операция и так не заполнена целиком — держать нечего
            if auto_held and not manually_held and any(k in assignments for k in slots_here):
                continue  # донабор по частям (см. комментарий выше) — не держим повторно
            newly_here = [k for k in slots_here if k not in assignments]
            if not newly_here:
                continue  # все слоты были заполнены раньше вручную — офицер сам решил добить

            def value_of(key):
                p, op, idx = key
                ul = tb_platoon_data.ROTE_PLATOON_SUGGESTIONS.get((p, op)) or []
                bid = name_to_base_id.get(ul[idx]) if idx < len(ul) else None
                ally_code = unified[key]["ally_code"]
                is_ship = unit_types.get(bid) == "ship"
                for o in owners_by_base_id.get(bid, []):
                    if o["ally_code"] == ally_code:
                        return o["stars"] if is_ship else o["relic"]
                return 0

            victim = min(newly_here, key=value_of)
            v_planet, v_op, v_idx = victim

            if auto_held and defer_to_round is not None:
                # Многоэтапная планета: не убираем донора — переносим зачёт слота на её
                # ПОСЛЕДНИЙ этап, где она встречается в плане. Прямой запрос пользователя
                # 2026-08-30: "на 4 этапе надо показать, что заполняем только 1/15, который
                # не заполнили на 3, т.к. остальные уже стоят с прошлого этапа" — донор
                # физически выбирается и записывается СРАЗУ, в этом же прогоне; round-aware
                # отображение (tb_platoon_engine.visible_assignment, database.py::
                # set_tb_platoon_assignment) само покажет слот пустым на текущем round_num и
                # занятым начиная с defer_to_round — второй прогон автозаполнения не нужен.
                ally_code = unified[victim]["ally_code"]
                if not dry_run:
                    database.clear_tb_platoon_assignment(guild_id, plan_id, v_planet, v_op, v_idx)
                    database.set_tb_platoon_assignment(
                        guild_id, plan_id, defer_to_round, v_planet, v_op, v_idx, ally_code, assigned_by="autofill",
                    )
                unified[victim]["round_num"] = defer_to_round
                result.held_back += 1
            else:
                # Планета на единственном этапе (только ручной "держим" — переносить некуда)
                # — слот остаётся реально пустым, как и раньше.
                if not dry_run:
                    database.clear_tb_platoon_assignment(guild_id, plan_id, v_planet, v_op, v_idx)
                del unified[victim]
                result.newly_filled -= 1
                result.held_back += 1
                v_unit_list = tb_platoon_data.ROTE_PLATOON_SUGGESTIONS.get((v_planet, v_op)) or []
                v_unit = v_unit_list[v_idx] if v_idx < len(v_unit_list) else "?"
                result.unfilled.append(SlotOutcome(round_num, v_planet, v_op, v_idx, v_unit, "held_back"))

    # Отчёт по этапам считается по ФИНАЛЬНОМУ состоянию unified (после автозаполнения и
    # снятия held-back слотов), не инкрементально по ходу — планета, растянутая на 2+
    # этапа, показывается в отчёте каждого из них (та же дублирующая логика отображения,
    # что и на /tb/platoons), и её слоты, физически заполненные ПОД ОДНИМ round_num
    # (первым появлением), должны засчитываться как "заполнено" и в отчётах остальных
    # этапов, где эта же планета тоже видна.
    for round_num, planet_set in planets_by_round.items():
        total_here = sum(
            len(tb_platoon_data.ROTE_PLATOON_SUGGESTIONS.get((p, op)) or [])
            for p in planet_set for op in range(1, 7)
        )
        filled_here = sum(1 for (planet, _op, _idx) in unified if planet in planet_set)
        result.by_round[round_num] = {"total": total_here, "filled": filled_here}

    return result
