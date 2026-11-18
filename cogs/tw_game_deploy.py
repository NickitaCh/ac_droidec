"""Persistent personal TW defense controls, shared executor with the web flow."""
import asyncio

import disnake
from disnake.ext import commands

import database
import guild_resolver
from services import bot_profiles, datacron_catalog, game_account as ga
from services import tw_def_notify, tw_game_deploy as deploy
from tw_def_engine import ZONES_BY_KEY


def _cut(text, limit):
    return text if len(text) <= limit else text[:limit - 1] + '…'


def build_panel(ticket, record, live, catalog, names, datacrons, index=0, page=0, note='', *, rows=None):
    rows = record['data']['assignments'] if rows is None else rows
    checked = deploy.inspect_assignments(live, record['event'], rows) if live else []
    editable = [i for i in range(len(rows)) if not checked or not checked[i]['done']]
    index = index if index in editable else (editable[0] if editable else 0)
    row = rows[index]
    state = record['state']
    locked = state in ('applying', 'unknown', 'complete')
    rev = deploy.revision(record['data'])
    prefix = 'twd:' + ticket + ':'
    view = disnake.ui.View(timeout=None)
    text = []
    for i, assignment in enumerate(rows):
        status = ('✅ уже выставлен' if checked and checked[i]['done'] else
                  '⚠️ ' + checked[i]['error'] if checked and checked[i].get('error') else 'готов')
        text.append(f"{ZONES_BY_KEY[assignment['zone']]['label']} · {assignment['name']} — {status}")
    for result in record['data'].get('results', []):
        text.append(result['name'] + ' — ' + result['status'])
    if note:
        text.append(note)
    embed = disnake.Embed(title='Моя защита ВГ', description=_cut('\n'.join(text), 2600), color=0x58A6FF)
    units = [names.get(b, b) for b in row['units']]
    if row['zone'].endswith('f'):
        units_text = 'Флагман: ' + units[0] + '\nОснова: ' + ' · '.join(units[1:4])
        units_text += '\nПодкрепления по порядку: ' + (' · '.join(units[4:]) or 'нет')
    else:
        units_text = ' · '.join(units)
    embed.add_field(name=_cut(row['name'], 256), value=_cut(units_text, 1024), inline=False)
    dc_by_id = {dc['id']: dc for dc in datacrons if dc.get('id')}
    selected = checked[index].get('actual_datacron', '') if checked and checked[index]['done'] else row.get('datacron', '')
    label = tw_def_notify.describe_datacron(dc_by_id[selected], catalog) if selected in dc_by_id else 'Без датакрона'
    if not row['zone'].endswith('f'):
        embed.add_field(name='Выбранный ДК', value=_cut(label, 1024), inline=False)
    if row.get('recommendation'):
        embed.add_field(name='Рекомендация', value=_cut(row['recommendation'], 1024), inline=False)
    if row.get('datacron_source'):
        source = 'по рекомендации' if row['datacron_source'] == 'recommendation' else 'по истории этого пака'
        embed.add_field(name='Автовыбор', value=f'Подставлен {source}. Можно изменить.', inline=False)
    if not live:
        embed.add_field(name='Обновление сессии', value='Новый вход может завершить сеанс игры на другом устройстве. «Войти и обновить» выполняет только вход и чтение.', inline=False)
    if editable and not locked:
        options = [disnake.SelectOption(label=_cut(ZONES_BY_KEY[rows[i]['zone']]['label'] + ' · ' + rows[i]['name'], 100),
                                       value=str(i), default=i == index) for i in editable]
        view.add_item(disnake.ui.StringSelect(custom_id=prefix+'pack', options=options,
                                             placeholder='Выбрать пак', row=0))
    if live and not locked and index in editable and not row['zone'].endswith('f'):
        reserved = {r.get('datacron') for i, r in enumerate(rows)
                    if i != index and (not checked or not checked[i]['done'])}
        available = [dc for dc in datacrons if dc.get('id') in live['datacrons']
                     and dc['id'] not in live['used_dc'] and dc['id'] not in reserved]
        pages = max(1, (len(available) + 23) // 24)
        if page == 0 and selected:
            page = next((i // 24 for i, dc in enumerate(available) if dc['id'] == selected), 0)
        page %= pages
        options = [disnake.SelectOption(label='Без датакрона', value='-', default=not selected)]
        for n, dc in enumerate(available[page * 24:(page + 1) * 24], page * 24 + 1):
            desc = tw_def_notify.describe_datacron(dc, catalog)
            options.append(disnake.SelectOption(label=_cut(str(n) + '. ' + desc, 100),
                description=_cut(desc.partition(' — ')[2] or desc, 100), value=dc['id'], default=dc['id'] == selected))
        view.add_item(disnake.ui.StringSelect(custom_id=f'{prefix}dc:{index}:{rev}', options=options,
                                             placeholder=f'Изменить ДК · страница {page + 1}/{pages}', row=1))
        if pages > 1:
            view.add_item(disnake.ui.Button(label='Следующие ДК', custom_id=f'{prefix}page:{index}:{page + 1}', row=2))
    if not locked:
        view.add_item(disnake.ui.Button(label='Обновить', custom_id=f'{prefix}refresh:{index}', row=3))
        if not live:
            view.add_item(disnake.ui.Button(label='Войти и обновить', custom_id=f'{prefix}login:{index}', row=3))
        elif state == 'ready':
            pending = sum(not r['done'] and not r.get('error') for r in checked)
            view.add_item(disnake.ui.Button(label=f'Выставить доступные ({pending})', style=disnake.ButtonStyle.success,
                custom_id=f"{prefix}apply:{record['data']['nonce']}", disabled=not pending, row=3))
            embed.set_footer(text='Будут выставлены доступные паки. Ошибки и уже выставленные составы пропускаются.')
        else:
            view.add_item(disnake.ui.Button(label='Проверить перед выставлением', style=disnake.ButtonStyle.primary,
                custom_id=f'{prefix}prepare:{index}:{rev}', disabled=not live, row=3))
    view.add_item(disnake.ui.Button(label='Открыть в вебе', url='https://swgoh-sng.ru/game-account/tw-defense/'+ticket, row=4))
    return embed, view


class TWGameDeployCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    async def _user(self, inter):
        if inter.message.author.id != self.bot.user.id:
            raise ga.AccountError('Откройте личное назначение от тестового Дроидца.')
        access = await asyncio.to_thread(guild_resolver.resolve_access, inter.author)
        return {'discord_id': str(inter.author.id), 'guild_id': access.get('guild_id'),
                'tier': access.get('tier'), 'bot_profile': bot_profiles.worker_profile() or bot_profiles.runtime_profile()}

    async def _render(self, inter, user, ticket, index=0, page=0, note=''):
        record = await asyncio.to_thread(deploy.load, user, ticket)
        live = None
        try:
            live = await deploy.snapshot(user, record['code'])
            deploy.validate(live, record['event'], [])
        except ga.AccountError as exc:
            note = (note + '\n' if note else '') + str(exc)
            live = None
        catalog = getattr(self.bot, 'datacron_cache', None)
        if not catalog:
            try:
                catalog = await datacron_catalog.get_catalog(self.bot.comlink)
            except Exception:
                raise ga.AccountError('Не удалось загрузить описания ДК. Нажмите кнопку в назначении ещё раз.') from None
        bases = [b for row in record['data']['assignments'] for b in row['units']]
        names = await asyncio.to_thread(database.get_game_unit_names, bases)
        dcs = await asyncio.to_thread(database.get_player_datacrons_bulk, [record['code']])
        datacrons = dcs.get(record['code'], [])
        rows = await asyncio.to_thread(deploy.suggested_assignments, record, live, datacrons, catalog)
        embed, view = build_panel(ticket, record, live, catalog, names, datacrons, index, page, note, rows=rows)
        await inter.edit_original_response(content=None, embed=embed, view=view,
                                           allowed_mentions=disnake.AllowedMentions.none())

    async def _dispatch(self, inter):
        parts = (inter.component.custom_id or '').split(':')
        if len(parts) < 3 or parts[0] != 'twd':
            return
        _, ticket, action, *args = parts
        if action == 'open':
            await inter.response.defer(with_message=True, ephemeral=True)
        else:
            await inter.response.defer(with_message=False)
        user = None
        index, page = 0, 0
        try:
            user = await self._user(inter)
            record = await asyncio.to_thread(deploy.load, user, ticket)
            if action == 'pack':
                index = int(inter.values[0])
            elif action in ('page', 'refresh', 'login', 'prepare', 'dc'):
                index = int(args[0])
            if not 0 <= index < len(record['data']['assignments']):
                raise ga.AccountError('Пак не найден.')
            if action == 'page':
                page = int(args[1])
            elif action == 'login':
                await ga.reconnect(user['discord_id'], user['guild_id'], user['bot_profile'], user['tier'], record['code'])
            elif action == 'dc':
                await deploy.edit_datacron(user, ticket, index, '' if inter.values[0] == '-' else inter.values[0], args[1])
            elif action == 'prepare':
                if deploy.revision(record['data']) != args[1]:
                    raise ga.AccountError('Выбор ДК изменился. Проверьте актуальную панель.')
                catalog = getattr(self.bot, 'datacron_cache', None) or datacron_catalog._cache
                if not catalog:
                    catalog = await datacron_catalog.get_catalog(self.bot.comlink)
                await deploy.prepare(user, ticket, {}, catalog)
            elif action == 'apply':
                await inter.edit_original_response(content='Выставляю доступные назначения и проверяю результат в игре…', embed=None, view=None)
                await deploy.apply(user, ticket, args[0])
            elif action not in ('open', 'pack', 'page', 'refresh'):
                raise ga.AccountError('Неизвестное действие.')
            await self._render(inter, user, ticket, index, page)
        except ga.AccountError as exc:
            if user:
                try:
                    await self._render(inter, user, ticket, index, page, str(exc))
                    return
                except ga.AccountError:
                    pass
            await inter.edit_original_response(content=str(exc), embed=None, view=None)
        except (ValueError, IndexError):
            await inter.edit_original_response(content='Панель устарела. Откройте назначение заново.', embed=None, view=None)

    @commands.Cog.listener('on_button_click')
    async def tw_deploy_button(self, inter):
        await self._dispatch(inter)

    @commands.Cog.listener('on_dropdown')
    async def tw_deploy_dropdown(self, inter):
        await self._dispatch(inter)


def setup(bot):
    bot.add_cog(TWGameDeployCog(bot))
