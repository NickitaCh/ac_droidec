"""Account-wide mod allocation. Exact StatCalc evaluation, bounded diverse search.

The MILP chooses complete builds; its optimum is only over the builds searched.
Current builds are always retained, including incomplete builds and broken sets.
No game writes belong in this module.
"""
import asyncio
from collections import Counter, defaultdict
import itertools
import math
import random
import time

import pulp

import database
import stat_engine
from services import mod_optimizer as legacy
from services import mod_farm as mod_recommendations

SLOTS = ('square', 'arrow', 'diamond', 'triangle', 'circle', 'cross')
SLOT_LABELS = ('Квадрат', 'Стрела', 'Ромб', 'Треугольник', 'Круг', 'Крест')
BUILD_LIMIT = 96
SEARCH_SECONDS = 6


def final_stats(calc, unit, mods):
    result = stat_engine.calc_final_stats(calc, {**unit, 'equippedStatMod': [legacy._to_equipped_stat_mod(m) for m in mods]})
    if result.get('Health') is not None and result.get('Protection') is not None:
        result['Total Life'] = result['Health'] + result['Protection']
    return result


def validate_inventory(mods):
    ids, worn = set(), set()
    for mod in mods:
        mid = mod.get('id')
        if not isinstance(mid, str) or not mid or mid in ids:
            raise ValueError('В инвентаре отсутствуют или повторяются идентификаторы модулей.')
        ids.add(mid)
        if mod['slot_id'] not in range(1, 7) or not all(
                math.isfinite(float(s['value'])) and math.isfinite(float(s['unscaled']))
                for s in [mod['primary'], *mod['secondaries']]):
            raise ValueError('В инвентаре некорректные характеристики модулей.')
        if mod.get('equipped_to'):
            key = (mod['equipped_to'], mod['slot_id'])
            if key in worn:
                raise ValueError('На одном персонаже обнаружены два модуля в одном слоте.')
            worn.add(key)


def _ids(raw):
    return {int(v) for v in str(raw or '').split(',') if v.strip().isdigit()}


def _scheme(rows, mods):
    """Same auto-detection as /статы: set matches, primary matches, scheme 1."""
    counts = Counter(m['set_id'] for m in mods)
    primaries = {SLOTS[m['slot_id'] - 1]: m['primary']['unit_stat_id'] for m in mods}
    scores = {1: [0, 0], 2: [0, 0]}
    for r in rows:
        scheme = r[14] if len(r) > 14 else None
        if scheme not in scores:
            continue
        if r[3] in ('ModSet', 'ModSetAny', 'ModSetOnly'):
            scores[scheme][0] += bool(_mod_value(r, counts, primaries)[0])
        elif r[3] == 'ModPrimary':
            scores[scheme][1] += bool(_mod_value(r, counts, primaries)[0])
    return 1 if scores[1] >= scores[2] else 2


def evaluation_unit(unit, rows_by_plate, mods, relic_mode):
    """Project upwards to the active required relic; never downgrade a roster.

    Schemes are alternatives, so an inactive scheme must not raise the target.
    Optional relic rows do not promise an upgrade the player has not selected.
    """
    if relic_mode == 'current':
        return unit
    target = stat_engine.get_current_relic_level(unit)
    for rows in rows_by_plate.values():
        leaves = defaultdict(list)
        for row in rows:
            leaves[row[1]].append(row)
        for group in leaves.values():
            scheme = _scheme(group, mods)
            for row in group:
                if (row[3] == 'Relic' and row[6] == 'required' and row[4] in ('>=', '=')
                        and not (len(row) > 14 and row[14] and row[14] != scheme)):
                    value = float(row[5])
                    if not value.is_integer() or not 0 <= value <= 10:
                        raise ValueError('В плейте указан некорректный уровень реликвии.')
                    target = max(target, int(value))
    if target > stat_engine.get_current_relic_level(unit):
        return stat_engine.project_unit_relic(unit, target)
    return unit


