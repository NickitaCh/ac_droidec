"""Apply reviewed Discord/Telegram bot branding; dry run unless --apply."""
import argparse
import base64
import io
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import httpx
from dotenv import load_dotenv
from PIL import Image
from services import bot_profiles

DESCRIPTIONS = {
    'prod': 'Дроидец — помощник гильдии в Star Wars: Galaxy of Heroes. Задачи, требования, ТБ, ростеры и уведомления. Управление: https://swgoh-sng.ru',
}
SHORT = {
    'prod': 'Помощник гильдии SWGOH: задачи, требования, ТБ и уведомления. swgoh-sng.ru',
}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--profile', choices=['prod'], default='prod')
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    load_dotenv(ROOT / '.env')
    png = (ROOT / 'assets' / 'bot-branding' / (args.profile + '.png')).read_bytes()
    print(args.profile, DESCRIPTIONS[args.profile], 'avatar bytes', len(png))
    if not args.apply:
        return
    if not bot_profiles.configured(args.profile):
        raise ValueError('Branding requires both Discord and Telegram tokens for the selected profile.')
    # Telegram requires JPEG for InputProfilePhotoStatic (encoding only).
    jpeg = io.BytesIO()
    Image.open(io.BytesIO(png)).convert('RGB').save(jpeg, format='JPEG', quality=95)
    encoded = 'data:image/png;base64,' + base64.b64encode(png).decode()
    headers = {'Authorization':'Bot ' + bot_profiles.token('discord', args.profile),
               'User-Agent':'DiscordBot (https://swgoh-sng.ru, 1.0)'}
    with httpx.Client(timeout=60) as client:
        for path, body in [('/applications/@me', {'description':DESCRIPTIONS[args.profile], 'icon':encoded}),
                           ('/users/@me', {'avatar':encoded})]:
            r = client.patch('https://discord.com/api/v10' + path, headers=headers, json=body)
            if r.status_code >= 400:
                raise RuntimeError('Discord branding: ' + str(r.status_code) + ' ' + r.text[:300])
        for language in ('', 'ru'):
            for method, body in [('setMyDescription', {'description':DESCRIPTIONS[args.profile], 'language_code':language}),
                                 ('setMyShortDescription', {'short_description':SHORT[args.profile], 'language_code':language})]:
                r = client.post('https://api.telegram.org/bot' + bot_profiles.token('telegram', args.profile) + '/' + method, json=body)
                if not r.json().get('ok'):
                    raise RuntimeError('Telegram branding: ' + str(r.json().get('description')))
        r = client.post('https://api.telegram.org/bot' + bot_profiles.token('telegram', args.profile) + '/setMyProfilePhoto',
                        data={'photo':json.dumps({'type':'static','photo':'attach://avatar'})},
                        files={'avatar':('avatar.jpg',jpeg.getvalue(),'image/jpeg')})
        if not r.json().get('ok'):
            raise RuntimeError('Telegram avatar: ' + str(r.json().get('description')))
        # Verify the published values without ever printing credentials.
        me = client.get('https://discord.com/api/v10/users/@me', headers=headers).json()
        app = client.get('https://discord.com/api/v10/applications/@me', headers=headers).json()
        assert me.get('avatar') and app.get('icon') and app['description'] == DESCRIPTIONS[args.profile]
        tg_base = 'https://api.telegram.org/bot' + bot_profiles.token('telegram', args.profile)
        tg_me = client.post(tg_base + '/getMe').json()['result']
        photos = client.post(tg_base + '/getUserProfilePhotos', json={'user_id':tg_me['id']}).json()['result']
        assert photos['total_count'] > 0
        for language in ('', 'ru'):
            desc = client.post(tg_base + '/getMyDescription', json={'language_code':language}).json()['result']['description']
            assert desc == DESCRIPTIONS[args.profile]
        print('Verified Discord avatar, application icon/description, Telegram photo/description:', me['id'], tg_me['username'])


if __name__ == '__main__':
    main()
