"""Resolve the recipient from a live ally code; notify after committing an invite."""
import asyncio
import re

import database
from services import alliances, bot_profiles, discord_rest


async def resolve(comlink, code):
    code = re.sub(r'[\s-]', '', code)
    if not re.fullmatch(r'[1-9]{9}', code):
        raise alliances.AllianceError('Введите код союзника: 9 цифр без нуля.')
    try:
        player = await asyncio.to_thread(comlink.get_player, code)
    except Exception:
        raise alliances.AllianceError('Не удалось проверить игрока. Повторите позже.') from None
    swgoh_id = (player or {}).get('guildId')
    if not swgoh_id:
        raise alliances.AllianceError('Игрок не найден или не состоит в гильдии.')
    cfg = await asyncio.to_thread(database.get_guild_config_by_swgoh_id, str(swgoh_id))
    if not cfg:
        raise alliances.AllianceError('Гильдия этого игрока не подключена к боту. Её офицеру нужно подключить гильдию на странице «Подписка».')
    return cfg


async def notify(gid, target, actor, alliance_name):
    from services.payments import SITE_URL
    source = await asyncio.to_thread(database.get_guild_config, gid)
    recipient = await asyncio.to_thread(database.get_guild_config, target)
    text = f'Гильдия «{source["name"]}» приглашает «{recipient["name"]}» в альянс «{alliance_name}». Приглашение действует 14 дней. Принять или отклонить: {SITE_URL}/alliances'
    leaders = []
    rows = await asyncio.to_thread(database.get_all_user_mappings_with_rank, target)
    for code, _, level in rows:
        if level == 4:
            owner = await asyncio.to_thread(database.find_ally_code_owner, code, guild_id=target)
            if owner:
                leaders.append(owner)
    failures = 0
    for profile_gid, who, content in [(target, who, text) for who in set(leaders)] + [
            (gid, actor, f'Приглашение гильдии «{recipient["name"]}» в альянс «{alliance_name}» создано. Статус: {SITE_URL}/alliances#members')]:
        try:
            with bot_profiles.delivery_context(bot_profiles.guild_profile(profile_gid)):
                await discord_rest.send_dm(who, content)
        except Exception as exc:
            failures += 1
            print(f'⚠️ [Альянс] Личное уведомление не доставлено: {exc}', flush=True)
    return bool(leaders), failures
