"""Bounded, read-only counterfactuals: acquire one or two mods, keep everyone else.

Secondary profiles are copied together from an observed level-15 mod, never made
by summing the best rolls of several mods. Six-dot primaries use the same verified
catalogue as the mod calculator. Proposals are examples, not minimum roll claims.
The browser sends recipes only; the server rebuilds and checks the winning trials.
"""
from collections import Counter, defaultdict
import itertools

import stat_engine
from services import mod_optimizer_v2 as optimizer


def farm_mod(recipe, indexed):
    if not isinstance(recipe, dict) or set(recipe) != {'source', 'slot', 'set', 'primary'}:
        raise ValueError('Некорректный пример модуля.')
    source, slot, sid, primary_id = (recipe[k] for k in ('source', 'slot', 'set', 'primary'))
    if (not isinstance(source, str) or type(slot) is not int or slot not in range(1, 7)
            or type(sid) is not int or sid not in stat_engine.MOD_SET_PIECE_COUNT
            or type(primary_id) is not int):
        raise ValueError('Некорректный пример модуля.')
    original = indexed.get(source)
    if source and (original is None or original['level'] != 15 or original['rarity'] not in (5, 6)):
        raise ValueError('Образец модуля больше недоступен.')
    rarity = original['rarity'] if original else 6
    if rarity == 5:
        if slot != original['slot_id'] or primary_id != original['primary']['unit_stat_id']:
            raise ValueError('Основа модуля не соответствует образцу.')
        primary = dict(original['primary'])
    else:
        option = next((p for p in stat_engine.MOD_PRIMARY_OPTIONS[optimizer.SLOTS[slot - 1]]
                       if p['unit_stat'] == primary_id), None)
        if option is None:
            raise ValueError('Эта основа недоступна для выбранной фигуры.')
        percent = primary_id != 5
        unscaled = int(option['unscaled_value'])
        primary = dict(unit_stat_id=primary_id, unscaled=unscaled,
                       value=unscaled / (1e6 if percent else 1e8),
                       is_percent=percent, name=stat_engine.UNIT_STAT_RU.get(primary_id, str(primary_id)))
    if original and any(s['unit_stat_id'] == primary_id for s in original['secondaries']):
        raise ValueError('Основа повторяется в дополнительных характеристиках.')
    secondaries = [dict(s) for s in original['secondaries']] if original else []
    return dict(id='farm-example', slot_id=slot, set_id=sid, rarity=rarity, level=15,
                tier=original['tier'] if original else 1, primary=primary, secondaries=secondaries,
                equipped_to=None, locked=False, set_name=stat_engine.MOD_SET_NAMES_RU[sid],
                slot_name=optimizer.SLOT_LABELS[slot - 1])


def farm_signature(mod):
    return (mod['slot_id'], mod['set_id'], mod['rarity'], mod['primary']['unit_stat_id'],
            float(mod['primary']['unscaled']),
            tuple(sorted((s['unit_stat_id'], float(s['unscaled'])) for s in mod['secondaries'])))


def farm_matches(actual, example):
    if any(actual[k] != example[k] for k in ('slot_id', 'set_id', 'rarity')):
        return False
    if (actual['primary']['unit_stat_id'] != example['primary']['unit_stat_id']
            or float(actual['primary']['unscaled']) != float(example['primary']['unscaled'])):
        return False
    stats = {s['unit_stat_id']: float(s['unscaled']) for s in actual['secondaries']}
    return stats == {s['unit_stat_id']: float(s['unscaled']) for s in example['secondaries']}