def _mod_value(row, counts, primaries):
    name, threshold = row[3], row[5]
    skill, slot = (row[11] if len(row) > 11 else ''), (row[12] if len(row) > 12 else '')
    complete = {s: n // stat_engine.MOD_SET_PIECE_COUNT[s] for s, n in counts.items()}
    if name == 'ModPrimary':
        allowed = _ids(skill) or {int(threshold)}
        actual = primaries.get(slot)
        return actual in allowed, actual, ' / '.join(stat_engine.UNIT_STAT_RU.get(s, str(s)) for s in sorted(allowed))
    if name == 'ModSet':
        actual = complete.get(int(skill), 0)
        return legacy._compare(actual, row[4], threshold), actual, f'{stat_engine.MOD_SET_NAMES_RU.get(int(skill), skill)} × {threshold:g}'
    worn = {s for s, n in complete.items() if n}
    listed = _ids(skill)
    ok = bool(worn & listed) if name == 'ModSetAny' else bool(worn) and worn <= listed
    label = 'Один из: ' if name == 'ModSetAny' else 'Только: '
    return ok, ', '.join(stat_engine.MOD_SET_NAMES_RU[s] for s in sorted(worn)) or 'Нет', label + ' / '.join(stat_engine.MOD_SET_NAMES_RU.get(s, str(s)) for s in sorted(listed))


def evaluate(rows_by_plate, unit, mods, stats, peers, omicron_tiers=None):
    """Never merge required/optional rows or thresholds from different plates."""
    counts = Counter(m['set_id'] for m in mods)
    primaries = {SLOTS[m['slot_id'] - 1]: m['primary']['unit_stat_id'] for m in mods}
    checked, plate_ok, deficit = [], {}, 0.0
    for plate, rows in rows_by_plate.items():
        # A modular plate can include independent schemes from several leaves.
        leaves = defaultdict(list)
        for row in rows:
            leaves[row[1]].append(row)
        active = {leaf: _scheme(group, mods) for leaf, group in leaves.items()}
        plate_ok[plate] = True
        for row_index, row in enumerate(rows):
            if len(row) > 14 and row[14] and row[14] != active[row[1]]:
                continue
            name, op, target, priority = row[3:7]
            other = row[13] if len(row) > 13 else None
            current = None
            label, target_text = name, None
            if name in ('ModPrimary', 'ModSet', 'ModSetAny', 'ModSetOnly'):
                ok, current, target_text = _mod_value(row, counts, primaries)
                label = SLOT_LABELS[SLOTS.index(row[12])] if name == 'ModPrimary' else 'Сеты'
            else:
                if name == 'Relic':
                    current = stat_engine.get_current_relic_level(unit)
                elif name == 'Omicron':
                    tiers = {s.get('id'): s.get('tier', -1) for s in unit.get('skill', [])}
                    needed = (omicron_tiers or {}).get(row[11])
                    current = int(needed is not None and tiers.get(row[11], -1) >= needed)
                    target = 1
                    label = 'Омикрон ' + str(row[11])
                else:
                    current = stats.get(name)
                if other:
                    value = peers.get(other, {}).get(name)
                    target = None if value is None else value + (-1 if op == '<=' else 1) * (target or 0)
                    label += ' vs ' + other
                ok = current is not None and target is not None and legacy._compare(current, op, target)
            required = priority == 'required'
            if required and not ok:
                plate_ok[plate] = False
                if isinstance(current, (float, int)) and isinstance(target, (float, int)) and not other:
                    deficit += min(10, abs(current - target) / max(abs(target), 1))
                else:
                    deficit += 1
            checked.append({'stat_name': name, 'label': label, 'operator': op, 'threshold': target,
                            'priority': priority, 'current': current, 'ok': bool(ok),
                            'target_text': target_text, 'plate': plate, 'compare': other, 'row_index': row_index,
                            'editable_id': row[0] if not other and name in stats and isinstance(target, (int, float)) else None})
    return {'rows': checked, 'plate_ok': plate_ok, 'full_pass': all(plate_ok.values()),
            'required_matched': sum(r['ok'] and r['priority'] == 'required' for r in checked),
            'optional_matched': sum(r['ok'] and r['priority'] != 'required' for r in checked),
            'deficit': deficit}


def _rank(c):
    return (sum(c['plate_ok'].values()), c['required_matched'], -c['deficit'],
            c['optional_matched'], -c['moves'])


def search(calc, base_id, unit, rows, pool, current, peers, locked=False, omicron_tiers=None, seconds=SEARCH_SECONDS, relic_mode='current', slow_candidate=False, deep_search=False):
    """Diverse seeds + exact coordinate descent, preserving resource alternatives.

    Mod contributions are ranked by one-mod StatCalc deltas, not by adding raw HP
    to percentages. Every retained build is independently recalculated in full.
    """
    start = time.monotonic()
    cache = {}
    fixed = {m['slot_id']: m for m in current if m.get('locked')}
    current_ids = {m['id'] for m in current}

    def candidate(mods):
        mods = sorted(mods, key=lambda m: m['slot_id'])
        key = tuple(m['id'] for m in mods)
        if key not in cache:
            modeled = evaluation_unit(unit, rows, mods, relic_mode)
            final = final_stats(calc, modeled, mods)
            check = evaluate(rows, modeled, mods, final, peers, omicron_tiers)
            cache[key] = {**check, 'mods': mods, 'mod_ids': frozenset(key), 'final_stats': final,
                          'evaluation_unit': modeled,
                          'moves': sum(m['id'] not in current_ids for m in mods), 'baseline': set(key) == current_ids}
        return cache[key]

    baseline = candidate(current)
    if locked or unit.get('currentLevel', 85) < 50:
        return [baseline]
    by_slot = defaultdict(list)
    for mod in pool:
        if (mod.get('locked') and mod['id'] not in current_ids) or (mod['rarity'] == 6 and unit.get('currentTier', 0) < 12):
            continue
        if mod['slot_id'] in fixed and mod['id'] != fixed[mod['slot_id']]['id']:
            continue
        by_slot[mod['slot_id']].append(mod)
    if any(not by_slot[s] for s in range(1, 7)):
        return [baseline]

    # One-mod deltas capture flat vs % scaling and the unit's actual base.
    scoring_unit = evaluation_unit(unit, rows, current, relic_mode)
    naked = final_stats(calc, scoring_unit, [])
    stat_names = {r[3] for group in rows.values() for r in group}
    if slow_candidate:
        stat_names.add('Speed')
    names = sorted(stat_names & naked.keys())
    deltas = {}
    for slot in by_slot.values():
        for m in slot:
            vals = final_stats(calc, scoring_unit, [m])
            deltas[m['id']] = {n: vals[n] - naked[n] for n in names}

    reqs = [r for group in rows.values() for r in group if r[3] in naked]
    weights = defaultdict(float)
    for r in reqs:
        sign = -1 if r[4] == '<=' else 1
        weights[r[3]] += sign / max(abs(r[5]), abs(naked.get(r[3], 0)), 1)
    rng = random.Random(base_id)
    modes = [dict(weights)] + [{n: 1 / max(abs(naked[n]), 1)} for n in names]
    if slow_candidate and 'Speed' in naked:
        modes.insert(0, {'Speed': -1})
    modes += [{n: w * rng.uniform(.2, 2.5) for n, w in weights.items()} for _ in range(16 if deep_search else 8)]
    seeds = [current] if len(current) == 6 else []
    preferred_primaries = defaultdict(set)
    for group in rows.values():
        for r in group:
            if r[3] == 'ModPrimary':
                preferred_primaries[SLOTS.index(r[12]) + 1].update(_ids(r[11]) or {int(r[5])})

    # Enumerate every set placement, not just the first best permutation.
    permutations = sorted({p for shape in legacy._target_multisets() for p in itertools.permutations(shape)})
    for mode in modes:
        def score(m):
            return sum(deltas[m['id']].get(n, 0) * w for n, w in mode.items()) + (
                .5 if m['primary']['unit_stat_id'] in preferred_primaries[m['slot_id']] else 0)
        ranked = {s: sorted(ms, key=lambda m: (-score(m), m['id'])) for s, ms in by_slot.items()}
        buckets = {(s, sid): [m for m in ranked[s] if m['set_id'] == sid] for s in ranked for sid in range(1, 9)}
        shapes = []
        for perm in permutations:
            choices = [buckets[s, sid] for s, sid in enumerate(perm, 1)]
            if all(choices):
                shapes.append((sum(score(c[0]) for c in choices), choices))
        shapes.sort(key=lambda x: -x[0])
        for _, choices in shapes[:18]:
            seeds.append([c[0] for c in choices])
        # Broken sets are legitimate too; actual thresholds decide their merit.
        seeds.append([ranked[s][0] for s in range(1, 7)])
        for k in range(1, 6):
            seeds.append([ranked[s][min(k, len(ranked[s]) - 1)] for s in range(1, 7)])
        if time.monotonic() - start > seconds:
            break
    for mods in seeds:
        candidate(mods)
    # Exact, threshold-aware multi-stat search around several distinct seeds.
    frontier = sorted(cache.values(), key=_rank, reverse=True)[:24 if deep_search else 12]
    for seed in frontier:
        best = seed
        for _ in range(3):
            old = best
            for s in range(1, 7):
                if s in fixed:
                    continue
                alternatives = by_slot[s]
                # Keep the best of each set and primary as well as overall mods.
                alternatives = sorted(alternatives, key=lambda m: -sum(
                    deltas[m['id']].get(n, 0) * w for n, w in weights.items()))
                selected, groups = alternatives[:12], set()
                for m in alternatives:
                    group = (m['set_id'], m['primary']['unit_stat_id'])
                    if group not in groups:
                        selected.append(m)
                        groups.add(group)
                for m in selected:
                    trial = candidate([m if x['slot_id'] == s else x for x in best['mods']])
                    if _rank(trial) > _rank(best):
                        best = trial
                if time.monotonic() - start > seconds:
                    break
            if best is old or time.monotonic() - start > seconds:
                break
        if time.monotonic() - start > seconds:
            break
    ordered = sorted(cache.values(), key=_rank, reverse=True)
    # Reserve diversity: a purely top-N cut makes all builds share rare mods.
    kept = [baseline]
    if slow_candidate and 'Speed' in naked:
        slowest = min(ordered, key=lambda c: c['final_stats']['Speed'])
        if slowest is not baseline:
            kept.append(slowest)
    for c in ordered:
        if c not in kept and len(kept) < BUILD_LIMIT // 2:
            kept.append(c)
    used = Counter(mid for c in kept for mid in c['mod_ids'])
    rest = [c for c in ordered if c not in kept]
    while rest and len(kept) < BUILD_LIMIT:
        c = min(rest, key=lambda x: (sum(used[mid] for mid in x['mod_ids']), -x['required_matched'], x['deficit']))
        rest.remove(c)
        kept.append(c)
        used.update(c['mod_ids'])
    return kept


def solve(candidates, members, rows_by_char, units, peers, omicron_tiers, seconds=45):
    """Global allocation with cross-character constraints and strict priorities."""
    prob = pulp.LpProblem('mod_optimizer_v2', pulp.LpMaximize)
    y = {(b, i): pulp.LpVariable(f'b_{n}_{i}', cat='Binary')
         for n, (b, cs) in enumerate(candidates.items()) for i in range(len(cs))}
    usage = defaultdict(list)
    for b, cs in candidates.items():
        prob += pulp.lpSum(y[b, i] for i in range(len(cs))) == 1
        for i, c in enumerate(cs):
            for mid in c['mod_ids']:
                usage[mid].append(y[b, i])
    for vs in usage.values():
        prob += pulp.lpSum(vs) <= 1

    # Cross-character comparisons use selected stat expressions and bounded gates.
    row_ok, required_terms, optional_terms = {}, [], []
    details = {}
    for b, cs in candidates.items():
        for i, c in enumerate(cs):
            check = evaluate(rows_by_char[b], c.get('evaluation_unit', units[b]), c['mods'], c['final_stats'], peers, omicron_tiers)
            details[b, i] = {(r['plate'], r['row_index']): r for r in check['rows']}
    for b, cs in candidates.items():
        rows = rows_by_char[b]
        # Use stable raw-row positions, including alternative schemes.
        for plate, raw_rows in rows.items():
            for ri, r in enumerate(raw_rows):
                other = r[13] if len(r) > 13 else None
                terms = []
                if other in candidates and other != b:
                    v = pulp.LpVariable(f'comparison_{len(row_ok)}', cat='Binary')
                    own_values = [c['final_stats'].get(r[3]) for c in cs]
                    peer_values = [c['final_stats'].get(r[3]) for c in candidates[other]]
                    inactive_terms = []
                    valid_terms = []
                    for i, c in enumerate(cs):
                        inactive = (plate, ri) not in details[b, i]
                        if inactive:
                            inactive_terms.append(y[b, i])
                        elif own_values[i] is not None:
                            valid_terms.append(y[b, i])
                    inactive_expr = pulp.lpSum(inactive_terms)
                    valid_peer = pulp.lpSum(y[other, j] for j, value in enumerate(peer_values) if value is not None)
                    prob += v <= inactive_expr + pulp.lpSum(valid_terms)
                    prob += v <= inactive_expr + valid_peer
                    left = pulp.lpSum(y[b, i] * (value or 0) for i, value in enumerate(own_values))
                    right = pulp.lpSum(y[other, j] * (value or 0) for j, value in enumerate(peer_values))
                    offset = (-1 if r[4] == '<=' else 1) * (r[5] or 0)
                    bounds = [abs(value) for value in own_values + peer_values if value is not None]
                    big_m = 2 * max(bounds, default=0) + abs(offset) + 1
                    if r[4] in ('>=', '='):
                        prob += left - right - offset >= -big_m * (1 - v + inactive_expr)
                    if r[4] in ('<=', '='):
                        prob += left - right - offset <= big_m * (1 - v + inactive_expr)
                    row_ok[b, plate, ri] = v
                    (required_terms if r[6] == 'required' else optional_terms).append(v)
                    continue
                for i, c in enumerate(cs):
                    detail = details[b, i].get((plate, ri))
                    inactive = detail is None
                    if inactive:
                        terms.append(y[b, i])
                    elif other == b:
                        current = c['final_stats'].get(r[3])
                        if current is not None and legacy._compare(
                                current, r[4], current + (-1 if r[4] == '<=' else 1) * (r[5] or 0)):
                            terms.append(y[b, i])
                    elif detail['ok']:
                        terms.append(y[b, i])
                expr = pulp.lpSum(terms)
                row_ok[b, plate, ri] = expr
                (required_terms if r[6] == 'required' else optional_terms).append(expr)
    plate_vars, char_vars = {}, []
    for b, rows in rows_by_char.items():
        if b not in candidates:
            continue
        cv = pulp.LpVariable(f'charpass_{len(char_vars)}', cat='Binary')
        for plate, rs in rows.items():
            for ri, r in enumerate(rs):
                if r[6] == 'required':
                    prob += cv <= row_ok[b, plate, ri]
        char_vars.append(cv)
    for plate, bs in members.items():
        pv = pulp.LpVariable(f'platepass_{len(plate_vars)}', cat='Binary')
        if not bs or any(b not in candidates for b in bs):
            prob += pv == 0
        for b in bs:
            if b not in candidates:
                continue
            for ri, r in enumerate(rows_by_char.get(b, {}).get(plate, [])):
                if r[6] == 'required':
                    prob += pv <= row_ok[b, plate, ri]
        plate_vars[plate] = pv
    # Solve priorities separately: giant fixed weights lose numeric accuracy for
    # large modular plates and can let optional rows outweigh required ones.
    deficit = pulp.lpSum(y[b, i] * c['deficit'] for b, cs in candidates.items() for i, c in enumerate(cs))
    moves = pulp.lpSum(y[b, i] * c['moves'] for b, cs in candidates.items() for i, c in enumerate(cs))
    objectives = [(pulp.lpSum(plate_vars.values()), True), (pulp.lpSum(char_vars), True),
                  (pulp.lpSum(required_terms), True), (pulp.lpSum(optional_terms), True),
                  (deficit, False), (moves, False)]
    chosen = {b: next(c for c in cs if c['baseline']) for b, cs in candidates.items()}
    started, optimal = time.monotonic(), True
    for phase, (expression, maximize) in enumerate(objectives):
        if not len(expression):
            continue
        budget = seconds - (time.monotonic() - started)
        if budget <= 0:
            optimal = False
            break
        prob.sense = pulp.LpMaximize if maximize else pulp.LpMinimize
        prob.setObjective(expression)
        prob.solve(pulp.PULP_CBC_CMD(msg=False, timeLimit=max(.1, budget / (len(objectives) - phase))))
        incumbent = {}
        for b, cs in candidates.items():
            values = [pulp.value(y[b, i]) for i in range(len(cs))]
            if any(v is None or abs(v - round(v)) > 1e-5 for v in values) or sum(round(v) for v in values) != 1:
                return chosen, False
            incumbent[b] = cs[next(i for i, v in enumerate(values) if v > .5)]
        chosen = incumbent
        proven = prob.status == pulp.LpStatusOptimal and prob.sol_status == pulp.LpSolutionOptimal
        if not proven:
            optimal = False
            break
        value = pulp.value(expression)
        if phase < 4:
            prob += expression == round(value)
        elif not maximize:
            prob += expression <= value + 1e-7
    mids = [mid for c in chosen.values() for mid in c['mod_ids']]
    if len(mids) != len(set(mids)):
        raise ValueError('Солвер вернул повторное использование модуля.')
    return chosen, optimal


async def prepare_inputs(comlink, plates, code, mods, guild_id, force_refresh=False, *,
                         units_override=None, relic_mode='current', thresholds=None, turn_order=()):
    if relic_mode not in ('current', 'plate'):
        raise ValueError('Выберите расчёт по текущей реликвии или по реликвии плейта.')
    validate_inventory(mods)
    members = {p: database.get_stat_requirement_characters(p, guild_id=guild_id) for p in plates}
    types = database.get_unit_types(list({b for bs in members.values() for b in bs}))
    members = {p: [b for b in bs if types.get(b) != 'ship'] for p, bs in members.items()}
    members = {p: bs for p, bs in members.items() if bs}
    if not members:
        raise ValueError('В выбранных плейтах нет персонажей для подбора модулей.')
    rows = {b: {p: database.get_stat_requirements(p, b, guild_id=guild_id) for p, bs in members.items() if b in bs}
            for b in dict.fromkeys(b for bs in members.values() for b in bs)}
    if thresholds:
        matched = set()
        for group in rows.values():
            for plate, requirements in group.items():
                for i, row in enumerate(requirements):
                    key = str(row[0])
                    if (key in thresholds and not row[3].startswith('Mod') and row[3] not in ('Relic', 'Omicron')
                            and not (len(row) > 13 and row[13])):
                        requirements[i] = (*row[:5], thresholds[key], *row[6:])
                        matched.add(key)
        if matched != thresholds.keys():
            raise ValueError('Изменённое требование больше недоступно в выбранных плейтах.')
    order_group = 'Порядок ходов'
    while order_group in members:
        order_group += ' ·'
    for faster, slower in turn_order:
        if faster == slower or faster not in rows or slower not in rows:
            raise ValueError('Для порядка ходов выберите двух разных персонажей из выбранных плейтов.')
        rows[faster].setdefault(order_group, []).append(
            (None, order_group, faster, 'Speed', '>=', 1, 'required',
             None, None, None, None, None, None, slower, None))
    # Refresh once for the whole account, never once per character.
    if force_refresh and units_override is None:
        from services import activity_diff
        await activity_diff.refresh_player_units(comlink, code)
    equipped = defaultdict(list)
    for m in mods:
        if m.get('equipped_to'):
            equipped[m['equipped_to']].append(m)
    other_keys = {r[13] for group in rows.values() for rs in group.values() for r in rs if len(r) > 13 and r[13]}
    unit_keys = set(rows) | set(equipped) | other_keys
    types.update(database.get_unit_types(list(unit_keys - types.keys())))
    units = {}
    for b in unit_keys:
        if types.get(b) == 'ship':
            continue
        unit = units_override.get(b) if units_override is not None else (database.get_player_unit(code, b) or (None, None))[0]
        if unit:
            units[b] = unit
    if any(faster not in units or slower not in units for faster, slower in turn_order):
        raise ValueError('Оба персонажа из правила порядка ходов должны быть в актуальном ростере.')
    missing_donors = set(equipped) - set(units)
    if missing_donors:
        raise ValueError('Ростер неполный: обновите данные игрока перед расчётом.')
    tiers = database.get_skill_omicron_tiers(list({r[11] for group in rows.values() for rs in group.values() for r in rs if r[3] == 'Omicron'}))

    return members, rows, units, tiers


def calculate(calc, members, rows, units, tiers, mods, names, *, locked=(), relic_mode='current',
              turn_order=(), deep_search=False):
    equipped = defaultdict(list)
    for mod in mods:
        if mod.get('equipped_to'):
            equipped[mod['equipped_to']].append(mod)
    slower_units = {slower for _, slower in turn_order}

    search_seconds = 18 if deep_search else SEARCH_SECONDS
    repair_seconds = 6 if deep_search else 2
    solve_seconds = 135 if deep_search else 45

    before = {b: final_stats(calc, u, equipped[b]) for b, u in units.items()}
    modeled_before = {**before, **{b: final_stats(calc, evaluation_unit(units[b], group, equipped[b], relic_mode), equipped[b])
                                  for b, group in rows.items() if b in units}}
    pool = [m for m in mods if m.get('equipped_to') not in locked]
    candidates = {b: search(calc, b, units[b], group, pool, equipped[b], modeled_before, b in locked, tiers,
                            seconds=search_seconds, relic_mode=relic_mode, slow_candidate=b in slower_units,
                            deep_search=deep_search)
                  for b, group in rows.items() if b in units}
    chosen, optimal = solve(candidates, members, rows, units, before, tiers, seconds=solve_seconds) if candidates else ({}, True)
    first_choice = chosen
    # Contention repair: search again with other selected builds reserved.
    for b in candidates:
        if b in locked or chosen[b]['full_pass']:
            continue
        reserved = {mid for key, c in chosen.items() if key != b for mid in c['mod_ids']}
        extra = search(calc, b, units[b], rows[b], [m for m in pool if m['id'] not in reserved], equipped[b],
                       {**modeled_before, **{key: c['final_stats'] for key, c in chosen.items()}},
                       omicron_tiers=tiers, seconds=repair_seconds, relic_mode=relic_mode,
                       slow_candidate=b in slower_units, deep_search=deep_search)
        known = {c['mod_ids'] for c in candidates[b]}
        candidates[b].extend(c for c in extra if c['mod_ids'] not in known)
    if candidates:
        chosen, optimal = solve(candidates, members, rows, units, before, tiers, seconds=solve_seconds)
    def assess(allocation):
        allocated = {mid: b for b, c in allocation.items() for mid in c['mod_ids']}
        after = {**before, **{b: c['final_stats'] for b, c in allocation.items()}}
        for b, worn in equipped.items():
            if b in allocation:
                continue
            remaining = [m for m in worn if allocated.get(m['id'], b) == b]
            if len(remaining) != len(worn):
                after[b] = final_stats(calc, units[b], remaining)
        checks = {b: evaluate(rows[b], c['evaluation_unit'], c['mods'], c['final_stats'], after, tiers) for b, c in allocation.items()}
        completed = set(members)
        for b, group in rows.items():
            completed -= {p for p in group if b not in checks or not checks[b]['plate_ok'].get(p)}
        score = (len(completed), sum(c['full_pass'] for c in checks.values()),
                 sum(c['required_matched'] for c in checks.values()),
                 sum(c['optional_matched'] for c in checks.values()),
                 -sum(c['deficit'] for c in checks.values()), -sum(c['moves'] for c in allocation.values()))
        return score, after
    baseline = {b: next(c for c in cs if c['baseline']) for b, cs in candidates.items()}
    options = [chosen, first_choice, baseline]
    scores = [assess(option) for option in options]
    best = max(range(len(options)), key=lambda i: scores[i][0])
    chosen, after = options[best], scores[best][1]
    if best:
        optimal = False
    results, passed, donors = render_allocation(calc, members, rows, units, tiers, mods, names,
                                                chosen, locked=locked, before=before, after=after)
    farm = mod_recommendations.farm_context(calc, rows, units, tiers, mods, chosen, locked, relic_mode)
    for result in results:
        if result['owned']:
            result['recommendations'] = mod_recommendations.recommend(farm, result['base_id'])
    return results, passed, optimal, donors


def render_allocation(calc, members, rows, units, tiers, mods, names, chosen, *, locked=(), before=None, after=None):
    """Exact assessment of a concrete allocation; no search or solver calls."""
    equipped = defaultdict(list)
    for mod in mods:
        if mod.get('equipped_to'):
            equipped[mod['equipped_to']].append(mod)
    if before is None:
        before = {b: final_stats(calc, u, equipped[b]) for b, u in units.items()}
    if after is None:
        allocation = {mid: b for b, c in chosen.items() for mid in c['mod_ids']}
        after = {**before, **{b: c['final_stats'] for b, c in chosen.items()}}
        for b, worn in equipped.items():
            if b not in chosen:
                remaining = [m for m in worn if allocation.get(m['id'], b) == b]
                if len(remaining) != len(worn):
                    after[b] = final_stats(calc, units[b], remaining)
    actual_after = {**after, **{b: final_stats(calc, units[b], c['mods']) for b, c in chosen.items()}}
    results, passed = [], set(members)
    for b, group in rows.items():
        if b not in chosen:
            passed -= set(group)
            results.append({'base_id': b, 'char_name': names.get(b) or b, 'owned': False,
                            'plate': ', '.join(group), 'plates': list(group), 'passed': [], 'failed': [], 'used_mods': [],
                            'reason': 'Персонаж не открыт или отсутствует в актуальном ростере.'})
            continue
        c = chosen[b]
        check = evaluate(group, c['evaluation_unit'], c['mods'], c['final_stats'], after, tiers)
        actual_check = evaluate(group, units[b], c['mods'], actual_after[b], actual_after, tiers)
        actual_rows = {(r['plate'], r['row_index']): r for r in actual_check['rows']}
        old = evaluate(group, units[b], equipped[b], before[b], before, tiers)
        old_rows = {(r['plate'], r['label'], r['target_text']): r for r in old['rows']}
        for r in check['rows']:
            r['before'] = old_rows.get((r['plate'], r['label'], r['target_text']), {}).get('current')
            r['now'] = actual_rows[(r['plate'], r['row_index'])]['current']
            r['now_ok'] = actual_rows[(r['plate'], r['row_index'])]['ok']
        passed -= {p for p, ok in check['plate_ok'].items() if not ok}
        results.append({'base_id': b, 'char_name': names.get(b) or b, 'owned': True,
                        'plate': ', '.join(group), 'plates': list(group), 'locked': b in locked, 'ok': check['full_pass'],
                        'passed': [r for r in check['rows'] if r['ok']], 'failed': [r for r in check['rows'] if not r['ok']],
                        'used_mods': c['mods'], 'reason': 'Персонаж заблокирован.' if b in locked else None,
                        'current_relic': stat_engine.get_current_relic_level(units[b]),
                        'target_relic': stat_engine.get_current_relic_level(c['evaluation_unit']),
                        'projected': c['evaluation_unit'] is not units[b],
                        'required_relic': None})
    allocation = {m['id']: b for b, c in chosen.items() for m in c['mods']}
    donors = []
    for b, worn in equipped.items():
        if b in chosen:
            continue
        remaining = [m for m in worn if allocation.get(m['id'], b) == b]
        if len(remaining) == len(worn):
            continue
        stats = final_stats(calc, units[b], remaining)
        changes = [{'stat_name': n, 'before': v, 'current': stats.get(n)} for n, v in before[b].items() if stats.get(n) != v]
        old_sets, new_sets = Counter(m['set_id'] for m in worn), Counter(m['set_id'] for m in remaining)
        lost = [stat_engine.MOD_SET_NAMES_RU[s] for s, n in old_sets.items()
                for _ in range(n // stat_engine.MOD_SET_PIECE_COUNT[s] - new_sets[s] // stat_engine.MOD_SET_PIECE_COUNT[s])]
        donors.append({'base_id': b, 'name': names.get(b) or b, 'changes': changes,
                       'removed': len(worn) - len(remaining), 'lost_sets': lost})
    return results, sorted(passed), donors


async def optimize(comlink, calc, plates, code, mods, guild_id, locked=(), force_refresh=False, *,
                   units_override=None, relic_mode='current', thresholds=None, turn_order=(), deep_search=False):
    members, rows, units, tiers = await prepare_inputs(
        comlink, plates, code, mods, guild_id, force_refresh, units_override=units_override,
        relic_mode=relic_mode, thresholds=thresholds, turn_order=turn_order)

    def run():
        names = {base_id: database.get_game_unit_name(base_id) for base_id in rows.keys() | units.keys()}
        return calculate(calc, members, rows, units, tiers, mods, names, locked=locked,
                         relic_mode=relic_mode, turn_order=turn_order, deep_search=deep_search)

    return await asyncio.to_thread(run)
