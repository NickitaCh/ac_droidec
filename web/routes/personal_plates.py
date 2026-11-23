"""Personal plate editor and explicit HotBot preview for guild members."""
from pathlib import Path
import hashlib
import json
import math
import secrets
from urllib.parse import urlencode
import aiohttp

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates

import database
from services import feature_flags, hotbot_import as hb, personal_plates as plates
from web.routes.stat_plates import STAT_OPTIONS, OPERATOR_OPTIONS, PRIORITY_OPTIONS, _stat_ru, _fmt_value

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent.parent / 'templates'))
_member_access = feature_flags.require_feature_member('stat_requirements')


async def access(user=Depends(_member_access)):
    if user.get('bot_profile') != 'test':
        raise HTTPException(404)
    return user


def render(request, user, **context):
    csrf = request.session.setdefault('personal_plates_csrf', secrets.token_urlsafe(24))
    return templates.TemplateResponse(request, 'personal_plates.html', {
        'user': user, 'csrf': csrf, 'stat_options': STAT_OPTIONS,
        'operator_options': OPERATOR_OPTIONS, 'priority_options': PRIORITY_OPTIONS,
        'add_draft': getattr(request.state, 'personal_plate_draft', {}),
        'error': getattr(request.state, 'personal_plate_error', request.query_params.get('error')), **context}, headers={'Cache-Control': 'no-store'})


def check(request, form):
    expected = request.session.get('personal_plates_csrf')
    if not expected or not secrets.compare_digest(str(form.get('csrf', '')), expected):
        raise HTTPException(403, 'Обновите страницу и повторите действие.')


def redirect(path, error=None):
    return RedirectResponse(path + ('?' + urlencode({'error': str(error)}) if error else ''), status_code=303)


@router.get('')
async def index(request: Request, user=Depends(access)):
    return render(request, user, plates=plates.list_plates(user))


@router.post('/create')
async def create(request: Request, user=Depends(access)):
    form = await request.form()
    check(request, form)
    try:
        pid = plates.create(user, str(form.get('name', '')))
    except ValueError as error:
        return redirect('/personal-plates', error)
    return redirect('/personal-plates/' + pid)


@router.get('/import')
async def import_page(request: Request, user=Depends(access)):
    return render(request, user, importing=True)