def farm_context(calc, rows, units, tiers, mods, chosen, locked, relic_mode):
    worn = defaultdict(list)
    for mod in mods:
        if mod.get('equipped_to'):
            worn[mod['equipped_to']].append(mod)
    assigned = {m['id']: b for b, c in chosen.items() for m in c['mods']}
    peers = {b: c['final_stats'] for b, c in chosen.items()}
    for b, unit in units.items():
        if b not in chosen:
            peers[b] = optimizer.final_stats(calc, unit, [m for m in worn[b] if assigned.get(m['id'], b) == b])
    checks = {b: optimizer.evaluate(rows[b], c['evaluation_unit'], c['mods'], peers[b], peers, tiers)
              for b, c in chosen.items()}
    return dict(calc=calc, rows=rows, units=units, tiers=tiers, mods=mods, chosen=chosen,
                locked=set(locked), relic_mode=relic_mode, indexed={m['id']: m for m in mods},
                assigned=assigned, peers=peers, checks=checks)


def farm_trial(context, base, recipes, *, describe=True):
    """Rebuild a proposal and guard every previously satisfied requirement."""
    if (base not in context['chosen'] or base in context['locked']
            or context['units'][base].get('currentLevel', 85) < 50):
        return None
    if not isinstance(recipes, list) or not 1 <= len(recipes) <= 2:
        raise ValueError('В рекомендации допустимы один или два модуля.')
    examples = [farm_mod(r, context['indexed']) for r in recipes]
    slots = {m['slot_id'] for m in examples}
    current = context['chosen'][base]['mods']
    if any(farm_matches(m, example) for example in examples for m in current):
        return None
    if len(slots) != len(examples) or any(m.get('locked') and m['slot_id'] in slots for m in current):
        return None
    if context['units'][base].get('currentTier', 0) < 12 and any(m['rarity'] == 6 for m in examples):
        return None
    trial_mods = [m for m in current if m['slot_id'] not in slots] + examples
    unit = optimizer.evaluation_unit(context['units'][base], context['rows'][base], trial_mods, context['relic_mode'])
    # A different scheme must not promise an additional relic upgrade for free.
    if stat_engine.get_current_relic_level(unit) != stat_engine.get_current_relic_level(context['chosen'][base]['evaluation_unit']):
        return None
    stats = optimizer.final_stats(context['calc'], unit, trial_mods)
    peers = {**context['peers'], base: stats}
    check = optimizer.evaluate(context['rows'][base], unit, trial_mods, stats, peers, context['tiers'])
    old = context['checks'][base]
    old_rows = {(r['plate'], r['row_index']): r for r in old['rows']}
    new_rows = {(r['plate'], r['row_index']): r for r in check['rows']}
    if any(r['ok'] and key in new_rows and not new_rows[key]['ok'] for key, r in old_rows.items()):
        return None
    if any(ok and not check['plate_ok'].get(p) for p, ok in old['plate_ok'].items()):
        return None
    if check['required_matched'] < old['required_matched'] or check['deficit'] > old['deficit'] + 1e-8:
        return None
    # Includes incoming turn-order constraints and comparisons in other plates.
    for other, group in context['rows'].items():
        if other == base or other not in context['chosen']:
            continue
        if not any(len(r) > 13 and r[13] == base for rs in group.values() for r in rs):
            continue
        c = context['chosen'][other]
        updated = optimizer.evaluate(group, c['evaluation_unit'], c['mods'], peers[other], peers, context['tiers'])
        prior = {(r['plate'], r['row_index']): r for r in context['checks'][other]['rows']}
        if any(prior.get((r['plate'], r['row_index']), {}).get('ok') and not r['ok'] for r in updated['rows']):
            return None
    closed = [r for key, r in new_rows.items() if r['ok'] and key in old_rows and not old_rows[key]['ok']]
    gains = []
    seen = set()
    for r in check['rows']:
        name = r['stat_name']
        if (name in seen or name in ('Relic', 'Omicron') or name.startswith('Mod')
                or r['operator'] != '>=' or r.get('compare')):
            continue
        seen.add(name)
        before, after = context['peers'][base].get(name), stats.get(name)
        if isinstance(before, (int, float)) and isinstance(after, (int, float)):
            delta = after - before
            if abs(delta) > 1e-7:
                gains.append(dict(stat_name=name, before=before, after=after, delta=delta))
    required = sum(r['priority'] == 'required' for r in closed)
    optional = len(closed) - required
    deficit_gain = old['deficit'] - check['deficit']
    gain = sum(g['delta'] / max(abs(g['before']), 1) for g in gains)
    if not (closed or deficit_gain > 1e-6 or gain >= .01 or any(g['stat_name'] == 'Speed' and g['delta'] >= 1 for g in gains)):
        return None
    needs = []
    for example in examples:
        if not describe:
            needs.append(dict(mod=example, recipe=recipes[len(needs)]))
            continue
        matches = [m for m in context['mods'] if farm_matches(m, example)]
        def final_owner(mod):
            return context['assigned'].get(mod['id'], mod.get('equipped_to')
                                          if mod.get('equipped_to') not in context['chosen'] else None)
        # The two examples have different slots, hence cannot consume one real mod twice.
        available = [m for m in matches if not m.get('locked') and m.get('equipped_to') not in context['locked']
                     and final_owner(m) in (None, base)]
        nearby = [m for m in context['mods'] if m['slot_id'] == example['slot_id']
                  and m['set_id'] == example['set_id']
                  and m['primary']['unit_stat_id'] == example['primary']['unit_stat_id']]
        def closeness(mod):
            values = {s['unit_stat_id']: float(s['unscaled']) for s in mod['secondaries']}
            return sum(min(1, values.get(s['unit_stat_id'], 0) / max(float(s['unscaled']), 1)) for s in example['secondaries'])
        candidates = [dict(mod=m, owner=final_owner(m), locked=bool(m.get('locked') or m.get('equipped_to') in context['locked']))
                      for m in sorted(nearby, key=lambda m: (-closeness(m), -m['level'], m['id']))[:3]]
        needs.append(dict(mod=example, recipe=recipes[len(needs)], owned=len(matches), available=len(available),
                          available_ids=[m['id'] for m in available],
                          status='available' if available else 'occupied' if matches else 'missing', candidates=candidates))
    return dict(recipes=recipes, needs=needs, closed=closed, gains=gains,
                kind='shortage' if not old['full_pass'] else 'upgrade',
                completes=check['full_pass'], deficit_gain=deficit_gain,
                score=(required, optional, deficit_gain, gain, -len(examples)))


