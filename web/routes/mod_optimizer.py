"""Owner-scoped background mod allocation, Dry run and confirmed application.

The existing super-admin gate and TEST account-pilot restrictions remain intact.
Game credentials never enter jobs, HTML, URLs or browser storage.
"""

from pathlib import Path
from collections import Counter
import secrets
import asyncio
import logging
import math
import json
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException, Request, UploadFile, File, Form
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse, Response
from fastapi.templating import Jinja2Templates

import database
from services import game_account, game_account_readonly
import stat_engine
import services.stat_forecast as stat_forecast
import services.mod_optimizer as mod_optimizer
from services import mod_optimizer_v2, mod_optimizer_plans as plans, game_mod_actions, mod_optimizer_browser
from services import personal_plates
from services.registration import register_player
from web.deps import require_member_access

router = APIRouter()
loadouts_router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent.parent / "templates"))
templates.env.filters['plate_label'] = personal_plates.display_reference
_jobs = set()
_log = logging.getLogger(__name__)


def require_loadouts_access(user: dict = Depends(require_member_access)):
    try:
        game_account.require_account_read(str(user['discord_id']), user['guild_id'], user['bot_profile'], user['tier'])
    except game_account.AccountError:
        raise HTTPException(404, 'Страница недоступна.') from None
    return user


def require_optimizer_access(user: dict = Depends(require_loadouts_access)):
    return user


def can_apply(user):
    return user.get('bot_profile') == 'test' and user.get('guild_id') is not None and user.get('tier') in ('member', 'officer')


def can_apply_loadout(user):
    return can_apply(user) or (
        user.get('bot_profile') == 'prod' and user.get('tier') == 'officer'
        and bool(user.get('is_super_admin')))


def _fmt_stat(value, name: str) -> str:
    if value is None:
        return "—"
    if not isinstance(value, (float, int)):
        return str(value)
    suffix = "%" if name in stat_engine.PERCENT_STATS else ""
    if abs(value) >= 1000:
        return f"{value:,.0f}".replace(",", " ") + suffix
    text = f"{value:.2f}".rstrip("0").rstrip(".")
    return text + suffix


def _mod_summary(m: dict) -> str:
    p = m["primary"]
    p_text = f"{p['value']:g}{'%' if p['is_percent'] else ''} {stat_engine.UNIT_STAT_RU.get(p['unit_stat_id'], p['name'])}"
    return f"{stat_engine.MOD_SET_NAMES_RU.get(m['set_id'], m['set_name'])} · {mod_optimizer_v2.SLOT_LABELS[m['slot_id']-1]} · {p_text}"


def _enrich_results(results: list[dict]) -> None:
    """Мутирует results на месте — добавляет отформатированные строки для шаблона, чтобы не
    тащить форматирование статов в Jinja."""
    from cogs.stat_requirements import STAT_LABEL_SHORT
    peer_keys = {req['compare'] for r in results for req in r['passed'] + r['failed'] if req.get('compare')}
    peer_keys.update(c['owner'] for r in results for s in r.get('recommendations', [])
                     for n in s['needs'] for c in n.get('candidates', []) if c['owner'])
    peer_names = database.get_game_unit_names(list(peer_keys)) if peer_keys else {}
    for r in results:
        for req in r["passed"] + r["failed"]:
            if req['stat_name'] in STAT_LABEL_SHORT:
                req['label'] = STAT_LABEL_SHORT[req['stat_name']]
                if req.get('compare'):
                    req['label'] += ' vs ' + peer_names.get(req['compare'], req['compare'])
            req["current_fmt"] = _fmt_stat(req.get("current"), req["stat_name"])
            req["before_fmt"] = _fmt_stat(req.get("before"), req["stat_name"])
            req['now_fmt'] = _fmt_stat(req.get('now'), req['stat_name'])
            if req['stat_name'] == 'ModPrimary':
                req['current_fmt'] = stat_engine.UNIT_STAT_RU.get(req.get('current'), 'Нет')
                req['before_fmt'] = stat_engine.UNIT_STAT_RU.get(req.get('before'), 'Нет')
                req['now_fmt'] = stat_engine.UNIT_STAT_RU.get(req.get('now'), 'Нет')
            req["threshold_fmt"] = req.get('target_text') or f"{req['operator']} {_fmt_stat(req['threshold'], req['stat_name'])}"
        r["mods_summary"] = [_mod_summary(m) for m in r.get("used_mods", [])]
        for suggestion in r.get('recommendations', []):
            def secondary_text(mod):
                labels = {1: 'Здоровье', 28: 'Защита', 41: 'Атака', 42: 'Оборона', **stat_engine.UNIT_STAT_RU}
                return ', '.join(f"{labels.get(s['unit_stat_id'], s.get('name', 'Доп. стат'))} "
                                 f"+{s['value']:g}{'%' if s['is_percent'] else ''}"
                                 for s in mod['secondaries']) or 'Без дополнительных требований'
            for need in suggestion['needs']:
                need['summary'] = _mod_summary(need['mod'])
                need['secondaries_fmt'] = secondary_text(need['mod'])
                for candidate in need.get('candidates', []):
                    candidate['location'] = peer_names.get(candidate['owner'], candidate['owner']) if candidate['owner'] else 'Свободный склад'
                    candidate['secondaries_fmt'] = secondary_text(candidate['mod'])
            for gain in suggestion['gains']:
                gain.update(label=STAT_LABEL_SHORT.get(gain['stat_name'], gain['stat_name']),
                            before_fmt=_fmt_stat(gain['before'], gain['stat_name']),
                            after_fmt=_fmt_stat(gain['after'], gain['stat_name']))
            for req in suggestion['closed']:
                req['label'] = STAT_LABEL_SHORT.get(req['stat_name'], req['label'])
                req['threshold_fmt'] = req.get('target_text') or f"{req['operator']} {_fmt_stat(req['threshold'], req['stat_name'])}"


