"""«Привязать Telegram» в «Моём профиле»: одноразовая ссылка на Telegram-бота
(tg_bot), после перехода по которой его Telegram-аккаунт становится тем же игроком,
что и этот Discord (tg_bot/store.py::link_to_discord). Вход на сайт через Discord
и есть доказательство, что Discord-аккаунт свой."""

from urllib.parse import urlencode

from fastapi import APIRouter, Depends
from fastapi.responses import RedirectResponse

import database
from tg_bot import store
from web.deps import own_accounts, require_member_access
from web.routes import guild_dashboard

router = APIRouter()

# Имя бота пишет сам Telegram-бот при запуске (tg_bot/__main__.py) — сайт его не знает.
BOT_USERNAME_STATE_KEY = "telegram_bot_username"


def bot_username() -> str | None:
    return database.get_bot_state(BOT_USERNAME_STATE_KEY, guild_id=0)


# Кнопка «Привязать Telegram» в карточке игрока (player_card.html) видна, только когда
# бот хоть раз запускался — у каждого роута свой Jinja2Templates, поэтому регистрируем тут.
guild_dashboard.templates.env.globals["telegram_bot_username"] = bot_username


@router.get("/me/telegram")
async def link_telegram(user: dict = Depends(require_member_access)):
    username = bot_username()
    if not username:
        accounts = own_accounts(user)
        back = f"/player/{accounts[0][0]}" if accounts else "/me"
        return RedirectResponse(f"{back}?{urlencode({'alt_error': 'Telegram-бот пока не запущен.'})}", status_code=303)
    token = store.create_link_token(str(user["discord_id"]))
    return RedirectResponse(f"https://t.me/{username}?start=link_{token}", status_code=303)
