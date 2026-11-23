"""Owner-bound TW deployment preview and explicit execution."""
import asyncio
from pathlib import Path
import secrets

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

import database
from services import game_account as ga, tw_game_deploy as deploy, tw_def_notify, datacron_catalog
from tw_def_engine import ZONES_BY_KEY
from web.deps import require_officer_access

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parents[1] / 'templates'))


def datacron_sort_key(dc):
    if not dc:
        return (-1, 0)
    try:
        season = int(dc.get('setId'))
    except (TypeError, ValueError):
        season = -1
    return (season, len(dc.get('affix') or []))


def datacron_option(dc_id, dc, catalog, index):
    if not dc:
        return {'id': dc_id, 'name': f'№{index} · ДК без описания', 'details': dc_id}
    from cogs.datacron_requirements import DATACRON_STAT_SCALE, DATACRON_STAT_SHORT_LABELS

    season = ((catalog or {}).get('seasons') or {}).get(dc.get('setId'), {})
    title = tw_def_notify.describe_datacron(dc, catalog).partition(' — ')[0]
    short, full = [], []
    affixes = dc.get('affix') or []
    labels = {**DATACRON_STAT_SHORT_LABELS, 25: 'Пробитие брони'}
    stats = {}
    for affix in affixes:
        stat = affix.get('statType')
        if affix.get('abilityId') or stat not in labels:
            continue
        try:
            value = int(affix.get('statValue', 0))
        except (TypeError, ValueError):
            continue
        if value:
            stats[stat] = stats.get(stat, 0) + value
    stats_text = ' · '.join(
        f'{labels[stat]} {value / DATACRON_STAT_SCALE:.1f}%'.replace('.0%', '%')
        for stat, value in sorted(stats.items(), key=lambda item: -item[1])
    )
    focused = bool(dc.get('focused'))
    focused_effects = season.get('focused_effects', {}).get(dc.get('templateId'), {})
    levels = range(1, len(affixes) + 1) if focused else (3, 6, 9)
    for level in levels:
        ability = affixes[level - 1].get('abilityId') if len(affixes) >= level else None
        if not ability:
            continue
        if focused:
            text = focused_effects.get(str(level), {}).get(ability) or 'Описание эффекта недоступно'
            label = text
        else:
            label = dict(season.get(f'level{level}') or []).get(ability, 'Описание эффекта недоступно')
            text = (season.get('abilities', {}).get(ability) or {}).get('full')
            branch = next((name for name, key in season.get('entries', {}).get(level, []) if key == ability), '')
            text = text.replace('{0}', branch) if text else label
        snippet = label if len(label) <= 36 else label[:35].rstrip() + '…'
        short.append(f'{level}: {snippet}')
        full.append(f'{level}: {text}')
    name = f'№{index} · {title}' + (' · ' + ' · '.join(short) if short else '')
    return {'id': dc_id, 'name': name, 'title': title, 'stats': stats_text, 'details': '\n'.join(full)}


def render(request, user, ticket, error=None, live=None, choices=None, login_required=False):
    record = deploy.load(user, ticket)
    request.session.setdefault('tw_deploy_csrf', secrets.token_urlsafe(32))
    bases = [u for a in record['data']['assignments'] for u in a['units']]
    names = database.get_game_unit_names(bases)
    dcs = database.get_player_datacrons_bulk([record['code']]).get(record['code'], [])
    cached = {str(dc['id']): dc for dc in dcs if dc.get('id')}
    available_ids = set(live['datacrons']) - live['used_dc'] if live else set()
    ordered_ids = [dc_id for dc_id in cached if dc_id in available_ids]
    ordered_ids.extend(sorted(available_ids.difference(cached)))
    ordered_ids.sort(key=lambda dc_id: datacron_sort_key(cached.get(dc_id)), reverse=True)
    options = [datacron_option(dc_id, cached.get(dc_id), datacron_catalog._cache, index)
               for index, dc_id in enumerate(ordered_ids, 1)]
    availability = []
    if live:
        try:
            rows = deploy.suggested_assignments(record, live, dcs, datacron_catalog._cache)
            for i, row in enumerate(rows):
                if choices is not None and str(i) in choices:
                    row['datacron'] = choices[str(i)]
                    row.pop('datacron_source', None)
            availability = deploy.inspect_assignments(live, record['event'], rows)
            record['data']['assignments'] = rows
        except ga.AccountError as exc:
            error = error or str(exc)
    return templates.TemplateResponse(request, 'tw_game_deploy.html', {
        'user': user, 'record': record, 'names': names, 'zones': ZONES_BY_KEY,
        'options': options, 'availability': availability, 'live_checked': bool(live),
        'error': error, 'csrf': request.session['tw_deploy_csrf'], 'login_required': login_required,
    }, headers={'Cache-Control': 'no-store', 'Referrer-Policy': 'no-referrer'})


