"""Запуск: python -m tg_bot (нужен TELEGRAM_BOT_TOKEN в .env).

Long polling, без вебхука — не нужен ни отдельный порт, ни правка nginx. У одного
токена может работать только один процесс getUpdates (второй получит 409 Conflict),
поэтому локально с боевым токеном не запускать."""

import asyncio
import os
import sys

from dotenv import load_dotenv
load_dotenv()
from swgoh_comlink import SwgohComlink

import database
from services import bot_profiles, stat_forecast
from tg_bot import checks, handlers, inline, more, notifier, relay_in, feedback  # noqa: F401 — регистрируют команды
from tg_bot.api import TelegramAPI
from tg_bot.core import App, menu_commands, run_polling
from tg_bot import command_access

command_access.install()

sys.stdout.reconfigure(line_buffering=True)

COMLINK_URL = "http://localhost:3000"
SITE_URL = "https://swgoh-sng.ru"

# Профильные тексты бота в Telegram (BotFather «Edit Bot» их тоже покажет, но отсюда
# они переустанавливаются сами при каждом запуске — не нужно помнить поправить руками
# после переименования сайта и т.п.). Картинку профиля бота Bot API ставить не умеет —
# она выставляется вручную через @BotFather → /setuserpic (файл: assets/tg_bot_avatar.png).
BOT_DESCRIPTION = (
    "«Дроидец» — бот для гильдий Star Wars: Galaxy of Heroes, тот же, что работает в Discord.\n\n"
    "Статы по нормам гильдии, датакроны, профиль игрока и гильдии картинкой, задачи на прокачку, "
    "нарушения, оповещения гильдии в чат.\n\n"
    "Привяжите код союзника — /reg 123456789, затем /help — список команд.\n\n"
    "Гильдия ещё не подключена к боту? swgoh-sng.ru/start"
)
BOT_SHORT_DESCRIPTION = "«Дроидец» — бот гильдий SWGOH: статы, датакроны, профиль, задачи. Тот же, что и в Discord."


async def main():
    load_dotenv()
    profile = bot_profiles.activate_worker()
    token = bot_profiles.token("telegram", profile)
    if not token:
        raise SystemExit(f"Не настроен Telegram-токен версии {profile}.")
    api = TelegramAPI(token)
    me = await api.get_me()
    comlink = SwgohComlink(url=COMLINK_URL)
    app = App(api=api, comlink=comlink, username=me["username"], bot_id=me["id"], site_url=SITE_URL, profile=profile)
    await api.set_my_commands(menu_commands())
    await api.set_my_description(BOT_DESCRIPTION)
    await api.set_my_short_description(BOT_SHORT_DESCRIPTION)
    # Сайт строит ссылку «Привязать Telegram» на этого бота (web/routes/telegram.py).
    database.set_bot_state(bot_profiles.state_key("telegram_bot_username", profile), me["username"], guild_id=0)
    database.set_bot_state(bot_profiles.state_key("telegram_bot_id", profile), str(me["id"]), guild_id=0)
    # Вебхук и polling несовместимы — на случай, если кто-то ставил вебхук руками.
    await api.call("deleteWebhook")
    if profile == "test":
        stat_forecast.start_background_build(comlink)
    from services import memory_maintenance
    memory_maintenance.start("Telegram")
    print(f"✅ [TG] @{me['username']} запущен")
    notifier_task = asyncio.create_task(notifier.run_notifier(app))
    try:
        await run_polling(app)
    finally:
        notifier_task.cancel()
        await memory_maintenance.stop()
        await api.close()


if __name__ == "__main__":
    from services.bot_worker_lock import worker_instance
    with worker_instance("telegram", bot_profiles.runtime_profile()):
        asyncio.run(main())
