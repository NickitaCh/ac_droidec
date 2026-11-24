"""Localized, maximum-level ability descriptions from Comlink. No LLM facts."""

import asyncio
import json
import re
import time
from datetime import datetime, timedelta, timezone

import database
from services import unit_aliases
from services.sync_queue import serialized_sync

STATE_KEY = 'unit_ability_catalog'
_MARKUP = re.compile(r'\[[0-9a-fA-F]{6,8}\]|\[-\]|\[/?[a-zA-Z]\]')


def clean(text):
    return _MARKUP.sub('', text or '').replace('\\n', '\n').strip()


def parse(units, skills, abilities, loc):
    by_skill = {s['id']: s.get('abilityReference') for s in skills if s.get('id')}
    by_ability = {a['id']: a for a in abilities if a.get('id')}
    result = {}
    for unit in units:
        bid = unit.get('baseId')
        if not bid or not unit.get('obtainable') or unit.get('obtainableTime') != '0' or bid in result:
            continue
        refs = [by_skill.get(ref.get('skillId')) for ref in unit.get('skillReference') or []]
        refs += [ref.get('abilityId') for ref in unit.get('limitBreakRef') or []]
        rows = []
        for aid in dict.fromkeys(refs):
            ability = by_ability.get(aid)
            if not ability or aid.startswith('hiddenability_'):
                continue
            tiers = ability.get('tier') or []
            desc_key = (tiers[-1].get('descKey') if tiers else None) or ability.get('descKey')
            name = clean(loc.get(ability.get('nameKey'), ''))
            description = clean(loc.get(desc_key, ''))
            if name and description:
                rows.append({'id': aid, 'name': name, 'description': description})
        if rows:
            result[bid] = {'name': clean(loc.get(unit.get('nameKey'), bid)), 'abilities': rows}
    return result


def load():
    raw = database.get_bot_state(STATE_KEY, guild_id=0)
    return json.loads(raw) if raw else {'updated_at': 0, 'units': {}}


def fetch_and_store(comlink):
    units = comlink.get_game_data(items='137438953472').get('units') or []
    skills = comlink.get_game_data(items='4').get('skill') or []
    abilities = comlink.get_game_data(items='2097152').get('ability') or []
    raw = comlink.get_localization(locale='RUS_RU', unzip=True)
    loc = {key.strip(): value.strip() for line in raw.get('Loc_RUS_RU.txt', '').splitlines()
           if '|' in line for key, _, value in [line.partition('|')]}
    data = parse(units, skills, abilities, loc)
    if not data:
        raise ValueError('Пустой справочник способностей; предыдущая версия сохранена.')
    database.set_bot_state(STATE_KEY, json.dumps({'updated_at': time.time(), 'units': data}, ensure_ascii=False), guild_id=0)
    return len(data)


@serialized_sync('описания способностей', quiet=True)
async def refresh(comlink):
    cached = await asyncio.to_thread(load)
    if cached['units'] and time.time() - cached['updated_at'] < 86400:
        return
    await asyncio.to_thread(fetch_and_store, comlink)


def normalize(text):
    return ' '.join(re.findall(r'\w+', text.casefold().replace('ё', 'е')))


def candidates(question, data):
    q = ' ' + normalize(question) + ' '
    found = []
    for bid, unit in data['units'].items():
        names = [unit['name'], bid, *unit_aliases.aliases(bid)]
        score = max((len(normalize(name)) for name in names if len(normalize(name)) >= 2 and ' ' + normalize(name) + ' ' in q), default=0)
        if score:
            found.append((score, bid))
    return [bid for _, bid in sorted(found, reverse=True)[:2]]


def context_for(question, data):
    return {bid: data['units'][bid] for bid in candidates(question, data)}


def evidence_pages(plan, data, question):
    unit = data['units'][plan['unit']]
    selected = [a for a in unit['abilities'] if a['id'] in plan['ids']]
    dodge = bool(re.search(r'промах|уверну|уклон|попада', question, re.I))
    pages = []
    for ability in selected:
        text = ability['description']
        if dodge:
            sentences = re.split(r'(?<=[.!?])\s+|\n+', text)
            relevant = [s for s in sentences if re.search(r'уверну|уклон|промах|попада', s, re.I)]
            if relevant:
                text = '\n'.join(relevant)
        # Preserve whole paragraphs/sentences where possible, splitting only long text.
        while text:
            cut = min(1450, len(text))
            if cut < len(text):
                cut = max(text.rfind('\n', 0, cut), text.rfind('. ', 0, cut), cut if cut < 200 else 0) or 1450
            piece, text = text[:cut].strip(), text[cut:].strip()
            pages.append(f"**{unit['name']} — {ability['name']}**\n\n{piece}")
    combined = []
    for page in pages:
        if combined and len(combined[-1]) + len(page) + 2 <= 1700:
            combined[-1] += '\n\n' + page
        else:
            combined.append(page)
    stamp = datetime.fromtimestamp(data['updated_at'], timezone(timedelta(hours=3))).strftime('%d.%m.%Y')
    return [page + f'\n\n*Источник: Comlink, максимальный уровень способности. Данные от {stamp}.*' for page in combined]
