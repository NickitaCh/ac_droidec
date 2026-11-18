"""Fixed-purpose mod snapshot and UpdateMods executor; never a generic RPC proxy.

No automatic login or mutation retries. The caller owns a one-shot durable claim.
Every operation is checked against a fresh inventory and verified afterwards.
"""
import base64
import hashlib

from services import game_account as ga, game_account_readonly as accounts
from services import game_mod_inventory, mod_optimizer, mod_optimizer_plans as plans


async def snapshot(user, code, *, read_only=False):
    scope = (str(user['discord_id']), user['guild_id'], user['bot_profile'], user['tier'], code)
    connection, credentials, is_current = accounts._load(*scope, optimizer_read=read_only)
    expected = connection[1]
    try:
        raw = base64.b64decode(credentials['read_request'], validate=True)
    except (KeyError, ValueError, TypeError):
        raise ga.ConnectionRequired('Подключите аккаунт через EA заново.') from None
    if not is_current():
        raise ga.AccountError('Подключение изменилось.')
    body = ga.read_request(raw, expected)
    payload = ga.fields(await ga._read_payload(body, 'PlayerRpc', 'GetInitialData'))
    if ga._one(payload, 1, 2) != expected.encode() or not is_current():
        raise ga.AccountError('Игра вернула другой аккаунт или подключение изменилось.')
    inventory = ga.fields(ga._one(payload, 5, 2))
    bank = game_mod_inventory.read_inventory(inventory)
    if bank['unreadable'] or any(not m.get('csv_row') for m in bank['modules']):
        raise ga.AccountError('Не удалось прочитать все модули. Неполный инвентарь нельзя использовать для перестановки.')
    mods = []
    for m in bank['modules']:
        def stat(s):
            sid = s['stat_id']
            return {'unit_stat_id': sid, 'unscaled': s.get('unscaled', round(s['value'] * (1e6 if s['is_percent'] else 1e8))),
                    'value': s['value'], 'is_percent': s['is_percent'],
                    'name': game_mod_inventory.CSV_STATS.get(sid, s['name'])}
        row = {'id': m['id'], 'level': m['level'], 'rarity': m['rarity'], 'tier': m['tier'],
               'set_id': m['set_id'], 'set_name': mod_optimizer.MOD_SET_IDS[str(m['set_id'])],
               'slot_id': list(game_mod_inventory.CSV_SLOTS).index(m['slot_key']) + 1,
               'slot_name': game_mod_inventory.CSV_SLOTS[m['slot_key']], 'locked': m['locked'],
               'equipped_to': m['csv_row'][5] or None, 'primary': stat(m['primary']),
               'secondaries': [stat(s) for s in m['secondaries']]}
        for s, rolls in zip(row['secondaries'], m['secondary_rolls']):
            s['rolls'] = rolls
        mods.append(row)
    from services.mod_optimizer_v2 import validate_inventory
    validate_inventory(mods)
    units, progress, roster = {}, {}, {}
    for tag, wire, raw_unit in inventory:
        if (tag, wire) != (1, 2):
            continue
        fields = ga.fields(raw_unit)
        base = ga._one(fields, 2, 2).decode().split(':')[0]
        units[base] = ga._one(fields, 1, 2).decode()
        values = {n: v for n, w, v in fields if w == 0}
        skills, equipment, purchased = [], [], []
        relic = {'currentTier': 0}
        for n, w, value in fields:
            if w != 2:
                continue
            if n == 16:
                skill = ga.fields(value)
                skills.append({'id': ga._one(skill, 1, 2).decode(),
                               'tier': next((v for k, wire, v in skill if (k, wire) == (2, 0)), 0)})
            elif n == 18:
                piece = ga.fields(value)
                equipment.append({'equipmentId': ga._one(piece, 1, 2).decode(),
                                  'slot': next((v for k, wire, v in piece if (k, wire) == (2, 0)), 0)})
            elif n == 22:
                relic['currentTier'] = next((v for k, wire, v in ga.fields(value) if (k, wire) == (1, 0)), 0)
            elif n == 23:
                purchased.append(value.decode())
        roster[base] = {'id': units[base], 'definitionId': ga._one(fields, 2, 2).decode(),
                        'currentRarity': values.get(8, 1), 'currentLevel': values.get(9, 1),
                        'currentTier': values.get(19, 1), 'skill': skills, 'equipment': equipment,
                        'relic': relic, 'purchasedAbilityId': purchased}
        # Stable progression fields, excluding calculated stats and equipped mods.
        progress[base] = hashlib.sha256(ga.pack(row for row in fields if row[0] in (1, 2, 8, 9, 16, 18, 19, 22, 23))).hexdigest()
    return {'mods': mods, 'unit_ids': units, 'progress': progress, 'roster': roster,
            'fingerprint': plans.fingerprint(mods, progress),
            'connection': hashlib.sha256(connection[2]).hexdigest(),
            '_raw': raw, '_expected': expected, '_is_current': is_current}


