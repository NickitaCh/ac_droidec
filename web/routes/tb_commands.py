"""Manual and automatic in-game TB orders, restricted to connected officers."""
import asyncio
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
import secrets
import time
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates

from services import feature_flags, game_account as ga, game_watch, tb_commands as orders, tb_live, tb_forecast

router = APIRouter()


@router.on_event('startup')
async def restore_rules():
    await orders.restore()


templates = Jinja2Templates(directory=str(Path(__file__).resolve().parents[1] / 'templates'))
_access = feature_flags.require_feature('tb_reports')


def _code(user):
    owner = str(user['discord_id'])
    ga.require_game_officer(owner, user['guild_id'], user.get('bot_profile'), user['tier'])
    return ga.select_account(owner, user['guild_id'])


def render(request, user, error=None):
    try:
        code = _code(user)
    except ga.AccountError as denied:
        raise HTTPException(403, str(denied)) from None
    recent = tb_live.history(user['guild_id'], str(user['discord_id']), 1)
    state = recent[0][1] if recent else None
    cfg = orders.config(user['guild_id'])
    available = orders.targets(state)
    planets = [dict(row, key=key, threshold=max(0, (row.get('points') or 0)))
               for key, row in available.items() if row['kind'] == 'deployment']
    # Suggested stop point: 20M before the last star; the officer can edit it.
    defs = tb_forecast.zones().get('planets', {})
    for planet in planets:
        where = tb_forecast.planet_of(planet['planet'])
        stars = defs.get(where[1], {}).get('stars', []) if where else []
        planet['threshold'] = max(0, stars[-1] - 20_000_000) if stars else 0
    request.session.setdefault('tb_commands_csrf', secrets.token_urlsafe(32))
    log = orders.history(user['guild_id'])
    for row in log:
        row['at_text'] = datetime.fromtimestamp(row['at'], ZoneInfo('Europe/Moscow')).strftime('%d.%m %H:%M:%S')
    return templates.TemplateResponse(request, 'tb_commands.html', {
        'user': user, 'code': code, 'state': state, 'captured': recent[0][0] if recent else None,
        'targets': available, 'planets': planets, 'states': orders.STATES, 'cfg': cfg,
        'rules': cfg['rules'] if cfg and state and (cfg['event'], cfg['phase']) == (state['event'], state['phase']) else [],
        'revision': cfg['revision'] if cfg else 0, 'log': log,
        'stale': bool(state and state['end_ms'] <= time.time() * 1000),
        'csrf': request.session['tb_commands_csrf'], 'error': error,
        'watch': game_watch.get(user['guild_id'], str(user['discord_id']), 'tb'),
    }, headers={'Cache-Control': 'no-store'})


@router.get('/tb/commands')
def page(request: Request, user: dict = Depends(_access)):
    try:
        return render(request, user)
    except ga.AccountError as error:
        from fastapi import HTTPException
        raise HTTPException(403, str(error)) from None


def _million(value):
    try:
        result = Decimal(value) * 1_000_000
        if not result.is_finite() or result < 0 or result > 10**12:
            raise ValueError()
        return int(result)
    except (InvalidOperation, ValueError, TypeError):
        raise ga.AccountError('Введите неотрицательное число миллионов очков.') from None


def _rule(form, state):
    conditions = []
    for zone in state['zones']:
        if form.get('points:' + zone, '').strip():
            conditions.append({'kind': 'points', 'zone': zone, 'value': _million(form['points:' + zone])})
    if form.get('minutes', '').strip():
        conditions.append({'kind': 'remaining', 'value': int(form['minutes']) * 60})
    for kind in ('platoons', 'below_max'):
        if form.get(kind):
            conditions.append({'kind': kind, 'zone': str(form[kind])})
    return {'id': str(form.get('rule_id') or secrets.token_hex(6)), 'name': str(form['name']).strip(),
            'priority': int(form['priority']), 'target': str(form['target']), 'command': int(form['command']),
            'message': str(form.get('message', '')), 'enabled': form.get('enabled') == 'on', 'conditions': conditions}


@router.post('/tb/commands')
async def action(request: Request, user: dict = Depends(_access)):
    form = await request.form()
    csrf = request.session.get('tb_commands_csrf')
    if not csrf or not secrets.compare_digest(str(form.get('csrf', '')).encode(), csrf.encode()):
        from fastapi import HTTPException
        raise HTTPException(403)
    try:
        code = await asyncio.to_thread(_code, user)
        kind = form.get('action')
        if kind == 'refresh':
            await orders.read(user, code)
        elif kind == 'manual':
            await orders.change(user, code, str(form['event']), int(form['phase']), str(form['target']),
                                int(form['command']), str(form.get('message', '')),
                                [int(form['old_command']), str(form.get('old_message', ''))])
        else:
            async with orders.locked(user['guild_id']):
                state = await orders.read(user, code)
                orders.active(state, str(form['event']), int(form['phase']))
                cfg = await asyncio.to_thread(orders.config, user['guild_id'])
                rules = cfg['rules'] if cfg and (cfg['event'], cfg['phase']) == (state['event'], state['phase']) else []
                enabled = bool(cfg and cfg['enabled'])
                if kind == 'recipe':
                    thresholds = {orders.targets(state)[target]['planet']: _million(form['threshold:' + target])
                                  for target in form.getlist('planet') if target in orders.targets(state)}
                    if not thresholds:
                        raise ga.AccountError('Выберите планеты, которые закрываем на этом этапе.')
                    rules = orders.recipe(state, thresholds, _million(form.get('buffer', '5')), int(form.get('final', '60')))
                    enabled = False
                elif kind == 'save_rule':
                    rule = _rule(form, state)
                    if form.get('rule_id') and not any(r['id'] == rule['id'] for r in rules):
                        raise ga.AccountError('Правило уже удалено. Обновите страницу.')
                    rules = [r for r in rules if r['id'] != rule['id']] + [rule]
                elif kind == 'delete':
                    rules = [r for r in rules if r['id'] != form.get('rule_id')]
                elif kind == 'pause':
                    enabled = False
                elif kind == 'enable':
                    if not rules:
                        raise ga.AccountError('Добавьте правила.')
                    enabled = True
                else:
                    raise ga.AccountError('Неизвестное действие.')
                await asyncio.to_thread(orders.save_rules, user, code, state, rules, enabled, int(form['revision']))
            if enabled:
                # Only reuse the saved game session. Never evict an officer from
                # the game automatically to make an automation work again.
                await game_watch.start(str(user['discord_id']), user['guild_id'], user['bot_profile'],
                                       user['tier'], code, 'tb', 60, saved_session=True)
        return RedirectResponse('/tb/commands', status_code=303)
    except (ga.AccountError, ValueError, KeyError) as error:
        return await asyncio.to_thread(render, request, user,
                                       str(error) if isinstance(error, ga.AccountError) else 'Проверьте поля формы.')
