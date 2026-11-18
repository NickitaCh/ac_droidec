"""Owner-scoped background mod allocation, Dry run and confirmed application.

The existing super-admin gate and TEST account-pilot restrictions remain intact.
Game credentials never enter jobs, HTML, URLs or browser storage.
"""

from pathlib import Path
import secrets
import asyncio
import logging
import math

from fastapi import APIRouter, Depends, HTTPException, Request, UploadFile, File, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

import database
from services import game_account, game_account_readonly
import stat_engine
import services.stat_forecast as stat_forecast
import services.mod_optimizer as mod_optimizer
from services import mod_optimizer_v2, mod_optimizer_plans as plans, game_mod_actions
from web.deps import require_member_access

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent.parent / "templates"))
_jobs = set()
_log = logging.getLogger(__name__)


def require_optimizer_access(user: dict = Depends(require_member_access)):
    try:
        game_account.require_account_read(str(user['discord_id']), user['guild_id'], user['bot_profile'], user['tier'])
    except game_account.AccountError:
        raise HTTPException(404, 'Страница недоступна.') from None
    return user


def can_apply(user):
    return user.get('guild_id') == 1 and user.get('tier') == 'officer' and bool(user.get('is_super_admin'))


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
    context['can_apply'] = can_apply(context['user'])
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


def _characters(code):
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