@router.post('/import')
async def import_submit(request: Request, user=Depends(access)):
    form = await request.form(max_fields=plates.MAX_REQUIREMENTS + 20)
    check(request, form)
    link, name = str(form.get('link', '')), str(form.get('name', ''))
    end_link = str(form.get('end_link', '')).strip()
    try:
        messages = await hb.fetch_messages(link, owner=str(user['discord_id']), profile=user.get('bot_profile'), end_link=end_link)
        title, items, skipped = plates.parse_source(messages, user)
        if not items:
            raise ValueError('В сообщении нет поддерживаемых требований.')
        digest = hashlib.sha256(json.dumps(items, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        if form.get('action') == 'save':
            if not secrets.compare_digest(str(form.get('digest', '')), digest):
                raise ValueError('Сообщение изменилось. Повторите предпросмотр.')
            selected = set(form.getlist('selected'))
            chosen = [item for i, item in enumerate(items) if str(i) in selected]
            if not chosen:
                raise ValueError('Отметьте хотя бы одно требование.')
            pid = plates.create(user, name, chosen)
            return redirect('/personal-plates/' + pid)
        return render(request, user, importing=True, link=link, end_link=end_link, message_count=len(messages), name=name or title,
                      preview=[item['label'] if item.get('kind') == 'stored' else hb.describe_item(item) for item in items], skipped=skipped, digest=digest,
                      source_note='Текущие требования исходного плейта.' if messages[0].get('_droidec_dm') else '')
    except (ValueError, hb.HotbotImportError) as error:
        return render(request, user, importing=True, link=link, end_link=end_link, name=name, error=str(error))
    except (aiohttp.ClientError, TimeoutError):
        return render(request, user, importing=True, link=link, end_link=end_link, name=name, error='Не удалось прочитать сообщение Discord. Попробуйте ещё раз.')


@router.get('/api/units')
async def units(q: str = '', user=Depends(access)):
    return [{'base_id': base, 'name': name} for base, name in database.search_game_units(q.strip(), limit=20)] if len(q.strip()) >= 2 else []


@router.get('/{plate_id}')
async def detail(request: Request, plate_id: str, ally_code: str = '', check_stats: bool = False, user=Depends(access)):
    try:
        plate = plates.get(plate_id, user)
    except ValueError:
        raise HTTPException(404, 'Плейт недоступен.') from None
    rows = plates.rows(plate)
    names = database.get_game_unit_names(list({row[2] for row in rows}))
    entries = [{'id': row[0], 'character': names.get(row[2], row[2]),
                'text': hb.describe_row(row).replace(' — ' + row[3], ' — ' + _stat_ru(row[3])),
                'stat': _stat_ru(row[3]), 'value': row[5], 'operator': row[4], 'priority': row[6],
                'condition': hb.describe_row(row).split(' — ', 1)[-1].rstrip('*'),
                'norm': row[4] + ' ' + _fmt_value(row[5]),
                'priority_label': dict((value, name) for name, value in PRIORITY_OPTIONS).get(row[6], row[6]),
                'numeric': row[3] in {v for _, v in STAT_OPTIONS} and not row[13]} for row in rows]
    accounts = database.get_user_registrations(str(user['discord_id']), guild_id=user['guild_id'])
    results, error = None, None
    if check_stats:
        from services import game_account, stat_forecast
        from web.routes.stat_forecast import _get_comlink
        try:
            code = game_account.select_account(str(user['discord_id']), user['guild_id'], ally_code=ally_code or None)
            comlink = _get_comlink()
            calc = await stat_forecast.get_stat_calc(comlink)
            results = []
            with plates.scope(user):
                for base in dict.fromkeys(row[2] for row in rows):
                    result = await stat_forecast.evaluate_character_player(comlink, calc, plates.reference(plate), base,
                                                                           code, False, code, guild_id=user['guild_id'])
                    if result:
                        results.append({'name': result[0], 'block': result[1], 'matched': result[2], 'total': result[3]})
        except ValueError as exc:
            error = str(exc)
        except Exception:
            error = 'Не удалось проверить статы. Попробуйте позже.'
    return render(request, user, plate=plate, entries=entries, own=plate['owner'] == str(user['discord_id']),
                  reference=plates.reference(plate), accounts=accounts, results=results, **({'error': error} if error else {}))


@router.post('/{plate_id}/{action}')
async def change(request: Request, plate_id: str, action: str, user=Depends(access)):
    form = await request.form()
    check(request, form)
    path = '/personal-plates/' + plate_id
    try:
        plates.get(plate_id, user, edit=True)
        if action == 'rename':
            plates.rename(user, plate_id, str(form.get('name', '')))
        elif action == 'share':
            plates.share(user, plate_id, form.get('shared') == '1')
        elif action == 'delete':
            plates.delete(user, plate_id)
            return redirect('/personal-plates')
        elif action == 'remove':
            plates.remove_row(user, plate_id, int(form.get('row_id', '0')))
        elif action == 'edit':
            op, priority = str(form.get('operator', '')), str(form.get('priority', ''))
            value = float(str(form.get('value', '')).replace(',', '.'))
            if (op not in {v for _, v in OPERATOR_OPTIONS} or priority not in {v for _, v in PRIORITY_OPTIONS}
                    or not math.isfinite(value) or abs(value) > 1e9):
                raise ValueError('Проверьте условие, значение и приоритет.')
            plates.edit_numeric(user, plate_id, int(form.get('row_id', '0')), op, value, priority)
        elif action == 'add':
            line = str(form.get('line', '')).strip()
            if line:
                items, skipped = plates.parse_hotbot({'sections': [{'lines': [line]}]})
                if skipped or not items:
                    raise ValueError(skipped[0]['reason'] if skipped else 'Не удалось прочитать требование.')
            else:
                base = str(form.get('base_id', ''))
                fields = [form.getlist(key) for key in ('stat', 'operator', 'value', 'priority')]
                request.state.personal_plate_draft = {'base_id': base, 'name': str(form.get('character_name', '')),
                                                     'rows': [list(row) for row in zip(*fields)]}
                if not 1 <= len(fields[0]) <= 30 or len({len(values) for values in fields}) != 1:
                    raise ValueError('Добавьте от 1 до 30 полных условий.')
                if not database.get_game_unit_name(base):
                    raise ValueError('Выберите персонажа из списка.')
                items = []
                for index, (stat, op, raw_value, priority) in enumerate(zip(*fields), 1):
                    if (stat not in {v for _, v in STAT_OPTIONS} or op not in {v for _, v in OPERATOR_OPTIONS}
                            or priority not in {v for _, v in PRIORITY_OPTIONS}):
                        raise ValueError(f'Условие {index}: выберите характеристику, оператор и приоритет.')
                    try:
                        value = float(str(raw_value).replace(',', '.'))
                    except ValueError:
                        raise ValueError(f'Условие {index}: введите числовое значение.') from None
                    if not math.isfinite(value) or abs(value) > 1e9:
                        raise ValueError(f'Условие {index}: введите допустимое числовое значение.')
                    items.append({'kind': 'num', 'char': base, 'stat': stat, 'op': op, 'value': value, 'priority': priority})
            plates.add_many(user, plate_id, items)
        else:
            raise HTTPException(404)
    except ValueError as error:
        if action == 'add' and hasattr(request.state, 'personal_plate_draft'):
            request.state.personal_plate_error = str(error)
            return await detail(request, plate_id, user=user)
        return redirect(path, error)
    return redirect(path)