def operations(mods, results, locked):
    """Remove moved/displaced mods first; then equip. Handles cycles explicitly."""
    by_id = {m['id']: m for m in mods}
    desired, targets = {}, set()
    for result in results:
        if not result.get('owned'):
            continue
        base = result['base_id']
        targets.add(base)
        slots = set()
        for mod in result.get('used_mods', []):
            mid = mod['id']
            if mid not in by_id or mid in desired or mod['slot_id'] in slots:
                raise ga.AccountError('План содержит неизвестные или повторяющиеся модули.')
            slots.add(mod['slot_id'])
            desired[mid] = base
    remove, equip = {}, {}
    for mid, mod in by_id.items():
        old = mod.get('equipped_to') or None
        new = desired.get(mid, None if old in targets else old)
        if old == new:
            continue
        if old in locked or new in locked or mod.get('locked'):
            raise ga.AccountError('План затрагивает заблокированного персонажа или модуль.')
        if old:
            remove.setdefault(old, []).append(mid)
        if new:
            equip.setdefault(new, []).append(mid)
    return ([{'unit': b, 'remove': sorted(ids), 'equip': []} for b, ids in sorted(remove.items())] +
            [{'unit': b, 'remove': [], 'equip': sorted(ids)} for b, ids in sorted(equip.items())])


def request(snapshot, op):
    """UpdateModsRequest: UnitId=1, ModEquippedId=2, ModUnequippedId=3.

    Verified in Android 0.41.2115745 ProtoOutput.dll ProtoMember attributes:
    UnitId field 0x04000e2e -> 1; properties 0x17000178 -> 2 and
    0x17000179 -> 3. AutoAssigned is tag 5, omitted (false).
    No upgrade, sale or calibration operation.
    """
    unit_id = snapshot['unit_ids'].get(op['unit'])
    if not unit_id or not snapshot['_is_current']():
        raise ga.AccountError('Персонаж недоступен или подключение изменилось.')
    args = [(1, 2, unit_id.encode())]
    args.extend((2, 2, mid.encode()) for mid in op['equip'])
    args.extend((3, 2, mid.encode()) for mid in op['remove'])
    rows = ga.fields(ga.read_request(snapshot['_raw'], snapshot['_expected']))
    return ga.pack([r for r in rows if r[0] not in (4, 5, 6)] + [
        (4, 2, b'StatModRpc'), (5, 2, b'UpdateMods'), (6, 2, ga.pack(args))])


async def apply(user, code, data, save):
    fresh = await snapshot(user, code)
    if fresh['connection'] != data['connection'] or fresh['fingerprint'] != data['fingerprint']:
        raise ga.AccountError('Инвентарь, прокачка или подключение изменились. Выполните новый Dry run.')
    ops = operations(fresh['mods'], data['results'], set(data.get('locked', [])))
    expected = [dict(m) for m in fresh['mods']]
    for index, op in enumerate(ops):
        # Re-read before every write, including after a possibly long prior step.
        current = await snapshot(user, code) if index else fresh
        if (current['connection'] != data['connection'] or
                current['fingerprint'] != plans.fingerprint(expected, fresh['progress'])):
            raise ga.AccountError('Расстановка изменилась во время применения. Дальнейшие действия остановлены.')
        data['in_flight'] = {'step': index + 1, 'total': len(ops), **op}
        save(data)
        try:
            await ga._read_payload(request(current, op), 'StatModRpc', 'UpdateMods', allow_empty=True)
        except ga.SessionExpired:
            raise
        except ga.AccountError as error:
            raise ga.AccountError(str(error).replace('отклонила чтение', 'отклонила перестановку')) from None
        for m in expected:
            if m['id'] in op['remove']:
                m['equipped_to'] = None
            elif m['id'] in op['equip']:
                m['equipped_to'] = op['unit']
        actual = await snapshot(user, code)
        if (actual['connection'] != data['connection'] or
                actual['fingerprint'] != plans.fingerprint(expected, fresh['progress'])):
            raise ga.AccountError('Итог операции отличается от плана. Остановлено; проверьте текущую расстановку в игре.')
        data['completed_steps'] = index + 1
        data['in_flight'] = None
        save(data)
    data['completed_steps'] = len(ops)
    data['verified'] = True
    return data