def farm_recipes(context, base):
    """Diverse examples, ranked cheaply before exact single/pair checks."""
    rows = [r for group in context['rows'][base].values() for r in group]
    proxy = {1: ('Health', 'Total Life'), 55: ('Health', 'Total Life'), 28: ('Protection', 'Total Life'),
             56: ('Protection', 'Total Life'), 5: ('Speed',), 41: ('Physical Damage', 'Special Damage'),
             48: ('Physical Damage', 'Special Damage'), 42: ('Armor', 'Resistance'), 49: ('Armor', 'Resistance'),
             16: ('Critical Damage',), 17: ('Potency',), 18: ('Tenacity',),
             53: ('Physical Critical Chance', 'Special Critical Chance')}
    wanted = {r[3] for r in rows if r[4] == '>='}
    sources = [m for m in context['mods'] if m['level'] == 15 and m['rarity'] in (5, 6)]
    maxima = defaultdict(lambda: 1)
    for m in sources:
        for s in [m['primary'], *m['secondaries']]:
            maxima[s['unit_stat_id']] = max(maxima[s['unit_stat_id']], float(s['unscaled']))
    def score(mod):
        return sum(float(s['unscaled']) / maxima[s['unit_stat_id']]
                   for s in [mod['primary'], *mod['secondaries']] if wanted.intersection(proxy.get(s['unit_stat_id'], ())))
    # Preserve a secondary profile for each relevant stat as well as the balanced one.
    profiles = sorted(sources, key=lambda m: (-score(m), m['id']))[:3]
    for stat_id, names in proxy.items():
        if wanted.intersection(names) and sources:
            best = max(sources, key=lambda m: sum(float(s['unscaled']) for s in m['secondaries'] if s['unit_stat_id'] == stat_id))
            if best not in profiles:
                profiles.append(best)
    current = {m['slot_id']: m for m in context['chosen'][base]['mods']}
    by_slot = defaultdict(list)
    for slot in range(1, 7):
        if current.get(slot, {}).get('locked'):
            continue
        options = []
        for source in ([*profiles, *[m for m in current.values() if m not in profiles]] or [None]):
            if source and source['rarity'] == 5:
                if source['slot_id'] != slot:
                    continue
                primaries = [source['primary']['unit_stat_id']]
            else:
                primaries = [p['unit_stat'] for p in stat_engine.MOD_PRIMARY_OPTIONS[optimizer.SLOTS[slot - 1]]]
            for sid in stat_engine.MOD_SET_PIECE_COUNT:
                for primary in primaries:
                    if source and any(s['unit_stat_id'] == primary for s in source['secondaries']):
                        continue
                    recipe = dict(source=source['id'] if source else '', slot=slot, set=sid, primary=primary)
                    example = farm_mod(recipe, context['indexed'])
                    if example['rarity'] == 6 and context['units'][base].get('currentTier', 0) < 12:
                        continue
                    priority = score(example)
                    for r in rows:
                        if r[3] == 'ModPrimary' and r[12] == optimizer.SLOTS[slot - 1]:
                            if primary in (optimizer._ids(r[11]) or {int(r[5])}):
                                priority += 3 if r[6] == 'required' else 1
                    options.append((priority, recipe, example))
        seen, counts = set(), Counter()
        for priority, recipe, example in sorted(options, key=lambda t: -t[0]):
            signature = farm_signature(example)
            if signature in seen or counts[example['set_id']] >= 2:
                continue
            seen.add(signature)
            counts[example['set_id']] += 1
            by_slot[slot].append(recipe)
    return by_slot