async def _calculate(token, user, plates, code, source, raw_csv, locked, force_refresh, relic_mode='current', dry_run=True, *, reconnect=False, thresholds=None):
    dry_run = bool(dry_run) or not can_apply(user)
    data = {'selected_plates': plates, 'locked': locked, 'game_source': source == 'game', 'ally_code': code,
            'relic_mode': relic_mode, 'dry_run': dry_run, 'thresholds': thresholds or {}}
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
        results, passed, optimal, donors = await mod_optimizer_v2.optimize(
            comlink, calc, plates, code, mods, user['guild_id'], locked, force_refresh,
            units_override=snap['roster'] if source == 'game' else None, relic_mode=relic_mode,
            **({'thresholds': thresholds} if thresholds else {}))
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
        data.update(results=results, plates_passed=passed, plates_total=len({p for r in results for p in r['plates']}),
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


async def _apply(token, user, code, data):
    try:
        if not can_apply(user) or data.get('dry_run', True):
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
    context = _base_context(user, user["guild_id"])
    code = ally_code or context['self_ally_code']
    if plan:
        try:
            job = plans.get(plan, user['discord_id'], user['guild_id'])
        except ValueError:
            raise HTTPException(404, 'План не найден.') from None
        context.update(job['data'])
        context['can_apply'] = can_apply(user)
        context['dry_run'] = bool(job['data'].get('dry_run', True)) or not can_apply(user)
        context['plan'] = job
        if 'baseline' in job['data']:
            ids = sorted({m['equipped_to'] for m in job['data']['baseline'] if m['equipped_to']})
            context['baseline_names'] = database.get_game_unit_names(ids) if ids else {}
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
        context['saved_items'] = plans.saved(user['discord_id'], user['guild_id'], code)
        context['game_connected'] = bool(game_account_readonly._connection(str(user['discord_id']), code))
        names = dict(context['accounts'])
        context['player_name'] = context['self_player_name'] = names.get(code, code)
        context['characters'] = _characters(code)
        context['link_path'] = game_account.link_path(str(user['discord_id']), user['guild_id'], code)
        context['login_warning'] = game_account.LOGIN_WARNING
        allowed = {base for base, _ in context['characters']}
        context['locked'] = [base for base in context['locked'] if base in allowed]
    return _render(request, context)


@router.post('/save')
async def optimizer_save(request: Request, plan: str = Form(), csrf: str = Form(),
                         name: str = Form(default='', max_length=80),
                         user: dict = Depends(require_optimizer_access)):
    _csrf(request, csrf)
    owner, guild_id = user['discord_id'], user['guild_id']
    try:
        job = plans.get(plan, owner, guild_id)
        game_account.select_account(str(owner), guild_id, ally_code=job['ally_code'])
        plans.save_result(plan, owner, guild_id, name.strip())
    except (ValueError, game_account.AccountError) as error:
        raise HTTPException(409, str(error)) from None
    return RedirectResponse('/mod-optimizer?plan=saved-' + plan, status_code=303)


@router.post('/baseline')
async def optimizer_baseline(request: Request, ally_code: str = Form(), csrf: str = Form(),
                             name: str = Form(default='', max_length=80), characters: list[str] = Form(default=[]),
                             reconnect: bool = Form(default=False),
                             user: dict = Depends(require_optimizer_access)):
    _csrf(request, csrf)
    owner, guild_id = user['discord_id'], user['guild_id']
    try:
        code = game_account.select_account(str(owner), guild_id, ally_code=ally_code)
        if reconnect:
            await game_account.reconnect(str(owner), guild_id, user['bot_profile'], user['tier'],
                                         code, optimizer_read=True)
        snap = await game_mod_actions.snapshot(user, code, read_only=True)
        selected = set(characters)
        units = set(snap.get('unit_ids', {}))
        if selected - units:
            raise ValueError('Некоторые персонажи недоступны. Обновите страницу.')
        mods = [{**mod, 'equipped_to': mod.get('equipped_to') or '', 'summary': _mod_summary(mod)}
                for mod in snap['mods'] if not selected or mod.get('equipped_to') in selected]
        token = plans.save_baseline(owner, guild_id, code, mods, name=name.strip(), units=sorted(selected or units))
    except game_account.ConnectionRequired as error:
        context = _base_context(user, guild_id)
        context.update(
            self_ally_code=code, self_player_name=dict(context['accounts']).get(code, code),
            characters=_characters(code), saved_items=plans.saved(owner, guild_id, code),
            game_connected=isinstance(error, game_account.SessionExpired) and not reconnect,
            baseline_retry=True, baseline_name=name, baseline_characters=set(characters),
            login_warning=game_account.LOGIN_WARNING,
        )
        return _render(request, context)
    except (ValueError, game_account.AccountError) as error:
        raise HTTPException(409, str(error)) from None
    return RedirectResponse('/mod-optimizer?plan=' + token, status_code=303)


@router.post('/restore')
async def optimizer_restore(request: Request, plan: str = Form(), csrf: str = Form(),
                            user: dict = Depends(require_optimizer_access)):
    _csrf(request, csrf)
    if not can_apply(user):
        raise HTTPException(403, 'Для этого аккаунта доступно сохранение и просмотр пресетов.')
    owner, guild_id = user['discord_id'], user['guild_id']
    try:
        preset = plans.get(plan, owner, guild_id)
        code = game_account.select_account(str(owner), guild_id, ally_code=preset['ally_code'])
        data = preset['data']
        if preset['state'] != 'saved' or ('baseline' not in data and not data.get('game_source')):
            raise ValueError('Выберите сохранённую раскладку с игрового аккаунта.')
        game_account.require_pilot(str(owner), guild_id, user['bot_profile'], user['tier'])
        snap = await game_mod_actions.snapshot(user, code, read_only=True)
        results = plans.restore_results(data, snap)
        locked = plans.get_locks(owner, guild_id, code)
        moves = game_mod_actions.operations(snap['mods'], results, set(locked))
        names = database.get_game_unit_names(sorted({op['unit'] for op in moves}))
        by_id = {mod['id']: mod for mod in snap['mods']}
        preview = {'results': results, 'preset_restore': True, 'preset_name': data.get('preset_name'),
                   'game_source': True, 'dry_run': False, 'fingerprint': snap['fingerprint'],
                   'connection': snap['connection'], 'locked': locked, 'operation_count': len(moves),
                   'restore_moves': [{'name': names.get(op['unit'], op['unit']), 'remove': bool(op['remove']),
                                      'mods': [_mod_summary(by_id[mid]) for mid in op['remove'] + op['equip']]}
                                     for op in moves]}
        token = plans.create(owner, guild_id, code, preview)
        plans.finish(token, 'ready', preview)
    except (ValueError, game_account.AccountError) as error:
        raise HTTPException(409, str(error)) from None
    return RedirectResponse('/mod-optimizer?plan=' + token, status_code=303)


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
    user: dict = Depends(require_optimizer_access),
):
    guild_id = user["guild_id"]
    context = _base_context(user, guild_id)
    context["selected_plates"] = plates
    context['relic_mode'] = relic_mode
    dry_run = bool(dry_run) or not can_apply(user)
    context['dry_run'] = dry_run
    _csrf(request, csrf)

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
                             'relic_mode': relic_mode, 'dry_run': dry_run})
        plans.set_locks(user['discord_id'], guild_id, code, locked)
    except ValueError as error:
        context['error'] = str(error)
        return _render(request, context)
    _spawn(_calculate(token, dict(user), plates, code, source, raw_csv, locked, force_refresh, relic_mode, dry_run,
                      reconnect=reconnect, **({'thresholds': thresholds} if thresholds else {})))
    return RedirectResponse('/mod-optimizer?plan=' + token, status_code=303)


@router.post('/apply')
async def optimizer_apply(request: Request, plan: str = Form(), csrf: str = Form(),
                          confirm: bool = Form(default=False), user: dict = Depends(require_optimizer_access)):
    _csrf(request, csrf)
    if not can_apply(user):
        raise HTTPException(403, 'Игрокам доступен только Dry run. Применение в игре запрещено.')
    if not confirm:
        raise HTTPException(400, 'Подтвердите применение показанных перестановок.')
    try:
        job = plans.get(plan, user['discord_id'], user['guild_id'])
        game_account.require_pilot(str(user['discord_id']), user['guild_id'], user['bot_profile'], user['tier'])
        game_account.select_account(str(user['discord_id']), user['guild_id'], ally_code=job['ally_code'])
        data = plans.claim(plan, user['discord_id'], user['guild_id'])
    except ValueError as error:
        raise HTTPException(409, str(error)) from None
    _spawn(_apply(plan, dict(user), job['ally_code'], data))
    return RedirectResponse('/mod-optimizer?plan=' + plan, status_code=303)