@router.get('/game-account/tw-defense/{ticket}', response_class=HTMLResponse)
async def page(request: Request, ticket: str, user: dict = Depends(require_officer_access)):
    try:
        record = await asyncio.to_thread(deploy.load, user, ticket)
    except ga.AccountError as exc:
        raise HTTPException(403, str(exc)) from None
    return await display(request, user, ticket, record)


async def display(request, user, ticket, record, error=None, choices=None, login_required=False):
    live = None
    try:
        live = await deploy.snapshot(user, record['code'])
    except ga.SessionExpired:
        login_required = True
        error = error or 'Сессия игры истекла. Войдите заново, чтобы проверить защиту.'
    except ga.AccountError as exc:
        error = error or str(exc)
    try:
        from swgoh_comlink import SwgohComlink
        await datacron_catalog.get_catalog(SwgohComlink(url='http://localhost:3000'))
    except Exception:
        error = error or 'Каталог датакронов недоступен. Обновите страницу перед выбором ДК.'
    return await asyncio.to_thread(render, request, user, ticket, error, live, choices, login_required)


@router.post('/game-account/tw-defense/{ticket}', response_class=HTMLResponse)
async def action(request: Request, ticket: str, csrf: str = Form(...),
                 action: str = Form(...), nonce: str = Form(''),
                 confirm_login: bool = Form(False), confirm_deploy: bool = Form(False),
                 user: dict = Depends(require_officer_access)):
    expected = request.session.get('tw_deploy_csrf', '')
    if not expected or not secrets.compare_digest(expected, csrf):
        raise HTTPException(403)
    error = None
    choices = None
    login_required = False
    try:
        record = await asyncio.to_thread(deploy.load, user, ticket)
        if action in ('prepare', 'refresh', 'reconnect'):
            form = await request.form()
            choices = {str(i): str(form.get('dc_' + str(i), ''))
                       for i in range(len(record['data']['assignments'])) if 'dc_' + str(i) in form}
            try:
                if action in ('prepare', 'reconnect'):
                    await deploy.prepare(user, ticket, choices)
                else:
                    await deploy.snapshot(user, record['code'])
            except ga.SessionExpired:
                if not confirm_login and action != 'reconnect':
                    login_required = True
                    raise ga.AccountError('Сессия игры истекла. Войдите заново, чтобы проверить защиту.') from None
                await ga.reconnect(str(user['discord_id']), user['guild_id'], user['bot_profile'],
                                   user['tier'], record['code'], optimizer_read=user['bot_profile'] == 'prod')
                if action in ('prepare', 'reconnect'):
                    await deploy.prepare(user, ticket, choices)
        elif action == 'apply' and confirm_deploy:
            await deploy.apply(user, ticket, nonce)
        elif action == 'edit' and record['state'] == 'ready':
            data = record['data']
            data.pop('nonce', None)
            data.pop('preview', None)
            deploy._save(ticket, data, 'draft', expected='ready')
        else:
            raise ga.AccountError('Подтвердите выставление защиты.')
    except ga.AccountError as exc:
        error = str(exc)
    return await display(request, user, ticket, await asyncio.to_thread(deploy.load, user, ticket), error, choices, login_required)