def _preview_moves(results, all_mods):
    equipped = {(mod["equipped_to"], mod["slot_id"]): mod for mod in all_mods if mod["equipped_to"]}
    displaced = {mod["id"]: result["char_name"] for result in results for mod in result.get("used_mods", [])}
    source_ids = {mod["equipped_to"] for mod in all_mods if mod["equipped_to"]}
    names = database.get_game_unit_names(sorted(source_ids)) if source_ids else {}
    for result in results:
        moves = []
        target = result["base_id"]
        for mod in result.get("used_mods", []):
            source = mod["equipped_to"]
            if source == target:
                continue
            previous = equipped.get((target, mod["slot_id"]))
            replaced = ""
            if previous:
                destination = displaced.get(previous["id"])
                replaced = _mod_summary(previous) + (f" → {destination}" if destination else " → свободный склад")
            moves.append({"mod": _mod_summary(mod), "source": names.get(source, source) if source else "Свободный склад",
                          "replaced": replaced, "locked": bool(previous and previous.get("locked"))})
        result["moves"] = moves


def _render(request, context):
    shopping = {}
    reserved = set()
    for result in context.get('results') or []:
        alternatives = result.get('recommendations', [])
        if not alternatives:
            continue
        for need in alternatives[0]['needs']:
            if need['status'] == 'available':
                spare = next((mid for mid in need.get('available_ids', []) if mid not in reserved), None)
                if spare:
                    reserved.add(spare)
                    continue
            mod = need['mod']
            key = (mod['set_id'], mod['slot_id'], mod['primary']['unit_stat_id'], mod['rarity'])
            item = shopping.setdefault(key, dict(summary=need['summary'], rarity=mod['rarity'], characters=[]))
            item['characters'].append(result['char_name'])
    context['mod_shopping'] = sorted(shopping.values(), key=lambda x: -len(x['characters']))
    context['can_apply'] = (can_apply_loadout if context.get('loadouts_page') else can_apply)(context['user'])
    if not context['can_apply']:
        context['dry_run'] = True
    request.session.setdefault("mod_optimizer_csrf", secrets.token_urlsafe(32))
    context["game_csrf"] = request.session["mod_optimizer_csrf"]
    return templates.TemplateResponse(request, "mod_optimizer_v2.html", context,
                                      headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"})


def _get_comlink():
    from swgoh_comlink import SwgohComlink
    return SwgohComlink(url="http://localhost:3000")


def _base_context(user: dict, guild_id: int) -> dict:
    plates = database.get_all_stat_requirement_plates(guild_id=guild_id)
    plates = list(plates) + [personal_plates.reference(p) for p in personal_plates.list_plates(user)]
    self_reg = database.get_user_registration(user["discord_id"], guild_id=guild_id)
    code = str(self_reg[0]) if self_reg else None
    registrations = database.get_user_registrations(user['discord_id'], guild_id=guild_id)
    context = {
        "user": user,
        "plates": plates,
        "self_ally_code": self_reg[0] if self_reg else None,
        "self_player_name": self_reg[1] if self_reg else None,
        "selected_plates": [],
        "results": None,
        "error": None,
        "loading": False,
        "game_available": bool(self_reg),
        'accounts': [(str(r[0]), r[1]) for r in registrations],
        'characters': [], 'locked': [], 'plan': None, 'donors': [], 'relic_mode': 'current',
        'can_apply': can_apply(user), 'dry_run': True,
    }
    return context


def _characters(code, roster=None):
    if roster is None:
        roster = database.get_player_units(code)
    types = database.get_unit_types(list(roster))
    ids = [base for base in roster if types.get(base) != 'ship']
    names = database.get_game_unit_names(ids)
    return sorted([(base, names.get(base, base)) for base in ids], key=lambda row: row[1])


def _csrf(request, value):
    expected = request.session.get('mod_optimizer_csrf')
    if not expected or not secrets.compare_digest(value.encode(), expected.encode()):
        raise HTTPException(403, 'Обновите страницу и повторите действие.')


def _spawn(coro):
    task = asyncio.create_task(coro)
    _jobs.add(task)
    task.add_done_callback(_jobs.discard)


@personal_plates.scoped_job
async def _calculate(token, user, plates, code, source, raw_csv, locked, force_refresh, relic_mode='current', dry_run=True, *, reconnect=False, thresholds=None, turn_order=(), deep_search=False):
    dry_run = bool(dry_run) or not can_apply(user)
    data = {'selected_plates': plates, 'locked': locked, 'game_source': source == 'game', 'ally_code': code,
            'relic_mode': relic_mode, 'dry_run': dry_run, 'deep_search': deep_search, 'thresholds': thresholds or {}, 'turn_order': turn_order}
    try:
        data['requirements_fingerprint'] = plans.requirements_fingerprint(plates, user['guild_id'])
        if source == 'game':
            if reconnect:
                await game_account.reconnect(str(user['discord_id']), user['guild_id'], user['bot_profile'],
                                             user['tier'], code, optimizer_read=True)
            snap = await game_mod_actions.snapshot(user, code, read_only=True)
            mods = snap['mods']
            data.update(fingerprint=snap['fingerprint'], connection=snap['connection'])
        else:
            mods = mod_optimizer.parse_mods_csv(raw_csv)
        comlink = _get_comlink()
        calc = await stat_forecast.get_stat_calc(comlink)
        if user.get('bot_profile') == 'prod' or user.get('optimizer_engine') == 'browser':
            if not mod_optimizer_browser.runtime_ready():
                raise ValueError('Вычислитель пока не установлен. Попробуйте позже.')
            members, rows, units, tiers = await mod_optimizer_v2.prepare_inputs(
                comlink, plates, code, mods, user['guild_id'], force_refresh,
                units_override=snap['roster'] if source == 'game' else None,
                relic_mode=relic_mode, thresholds=thresholds, turn_order=turn_order)
            names = await asyncio.to_thread(database.get_game_unit_names, list(rows.keys() | units.keys()))
            data['browser_inputs'] = dict(members=members, rows=rows, units=units, tiers=tiers, mods=mods,
                names=names, locked=locked, relic_mode=relic_mode, turn_order=turn_order, deep_search=deep_search)
            data['browser_engine'] = True
            plans.finish(token, 'browser', data)
            return
        results, passed, optimal, donors = await mod_optimizer_v2.optimize(
            comlink, calc, plates, code, mods, user['guild_id'], locked, force_refresh,
            units_override=snap['roster'] if source == 'game' else None, relic_mode=relic_mode,
            **({'deep_search': True} if deep_search else {}),
            **({'thresholds': thresholds} if thresholds else {}),
            **({'turn_order': turn_order} if turn_order else {}))
        _enrich_results(results)
        _preview_moves(results, mods)
        for donor in donors:
            from cogs.stat_requirements import STAT_LABEL_SHORT
            for r in donor['changes']:
                r['before_fmt'] = _fmt_stat(r['before'], r['stat_name'])
                r['current_fmt'] = _fmt_stat(r['current'], r['stat_name'])
                r['label'] = STAT_LABEL_SHORT.get(r['stat_name'], r['stat_name'])
        if source == 'game':
            # Exact plan validation before ever showing an Apply button.
            ops = game_mod_actions.operations(mods, results, set(locked))
            data['operation_count'] = len(ops)
        if plans.requirements_fingerprint(plates, user['guild_id']) != data['requirements_fingerprint']:
            raise ValueError('Требования плейтов изменились во время расчёта. Запустите новый Dry run.')
        data.update(results=results, plates_passed=passed, plates_total=len({p for r in results for p in r['plates']} & set(plates)),
                    mods_total=len(mods), solver_timed_out=not optimal, donors=donors)
        plans.finish(token, 'ready', data)
    except asyncio.CancelledError:
        plans.finish(token, 'error', data, 'Расчёт прерван перезапуском сервиса.')
        raise
    except game_account.ConnectionRequired as error:
        data['connection_action'] = 'login' if isinstance(error, game_account.SessionExpired) and not reconnect else 'link'
        plans.finish(token, 'error', data)
    except Exception as error:
        message = str(error) if isinstance(error, (ValueError, game_account.AccountError)) else 'Не удалось выполнить расчёт. Повторите позже.'
        if not isinstance(error, ValueError):
            _log.error('Mod optimizer calculation failed (%s)', type(error).__name__)
        plans.finish(token, 'error', data, message)


@personal_plates.scoped_job
async def _apply(token, user, code, data):
    try:
        if not (can_apply_loadout if data.get('preset_restore') else can_apply)(user) or data.get('dry_run', True):
            raise ValueError('Для этого пользователя или плана доступен только Dry run.')
        requirements_changed = (not data.get('preset_restore')
                                and data.get('requirements_fingerprint') != plans.requirements_fingerprint(
                                    data['selected_plates'], user['guild_id']))
        if requirements_changed or sorted(data.get('locked', [])) != plans.get_locks(user['discord_id'], user['guild_id'], code):
            raise ValueError('Требования или локи изменились. Запустите новый Dry run.')
        def save(updated):
            plans.finish(token, 'applying', updated, expected='applying')
        await game_mod_actions.apply(user, code, data, save)
        plans.finish(token, 'complete', data, 'Расстановка применена и проверена по данным игры.', expected='applying')
    except asyncio.CancelledError:
        plans.finish(token, 'partial', data, 'Применение прервано. Проверьте фактическую расстановку перед новым расчётом.', expected='applying')
        raise
    except game_account.ConnectionRequired as error:
        data['connection_action'] = 'login' if isinstance(error, game_account.SessionExpired) else 'link'
        partial = bool(data.get('in_flight') or data.get('completed_steps'))
        plans.finish(token, 'partial' if partial else 'error', data, expected='applying')
    except Exception as error:
        message = str(error) if isinstance(error, ValueError) else 'Связь с игрой прервалась. Проверьте фактическую расстановку; автоматического повтора нет.'
        plans.finish(token, 'partial' if data.get('in_flight') or data.get('completed_steps') else 'error', data, message, expected='applying')


@router.get("", response_class=HTMLResponse)
async def optimizer_form(request: Request, plan: str = '', ally_code: str = '', user: dict = Depends(require_optimizer_access)):
    context = _optimizer_context(user, plan, ally_code)
    if plan and _is_loadout(context['plan']):
        return RedirectResponse('/mod-loadouts?plan=' + context['plan']['id'], status_code=303)
    return _render(request, context)


@router.get('/browser/inputs')
async def browser_inputs(plan: str, user: dict = Depends(require_optimizer_access)):
    try:
        job = await asyncio.to_thread(plans.get, plan, user['discord_id'], user['guild_id'])
        if job['expired'] or job['state'] != 'browser':
            raise ValueError('Расчёт устарел или уже завершён.')
        await asyncio.to_thread(game_account.select_account, str(user['discord_id']), user['guild_id'], ally_code=job['ally_code'])
        with personal_plates.scope(user):
            fingerprint = await asyncio.to_thread(plans.requirements_fingerprint, job['data']['selected_plates'], user['guild_id'])
        if fingerprint != job['data']['requirements_fingerprint']:
            raise ValueError('Требования изменились. Запустите новый подбор.')
        calc = await stat_forecast.get_stat_calc(_get_comlink())
        payload = {**job['data']['browser_inputs'], 'calculator': mod_optimizer_browser.calculator_data(calc)}
        body = await asyncio.to_thread(json.dumps, {'payload': payload, 'source': mod_optimizer_browser.python_source()}, ensure_ascii=False)
        return Response(body, media_type='application/json', headers={'Cache-Control': 'no-store'})
    except ValueError as error:
        raise HTTPException(400, str(error)) from None


@router.post('/browser/result')
async def browser_result(request: Request, user: dict = Depends(require_optimizer_access)):
    raw = bytearray()
    async for chunk in request.stream():
        raw.extend(chunk)
        if len(raw) > 256 * 1024:
            raise HTTPException(413, 'Результат слишком большой.')
    try:
        message = json.loads(raw)
        if not isinstance(message, dict) or not isinstance(message.get('plan'), str):
            raise ValueError('Некорректный результат подбора.')
        _csrf(request, message.get('csrf', ''))
        token = message['plan']
        job = await asyncio.to_thread(plans.get, token, user['discord_id'], user['guild_id'])
        if job['expired'] or job['state'] != 'browser':
            raise ValueError('Расчёт устарел или уже завершён.')
        data = job['data']
        if message.get('cancel'):
            data.pop('browser_inputs', None)
            await asyncio.to_thread(plans.finish, token, 'error', data, 'Подбор отменён.', expected='browser')
            return JSONResponse({'url': '/mod-optimizer?plan=' + token})
        await asyncio.to_thread(game_account.select_account, str(user['discord_id']), user['guild_id'], ally_code=job['ally_code'])
        with personal_plates.scope(user):
            fingerprint = await asyncio.to_thread(plans.requirements_fingerprint, data['selected_plates'], user['guild_id'])
        if fingerprint != data['requirements_fingerprint']:
            raise ValueError('Требования изменились. Запустите новый подбор.')
        calc = await stat_forecast.get_stat_calc(_get_comlink())
        prepared = data['browser_inputs']
        results, passed, donors = await asyncio.to_thread(mod_optimizer_browser.assess, calc, prepared, message['allocation'],
                                                        message.get('recommendations'))
        _enrich_results(results)
        _preview_moves(results, prepared['mods'])
        for donor in donors:
            from cogs.stat_requirements import STAT_LABEL_SHORT
            for row in donor['changes']:
                row.update(before_fmt=_fmt_stat(row['before'], row['stat_name']),
                           current_fmt=_fmt_stat(row['current'], row['stat_name']),
                           label=STAT_LABEL_SHORT.get(row['stat_name'], row['stat_name']))
        if data.get('game_source'):
            data['operation_count'] = len(game_mod_actions.operations(prepared['mods'], results, set(data['locked'])))
        with personal_plates.scope(user):
            current = await asyncio.to_thread(plans.requirements_fingerprint, data['selected_plates'], user['guild_id'])
        if current != data['requirements_fingerprint']:
            raise ValueError('Требования изменились во время проверки. Запустите новый подбор.')
        data.update(results=results, plates_passed=passed, donors=donors, mods_total=len(prepared['mods']),
                    plates_total=len(prepared['members']), solver_timed_out=True)
        data.pop('browser_inputs')
        await asyncio.to_thread(plans.finish, token, 'ready', data, expected='browser')
        return JSONResponse({'url': '/mod-optimizer?plan=' + token})
    except (ValueError, KeyError, TypeError) as error:
        raise HTTPException(400, str(error) if isinstance(error, ValueError) else 'Некорректный результат подбора.') from None


@loadouts_router.get("", response_class=HTMLResponse)
async def loadouts_form(request: Request, plan: str = '', ally_code: str = '', user: dict = Depends(require_loadouts_access)):
    context = _optimizer_context(user, plan, ally_code)
    if plan and not _is_loadout(context['plan']):
        return RedirectResponse('/mod-optimizer?plan=' + context['plan']['id'], status_code=303)
    context['loadouts_page'] = True
    context['alt_error'] = request.query_params.get('alt_error')
    context['alt_added'] = request.query_params.get('alt_added')
    context['alt_outside_guild'] = request.query_params.get('outside_guild') == '1'
    return _render(request, context)


@loadouts_router.post('/alt')
async def loadouts_add_alt(request: Request, ally_code: str = Form(), csrf: str = Form(),
                           user: dict = Depends(require_loadouts_access)):
    _csrf(request, csrf)
    result = await register_player(_get_comlink(), str(user['discord_id']), ally_code, is_alt=True)
    if not result.ok:
        query = {'alt_error': result.error}
    elif result.guild_id == user['guild_id']:
        query = {'ally_code': result.ally_code, 'alt_added': result.ingame_name}
    else:
        query = {'alt_added': result.ingame_name, 'outside_guild': 1}
    return RedirectResponse('/mod-loadouts?' + urlencode(query), status_code=303)


def _is_loadout(job):
    return job['state'] == 'saved' or job['data'].get('preset_restore', False)


def _optimizer_context(user, plan='', ally_code=''):
    context = _base_context(user, user["guild_id"])
    code = ally_code or context['self_ally_code']
    if plan:
        try:
            job = plans.get(plan, user['discord_id'], user['guild_id'])
        except ValueError:
            raise HTTPException(404, 'План не найден.') from None
        context.update(job['data'])
        context['can_apply'] = can_apply(user)
        context['deep_search'] = job['data'].get('deep_search', False)
        context['dry_run'] = bool(job['data'].get('dry_run', True)) or not (can_apply_loadout if job['data'].get('preset_restore') else can_apply)(user)
        context['plan'] = job
        if 'baseline' in job['data']:
            ids = plans.baseline_units(job['data'])
            names = database.get_game_unit_names(sorted(ids)) if ids else {}
            context['baseline_targets'] = sorted(
                ((base, names.get(base, base)) for base in ids), key=lambda row: (row[1].casefold(), row[0]))
            context['baseline_groups'] = {base: [] for base in ids}
            for mod in job['data']['baseline']:
                context['baseline_groups'].setdefault(mod.get('equipped_to') or '', []).append(mod)
            context['baseline_saved_counts'] = {base: len(group) for base, group in context['baseline_groups'].items()}
        context['error'] = job['message'] if job['state'] in ('error', 'partial') else None
        if (job['data'].get('game_source') and job['state'] == 'error'
                and job['message'] == 'Игровая сессия завершилась. Нужно повторное подключение.'):
            context.update(connection_action='login', error=None)
        code = job['ally_code']
    if code:
        try:
            game_account.select_account(str(user['discord_id']), user['guild_id'], ally_code=code)
        except game_account.AccountError:
            raise HTTPException(403, 'Выберите свой зарегистрированный аккаунт.') from None
        context['self_ally_code'] = code
        if not plan:
            context['locked'] = plans.get_locks(user['discord_id'], user['guild_id'], code)
        context['selected_code'] = code
        if not plan or _is_loadout(context['plan']):
            context['saved_items'] = plans.saved(user['discord_id'], user['guild_id'], code)
        context['game_connected'] = bool(game_account_readonly._connection(str(user['discord_id']), code))
        names = dict(context['accounts'])
        context['player_name'] = context['self_player_name'] = names.get(code, code)
        context['characters'] = _characters(code)
        context['link_path'] = game_account.link_path(str(user['discord_id']), user['guild_id'], code)
        context['login_warning'] = game_account.LOGIN_WARNING
        allowed = {base for base, _ in context['characters']}
        context['locked'] = [base for base in context['locked'] if base in allowed]
    return context


@router.post('/save')
async def optimizer_save(request: Request, plan: str = Form(), csrf: str = Form(),
                         name: str = Form(default='', max_length=80),
                         user: dict = Depends(require_optimizer_access)):
    _csrf(request, csrf)
    owner, guild_id = user['discord_id'], user['guild_id']
    try:
        job = plans.get(plan, owner, guild_id)
        if _is_loadout(job):
            raise ValueError('Выберите готовый результат оптимизатора.')
        game_account.select_account(str(owner), guild_id, ally_code=job['ally_code'])
        plans.save_result(plan, owner, guild_id, name.strip())
    except (ValueError, game_account.AccountError) as error:
        raise HTTPException(409, str(error)) from None
    return RedirectResponse('/mod-loadouts?plan=saved-' + plan, status_code=303)


@router.post('/baseline', include_in_schema=False)
@loadouts_router.post('/baseline')
async def optimizer_baseline(request: Request, ally_code: str = Form(), csrf: str = Form(),
                             name: str = Form(default='', max_length=80), characters: list[str] = Form(default=[]),
                             scope: str = Form(default=''), action: str = Form(default='save'),
                             reconnect: bool = Form(default=False),
                             user: dict = Depends(require_loadouts_access)):
    _csrf(request, csrf)
    owner, guild_id = user['discord_id'], user['guild_id']
    code = ally_code
    scope = scope or ('selected' if characters else 'all')
    selected = set(characters)
    try:
        code = game_account.select_account(str(owner), guild_id, ally_code=ally_code)
        if scope not in ('all', 'selected') or action not in ('save', 'refresh'):
            raise ValueError('Выберите действие и состав раскладки.')
        if action == 'save' and scope == 'selected' and not selected:
            raise ValueError('Выберите хотя бы одного персонажа.')
        if reconnect:
            await game_account.reconnect(str(owner), guild_id, user['bot_profile'], user['tier'],
                                         code, optimizer_read=True)
        snap = await game_mod_actions.snapshot(user, code, read_only=True)
        equipped = Counter(mod['equipped_to'] for mod in snap['mods'] if mod.get('equipped_to'))
        live_chars = sorted(_characters(code, {*snap.get('unit_ids', {}), *equipped}), key=lambda row: row[1].casefold())
        units = {base for base, _ in live_chars}
        if action == 'refresh':
            context = _baseline_context(user, code, name, selected & units, scope, action)
            context.update(characters=live_chars, baseline_counts=equipped,
                           baseline_live=True,
                           baseline_notice='Список персонажей обновлён из игры.')
            return _render(request, context)
        if scope == 'selected' and selected - units:
            raise ValueError('Некоторые персонажи недоступны. Обновите страницу.')
        targets = selected if scope == 'selected' else units
        mods = [{**mod, 'equipped_to': mod.get('equipped_to') or '', 'summary': _mod_summary(mod)}
                for mod in snap['mods'] if scope == 'all' or mod.get('equipped_to') in targets]
        token = plans.save_baseline(owner, guild_id, code, mods, name=name.strip(), units=sorted(targets), scope=scope)
    except game_account.ConnectionRequired as error:
        context = _baseline_context(user, code, name, selected, scope, action)
        context.update(game_connected=isinstance(error, game_account.SessionExpired) and not reconnect,
                       baseline_retry=True)
        return _render(request, context)
    except (ValueError, game_account.AccountError) as error:
        raise HTTPException(409, str(error)) from None
    return RedirectResponse('/mod-loadouts?plan=' + token, status_code=303)


def _baseline_context(user, code, name, selected, scope, action):
    guild_id = user['guild_id']
    context = _base_context(user, guild_id)
    characters = dict(_characters(code))
    missing = selected - characters.keys()
    if missing:
        characters.update({base: base for base in missing})
        characters.update(database.get_game_unit_names(sorted(missing)))
    context.update(
        self_ally_code=code, self_player_name=dict(context['accounts']).get(code, code),
        characters=sorted(characters.items(), key=lambda row: row[1].casefold()),
        saved_items=plans.saved(user['discord_id'], guild_id, code),
        locked=plans.get_locks(user['discord_id'], guild_id, code), game_connected=True,
        baseline_name=name, baseline_characters=selected, baseline_scope=scope, baseline_action=action,
        loadouts_page=True,
        login_warning=game_account.LOGIN_WARNING,
    )
    return context


@router.post('/restore', include_in_schema=False)
@loadouts_router.post('/restore')
async def optimizer_restore(request: Request, plan: str = Form(), csrf: str = Form(),
                            scope: str = Form(default='all'), characters: list[str] = Form(default=[]),
                            reconnect: bool = Form(default=False),
                            user: dict = Depends(require_loadouts_access)):
    _csrf(request, csrf)
    owner, guild_id = user['discord_id'], user['guild_id']
    try:
        preset = plans.get(plan, owner, guild_id)
        code = game_account.select_account(str(owner), guild_id, ally_code=preset['ally_code'])
        data = preset['data']
        if preset['state'] != 'saved' or ('baseline' not in data and not data.get('game_source')):
            raise ValueError('Выберите сохранённую раскладку с игрового аккаунта.')
        if scope not in ('all', 'selected') or (scope == 'selected' and not characters):
            raise ValueError('Выберите хотя бы одного персонажа для переодевания.')
        if scope == 'selected' and 'baseline' not in data:
            raise ValueError('Частичное переодевание доступно для раскладки из игры.')
        mutable = can_apply_loadout(user)
        if mutable:
            try:
                game_account.require_mod_actions(str(owner), guild_id, user['bot_profile'], user['tier'])
            except game_account.AccountError:
                mutable = False
        if reconnect:
            await game_account.reconnect(str(owner), guild_id, user['bot_profile'], user['tier'],
                                         code, optimizer_read=True)
        snap = await game_mod_actions.snapshot(user, code, read_only=True)
        results = plans.restore_results(data, snap, selected=set(characters) if scope == 'selected' else None)
        locked = plans.get_locks(owner, guild_id, code)
        moves = game_mod_actions.operations(snap['mods'], results, set(locked))
        targets = {result['base_id'] for result in results}
        names = database.get_game_unit_names(sorted({op['unit'] for op in moves}))
        by_id = {mod['id']: mod for mod in snap['mods']}
        preview = {'results': results, 'preset_restore': True, 'preset_name': data.get('preset_name'),
                   'saved_preset_id': plan, 'game_source': True, 'dry_run': not mutable, 'fingerprint': snap['fingerprint'],
                   'connection': snap['connection'], 'locked': locked, 'operation_count': len(moves),
                   'restore_moves': [{'name': names.get(op['unit'], op['unit']), 'remove': bool(op['remove']),
                                      'donor': op['unit'] not in targets,
                                      'mods': [_mod_summary(by_id[mid]) for mid in op['remove'] + op['equip']]}
                                     for op in moves]}
        token = plans.create(owner, guild_id, code, preview)
        plans.finish(token, 'ready', preview)
    except game_account.ConnectionRequired as error:
        context = _optimizer_context(user, plan)
        context.update(loadouts_page=True, restore_characters=set(characters), restore_scope=scope)
        if isinstance(error, game_account.SessionExpired) and not reconnect:
            context['restore_retry'] = True
        else:
            context['game_connected'] = False
        return _render(request, context)
    except (ValueError, game_account.AccountError) as error:
        raise HTTPException(409, str(error)) from None
    return RedirectResponse('/mod-loadouts?plan=' + token, status_code=303)


@router.post("", response_class=HTMLResponse)
async def optimizer_run(
    request: Request,
    plates: list[str] = Form(default=[]),
    force_refresh: bool = Form(default=False),
    mods_file: UploadFile | None = File(default=None),
    source: str = Form(default="csv"),
    csrf: str = Form(default=""),
    ally_code: str = Form(default=''),
    locked: list[str] = Form(default=[]),
    relic_mode: str = Form(default='current'),
    dry_run: bool = Form(default=False),
    deep_search: bool = Form(default=False),
    faster: list[str] = Form(default=[]),
    slower: list[str] = Form(default=[]),
    engine: str = Form(default='server'),
    user: dict = Depends(require_optimizer_access),
):
    guild_id = user["guild_id"]
    context = _base_context(user, guild_id)
    context["selected_plates"] = plates
    context['relic_mode'] = relic_mode
    context['deep_search'] = deep_search
    dry_run = bool(dry_run) or not can_apply(user)
    context['dry_run'] = dry_run
    _csrf(request, csrf)
    if engine not in ('server', 'browser'):
        raise HTTPException(400, 'Неизвестный вычислитель.')
    engine = 'browser' if user.get('bot_profile') == 'prod' else engine

    ally_code = ally_code or context["self_ally_code"]
    if not ally_code:
        context["error"] = "Ваш аккаунт не привязан через /регистрация — без этого не получится узнать ваш ростер/релики."
        return _render(request, context)

    try:
        if relic_mode not in ('current', 'plate'):
            raise ValueError('Выберите расчёт по текущей реликвии или по реликвии плейта.')
        code = game_account.select_account(str(user['discord_id']), guild_id, ally_code=ally_code)
        context['self_ally_code'] = code
        context['self_player_name'] = dict(context['accounts']).get(code, code)
        context['characters'] = _characters(code)
        context['game_connected'] = bool(game_account_readonly._connection(str(user['discord_id']), code))
        context['link_path'] = game_account.link_path(str(user['discord_id']), guild_id, code)
        context['login_warning'] = game_account.LOGIN_WARNING
        if len(faster) != len(slower) or len(faster) > 10:
            raise ValueError('Можно задать до 10 правил порядка ходов.')
        turn_order = list(dict.fromkeys((a, b) for a, b in zip(faster, slower) if a or b))
        context['turn_order'] = turn_order
        allowed = {base for base, _ in context['characters']}
        if any(a == b or a not in allowed or b not in allowed for a, b in turn_order):
            raise ValueError('В каждом правиле порядка ходов выберите двух разных персонажей.')
        if not plates:
            raise ValueError("Выберите хотя бы один плейт.")
        if not set(plates) <= set(context['plates']):
            raise ValueError('Один из плейтов больше недоступен.')
        if len(plates) > 20:
            raise ValueError('За один расчёт можно выбрать до 20 плейтов.')
        plates = list(dict.fromkeys(plates))
        thresholds = {}
        for field, raw in (await request.form()).multi_items():
            if field.startswith('threshold_') and raw:
                key = field.removeprefix('threshold_')
                try:
                    value = float(raw)
                except ValueError:
                    raise ValueError('Введите числовое значение характеристики.') from None
                if not key.isascii() or not key.isdigit() or not math.isfinite(value) or abs(value) > 1e9:
                    raise ValueError('Введите допустимое числовое значение характеристики.')
                if key in thresholds and thresholds[key] != value:
                    raise ValueError('Для одного требования указаны разные значения.')
                thresholds[key] = value
        locked = sorted(set(locked))
        if not set(locked) <= {base for base, _ in context['characters']}:
            raise ValueError('Список заблокированных персонажей устарел. Обновите страницу.')
        context['locked'] = locked
        raw_csv = None
        reconnect = source == 'reconnect'
        if source in ('game', 'reconnect'):
            game_account.require_account_read(str(user['discord_id']), guild_id, user['bot_profile'], user['tier'])
            if not context['game_connected']:
                return _render(request, context)
            source = 'game'
        elif source == 'csv' and mods_file and mods_file.filename:
            raw_csv = await mods_file.read(8 * 1024 * 1024 + 1)
            if len(raw_csv) > 8 * 1024 * 1024:
                raise ValueError('CSV должен быть не больше 8 МБ.')
            mod_optimizer_v2.validate_inventory(mod_optimizer.parse_mods_csv(raw_csv))
        else:
            raise ValueError('Выберите чтение из игры или загрузите CSV.')
        token = plans.create(user['discord_id'], guild_id, code, {'selected_plates': plates, 'locked': locked,
                             'relic_mode': relic_mode, 'dry_run': dry_run, 'deep_search': deep_search, 'turn_order': turn_order})
        plans.set_locks(user['discord_id'], guild_id, code, locked)
    except ValueError as error:
        context['error'] = str(error)
        return _render(request, context)
    _spawn(_calculate(token, {**user, 'optimizer_engine': engine}, plates, code, source, raw_csv, locked, force_refresh, relic_mode, dry_run,
                      reconnect=reconnect, **({'deep_search': True} if deep_search else {}), **({'thresholds': thresholds} if thresholds else {}),
                      **({'turn_order': turn_order} if turn_order else {})))
    return RedirectResponse('/mod-optimizer?plan=' + token, status_code=303)


@router.post('/apply')
@loadouts_router.post('/apply')
async def optimizer_apply(request: Request, plan: str = Form(), csrf: str = Form(),
                          confirm: bool = Form(default=False), user: dict = Depends(require_loadouts_access)):
    _csrf(request, csrf)
    loadout_route = request.url.path == '/mod-loadouts/apply'
    if not (can_apply_loadout if loadout_route else can_apply)(user):
        raise HTTPException(403, 'Игрокам доступен только Dry run. Применение в игре запрещено.')
    if not confirm:
        raise HTTPException(400, 'Подтвердите применение показанных перестановок.')
    try:
        job = plans.get(plan, user['discord_id'], user['guild_id'])
        if user['bot_profile'] == 'prod' and not job['data'].get('preset_restore'):
            raise HTTPException(404)
        game_account.require_mod_actions(str(user['discord_id']), user['guild_id'], user['bot_profile'], user['tier'])
        game_account.select_account(str(user['discord_id']), user['guild_id'], ally_code=job['ally_code'])
        data = plans.claim(plan, user['discord_id'], user['guild_id'])
    except ValueError as error:
        raise HTTPException(409, str(error)) from None
    _spawn(_apply(plan, dict(user), job['ally_code'], data))
    path = '/mod-loadouts' if data.get('preset_restore') else '/mod-optimizer'
    return RedirectResponse(path + '?plan=' + job['id'], status_code=303)
