"""Decode the explicit HotUtils squads export; keep the original alongside imports."""
import json


def convert(squad: dict) -> dict:
    definition = squad.get('definition')
    if not isinstance(definition, dict) or definition.get('structure') not in ('char5', 'char3', 'fleet'):
        raise ValueError('Неизвестная структура отряда HotUtils.')
    kind = 'ship' if definition['structure'] == 'fleet' else 'character'
    groups = definition.get('flexGroups') or {}
    slots, warnings = [], []
    for slot in definition.get('slots') or []:
        if slot.get('unit'):
            candidates, rules = [slot['unit']], []
        elif slot.get('flexGroup') in groups:
            group = groups[slot['flexGroup']]
            candidates, rules = group.get('candidates') or [], group.get('rules') or []
        else:
            raise ValueError('Пустой слот или неизвестная группа замен HotUtils.')
        if not candidates:
            raise ValueError('Группа замен HotUtils не содержит юнитов.')
        options = []
        for base_id in candidates:
            opt = {'base_id': base_id}
            # HotUtils has no implicit seven-star requirement for ships.
            if kind == 'ship':
                opt['min_stars'] = 1
            for rule in list(slot.get('requirements') or []) + list(rules):
                if rule.get('type') == 'preference':
                    metric = {'relic': 'реликвия', 'gp': 'ГМ'}.get(rule.get('metric'))
                    if not metric:
                        raise ValueError('Неизвестный приоритет замен HotUtils.')
                    warning = f'Приоритет замен в HotUtils: {metric}. Здесь сохранён порядок вариантов из файла.'
                    if warning not in warnings:
                        warnings.append(warning)
                    continue
                if rule.get('unit') and rule['unit'] != base_id:
                    continue
                requirement = rule.get('requires')
                if requirement in ('relic', 'stars'):
                    key, source = ('min_relic', 'minTier') if requirement == 'relic' else ('min_stars', 'minStars')
                    value = rule.get(source)
                    if isinstance(value, bool) or not isinstance(value, int):
                        raise ValueError('Некорректный уровень требования HotUtils.')
                    opt[key] = max(opt.get(key, value), value)
                elif requirement == 'omicron' and isinstance(rule.get('abilityId'), str):
                    opt.setdefault('required_omicrons', [])
                    if rule['abilityId'] not in opt['required_omicrons']:
                        opt['required_omicrons'].append(rule['abilityId'])
                else:
                    raise ValueError('Неизвестное требование HotUtils; импорт остановлен.')
            options.append(opt)
        slots.append({'options': options})
    notes = ['HotUtils · ' + ' · '.join(str(c) for c in squad.get('categories') or [])]
    if definition.get('datacrons'):
        notes.append('Рекомендация датакрона сохранена в исходном экспорте; привяжите требование ДК в редакторе отряда.')
    notes.extend(warnings)
    return {'name': squad.get('name'), 'combat_type': kind, 'slots': slots,
            'note': '\n'.join(notes), 'source': squad}


def ensure_sources(cursor):
    cursor.execute('''CREATE TABLE IF NOT EXISTS squad_import_sources (
        guild_id INTEGER NOT NULL, squad_id INTEGER NOT NULL, source_json TEXT NOT NULL,
        PRIMARY KEY (guild_id, squad_id))''')


def save_source(cursor, guild_id, squad_id, source):
    ensure_sources(cursor)
    cursor.execute('INSERT OR REPLACE INTO squad_import_sources VALUES (?, ?, ?)',
                   (guild_id, squad_id, json.dumps(source, ensure_ascii=False)))