def recommend(context, base):
    if (base in context['locked'] or base not in context['chosen']
            or context['units'][base].get('currentLevel', 85) < 50):
        return []
    by_slot = farm_recipes(context, base)
    found, ranked = [], defaultdict(list)
    for slot, recipes in by_slot.items():
        for recipe in recipes:
            trial = farm_trial(context, base, [recipe], describe=False)
            if trial:
                found.append(trial)
            # Keep every set represented, including replacements that alone break a set.
            ranked[slot].append((trial['score'] if trial else (-1, -1, -1, -1, -1), recipe))
    pair_recipes = {}
    for slot, entries in ranked.items():
        counts = Counter()
        selected = []
        for _, recipe in sorted(entries, key=lambda t: t[0], reverse=True):
            if not counts[recipe['set']]:
                selected.append(recipe)
                counts[recipe['set']] += 1
        pair_recipes[slot] = selected
    for left, right in itertools.combinations(pair_recipes, 2):
        for a, b in itertools.product(pair_recipes[left], pair_recipes[right]):
            trial = farm_trial(context, base, [a, b], describe=False)
            if trial:
                found.append(trial)
    kept, seen = [], set()
    for trial in sorted(found, key=lambda t: t['score'], reverse=True):
        signature = tuple(farm_signature(n['mod']) for n in trial['needs'])
        # Prefer the simpler single replacement when a second mod adds no benefit.
        if signature in seen or any(t['recipes'][0] in trial['recipes'] for t in kept if len(t['recipes']) == 1):
            continue
        seen.add(signature)
        kept.append(trial)
        if len(kept) == 3:
            break
    return [farm_trial(context, base, trial['recipes']) for trial in kept]


def verify(context, proposals):
    if not isinstance(proposals, dict) or set(proposals) - set(context['chosen']):
        raise ValueError('Некорректные рекомендации модулей.')
    checked = {}
    for base, alternatives in proposals.items():
        if not isinstance(alternatives, list) or len(alternatives) > 3:
            raise ValueError('Слишком много рекомендаций.')
        checked[base] = [trial for recipes in alternatives if (trial := farm_trial(context, base, recipes))]
    return checked
