import json
import os
import sys
import disnake
from disnake.ext import commands
from dotenv import load_dotenv
import database
import guild_resolver
from swgoh_comlink import SwgohComlink

# stdout в докер-контейнере без TTY по умолчанию полностью буферизован — print()
# копится в буфере и попадает в `docker logs` только при заполнении буфера либо
# выходе процесса (тогда там оказывается вывод ПРЕДЫДУЩЕГО запуска, а не текущего).
# Форсируем построчный флаш, чтобы логи отражали реальное текущее состояние бота.
sys.stdout.reconfigure(line_buffering=True)

load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")

if not TOKEN:
    raise ValueError("❌ Ошибка: Переменная DISCORD_TOKEN не найдена в файле .env!")

# Ключ для Gemini API — не используется сейчас (бесплатный тариф Google блокирует
# запросы с датацентр-IP VPS, см. CLAUDE.md/память), но оставлен в .env на будущее.
GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY")

# Ключ для Mistral API (vision-разбор картинок ТБ-планирования, cogs/tb_order_image.py,
# модель mistral-medium-latest). Бесплатный тариф, работает с VPS (проверено 2026-08-21,
# в отличие от Gemini). Опционален — без него просто недоступна одна команда
# /тб_ордер_из_картинки, бот не падает.
MISTRAL_API_KEY = os.getenv("MISTRAL_API_KEY")

# Free-тариф Mistral даёт $10/мес включённого API-кредита (проверено вживую в консоли
# console.mistral.ai — Subscription/Billing, 2026-09-04), официального usage/billing API
# на этом тарифе нет (их Admin API требует Enterprise Backoffice — проверено вживую,
# backoffice.mistral.ai на Free-аккаунте редиректит в обычную консоль). Поэтому расход
# считаем сами по токенам из каждого vision-ответа (services/mistral_vision.py::
# call_vision_json -> database.record_mistral_usage) и сравниваем со порогом здесь —
# это оценка, не официальный биллинг, может немного разойтись, если ключ когда-нибудь
# станет использоваться ещё где-то помимо /тб_ордер_из_картинки и /фарм.
MISTRAL_MONTHLY_BUDGET_USD = 10.0
MISTRAL_BUDGET_WARNING_RATIO = 0.9

# Ключ для OpenRouter API (services/openrouter_vision.py) — АКТУАЛЬНЫЙ провайдер для
# /фарм и /тб_ордер_из_картинки с 2026-09-04, заменил Mistral (см. комментарий выше —
# Mistral Free с этой даты требует включённого Pay-As-You-Go, которого нет; MISTRAL_API_KEY
# и весь код вокруг него оставлены нетронутыми на случай, если это включат позже).
# Бесплатно, без привязки карты — проверено вживую с этого VPS перед переключением
# (OpenRouter не блокирует IP датацентра, в отличие от Gemini/Groq). Лимит без покупки
# кредитов: 20 запросов/мин, 50/сутки (их документация) — считаем по суткам, см.
# database.record_openrouter_request/get_openrouter_requests_today.
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")
OPENROUTER_DAILY_BUDGET_WARNING_RATIO = 0.9

# Ключ для Groq API — не используется: их API целиком (не только vision) блокирует
# запросы с датацентр-IP этого VPS (проверено 2026-08-24 изнутри контейнера,
# GET /models -> 403 "Access denied. Please check your network settings.",
# тот же класс блокировки, что у бесплатного Gemini, см. CLAUDE.md/память).
# Раньше здесь было "не используется из-за deprecated vision-модели llama-4-scout" —
# это тоже верно, но не первопричина: даже текстовые модели недоступны с этой сети.
# Ключ оставлен в .env на случай смены VPS/сети.
GROQ_API_KEY = os.getenv("GROQ_API_KEY")

COMLINK_URL = "http://localhost:3000" 

SNG_GUILD_ID = 931280548402442310    # Discord-сервер AbsoluteChaos — используется для seed_default_guild ниже

ALLY_CODE = "572624393"
N_LIMIT = 3

# За сколько дней до дедлайна задачи на прокачку (/задачи) слать напоминание —
# один раз на задачу (см. database.get_tasks_needing_reminder/mark_task_reminder_sent).
TASK_REMINDER_DAYS_BEFORE = 1

# Время (МСК, "ЧЧ:ММ"), в которое раз в сутки шлются напоминания о скором дедлайне
# и отмечаются просроченные задачи — по умолчанию, если гильдия не задала своё
# значение в guilds.tasks_notify_time (веб /settings). См. cogs/tasks.py::tasks_notify_loop.
TASKS_DEFAULT_NOTIFY_TIME = "10:00"

ALLOWED_ROLE_IDS = [1153753506772164629]
ALLOWED_USER_IDS = [291656027659698176]

# Доступ по умолчанию — только уровень "officer" (игровой ранг офицер/лидер в
# гильдии, см. guild_resolver.resolve_access). Команды из этого списка (полное
# qualified_name — "группа сабкоманда" для сабкоманд) дополнительно открыты и
# уровню "member" (рядовой участник участвующей гильдии). "регистрация" — особый
# случай: открыта вообще всем, включая tier=None — это единственная "дверь" в
# систему (см. main.py::_check_access, services/registration.py).
ALWAYS_ALLOWED_COMMANDS = {"регистрация"}
MEMBER_ACCESSIBLE_COMMANDS = {
    "дк_требования список",
    "статы_требования список",
    "статы_требования плейты",
    "статы",
    "статы_релик",
    "регистрация",
    "задачи отчёт",
}

# === Настройки ротационного тега ===
PING_CHANNEL_ID = 1222515211659907204  # ID канала, куда слать теги
PING_ROLE_ID = 1165274669394436197     # ID роли, которую тегать
PING_START_DATE = "2026-07-06"        # Первый понедельник первой тегаемой недели (ГГГГ-ММ-ДД)

# Расписание тегов: список словарей
#   time  – время UTC в формате "ЧЧ:ММ"
#   text  – текст сообщения (можно использовать @роль, но лучше добавлять роль через mention)
#   days  – список дней недели (0=пн, 1=вт, ..., 6=вс), когда тег активен
PING_SCHEDULE = [
    {"time": "18:50", "text": "взводы", "days": [0,1,2,3,4,5]},   # вт и пт
    {"time": "19:50", "text": "ордер", "days": [0,1,2,3,4,5]}    # вт и пт
]

# === Настройки поздравлений с днём рождения ===
BIRTHDAY_CHANNEL_ID = 1153767794203172874  # ID канала для поздравлений
BIRTHDAY_ROLE_ID = 1524054148734259230     # ID роли, которая будет выдаваться в ДР

OFFICER_CHANNEL_ID = 1222515211659907204  # ID канала для отчётов и уведомлений
ALLOWED_OFFICER_ROLE_ID = 1153753506772164629  # та же роль, что и для других команд

TB_PLAN_CHANNEL_ID = 1153762436084662334  # #ac-тб-оповещения — сюда офицеры постят "Восход Империи — N этап"

# Ветка с полным планом на все 6 этапов (Цель/Взводы/БЗ по каждой планете) —
# офицеры выкладывают её целиком перед началом ТБ, не по дням.
TB_ORDER_SOURCE_CHANNEL_ID = 1523597487934541834
# Роль, которую тегаем при ежедневной публикации ордера на актуальный этап
TB_ORDER_ROLE_ID = 1153753562254426192

# PLAYER_STATS_SYNC_HOURS теперь определён в database.py (веб-процесс тоже его читает,
# для оценки "автообновление через X" на /activity — main.py ему не виден).
PLAYER_STATS_SYNC_HOURS = database.PLAYER_STATS_SYNC_HOURS

comlink = SwgohComlink(url=COMLINK_URL)

intents = disnake.Intents.default()
intents.members = True 
intents.message_content = True 

class GuildManagerBot(commands.Bot):
    def __init__(self):
        super().__init__(
            command_prefix="!",
            intents=intents,
            # Команды ГЛОБАЛЬНЫЕ (не test_guilds) — гильдийные слэш-команды Discord
            # принципиально не показывает в личке бота, только внутри своей гильдии.
            # default_contexts=bot_dm разрешает вызывать их в ЛС; guild_resolver
            # всё равно решает, что конкретному discord_id можно, не завися от
            # контекста вызова. Побочный эффект: новый Discord-сервер, добавленный
            # через "/гильдия добавить", получает все команды сразу (это глобальная
            # регистрация), без ручного список-менеджмента, который был нужен раньше.
            default_install_types=disnake.ApplicationInstallTypes(guild=True),
            default_contexts=disnake.InteractionContextTypes(guild=True, bot_dm=True),
            # Автосинк слэш-команд (disnake) ловил баг: периодический авто-ресинк
            # иногда пушил ПУСТОЙ список в гильдию 931280548402442310, стирая все
            # слэш-команды гильдии целиком. Отключаем автосинк совсем, пока не
            # разобрались в первопричине — список команд теперь меняется только
            # вручную через Discord REST API (см. память проекта). Раньше это была
            # guild-scoped регистрация (test_guilds); теперь при ручном пуше
            # список нужно слать на ГЛОБАЛЬНЫЙ endpoint (без guild_id), см. CLAUDE.md.
            command_sync_flags=commands.CommandSyncFlags.none()
        )
        self.comlink = comlink
        self.ally_code = ALLY_CODE
        self.n_limit = N_LIMIT
        self.allowed_user_ids = ALLOWED_USER_IDS
        self.allowed_role_ids = ALLOWED_ROLE_IDS
        # guild_roster_cache — легаси, один общий кэш (мирроит гильдию id=1),
        # оставлен для cogs, ещё не переведённых на guild_id (см. Milestone 4
        # плана мультитенантности); guild_roster_caches — новый, per-guild.
        # Убрать guild_roster_cache, когда миграция всех cogs завершится.
        self.guild_roster_cache = {}
        self.guild_roster_caches = {}
        
        # === Новые атрибуты для ротационного тега ===
        self.PING_CHANNEL_ID = PING_CHANNEL_ID
        self.PING_ROLE_ID = PING_ROLE_ID
        self.PING_START_DATE = PING_START_DATE
        self.PING_SCHEDULE = PING_SCHEDULE
        
        self.BIRTHDAY_CHANNEL_ID = BIRTHDAY_CHANNEL_ID
        self.BIRTHDAY_ROLE_ID = BIRTHDAY_ROLE_ID
        
        self.OFFICER_CHANNEL_ID = OFFICER_CHANNEL_ID
        self.ALLOWED_OFFICER_ROLE_ID = ALLOWED_OFFICER_ROLE_ID

        self.TB_PLAN_CHANNEL_ID = TB_PLAN_CHANNEL_ID
        self.TB_ORDER_SOURCE_CHANNEL_ID = TB_ORDER_SOURCE_CHANNEL_ID
        self.TB_ORDER_ROLE_ID = TB_ORDER_ROLE_ID

        self.PLAYER_STATS_SYNC_HOURS = PLAYER_STATS_SYNC_HOURS
        self.TASK_REMINDER_DAYS_BEFORE = TASK_REMINDER_DAYS_BEFORE
        self.TASKS_DEFAULT_NOTIFY_TIME = TASKS_DEFAULT_NOTIFY_TIME

        self.google_api_key = GOOGLE_API_KEY
        self.mistral_api_key = MISTRAL_API_KEY
        self.mistral_monthly_budget_usd = MISTRAL_MONTHLY_BUDGET_USD
        self.mistral_budget_warning_ratio = MISTRAL_BUDGET_WARNING_RATIO
        self.openrouter_api_key = OPENROUTER_API_KEY
        self.openrouter_daily_warning_ratio = OPENROUTER_DAILY_BUDGET_WARNING_RATIO
        self.groq_api_key = GROQ_API_KEY

bot = GuildManagerBot()

@bot.event
async def on_ready():
    # 1. Инициализация таблиц (создает таблицы, если их нет)
    database.init_db()
    database.init_birthday_table()

    # Сид первой (текущей) гильдии в реестр мультитенантности — не трогает
    # существующие строки, отрабатывает один раз, пока таблица guilds пуста.
    database.seed_default_guild(
        name="AbsoluteChaos",
        ally_code=ALLY_CODE,
        discord_guild_id=str(SNG_GUILD_ID),
        member_role_id=str(TB_ORDER_ROLE_ID),
        officer_role_id=str(ALLOWED_OFFICER_ROLE_ID),
        ping_channel_id=str(PING_CHANNEL_ID),
        ping_role_id=str(PING_ROLE_ID),
        ping_start_date=PING_START_DATE,
        ping_schedule_json=json.dumps(PING_SCHEDULE),
        birthday_channel_id=str(BIRTHDAY_CHANNEL_ID),
        birthday_role_id=str(BIRTHDAY_ROLE_ID),
        officer_channel_id=str(OFFICER_CHANNEL_ID),
        tb_plan_channel_id=str(TB_PLAN_CHANNEL_ID),
        tb_order_source_channel_id=str(TB_ORDER_SOURCE_CHANNEL_ID),
        tb_order_role_id=str(TB_ORDER_ROLE_ID),
    )

    # Сид первого супер-админа (текущий владелец бота) — идемпотентно (INSERT OR
    # IGNORE), безопасно на каждом старте. Супер-админы управляют гильдиями и
    # выдают доступ другим (см. guild_resolver.resolve_access, cogs/admin_management.py).
    database.seed_bot_admin(str(ALLOWED_USER_IDS[0]), added_by="startup-seed")

    # 2. Установка статуса бота
    await bot.change_presence(
        status=disnake.Status.online,
        activity=disnake.Activity(type=disnake.ActivityType.watching, name="Следит за игроками AC")
    )
    print(f"🤖 Бот {bot.user} успешно запущен в мультисерверном режиме!")

def _check_access(author, command_name: str) -> bool:
    """Единая точка входа для гейта: по умолчанию нужен tier="officer"
    (guild_resolver.resolve_access — игровой ранг офицер/лидер, супер-админ,
    или ручной грант уровня officer). ALWAYS_ALLOWED_COMMANDS пропускает вообще
    всех (сейчас только "регистрация" — единственная "дверь" в систему прав, без
    неё новый участник никогда не получит tier). MEMBER_ACCESSIBLE_COMMANDS
    дополнительно открыт tier="member" (рядовой участник)."""
    if author.id in bot.allowed_user_ids or guild_resolver.is_super_admin(author):
        return True
    if command_name in ALWAYS_ALLOWED_COMMANDS:
        return True
    tier = guild_resolver.resolve_tier(author)
    if command_name in MEMBER_ACCESSIBLE_COMMANDS:
        if tier in ("member", "officer"):
            return True
    elif tier == "officer":
        return True
    raise commands.CheckFailure(f"Недостаточно прав для команды «{command_name}» (уровень: {tier or 'нет доступа'})")

# @bot.check — только для текстовых (!) команд, слэш-команды не проверяет
# (см. disnake BotBase.check: "This is for text commands only, and doesn't
# apply to application commands."). Нужен отдельный @bot.slash_command_check.
@bot.check
async def check_guild_roles(ctx):
    return _check_access(ctx.author, ctx.command.qualified_name)

@bot.slash_command_check
async def check_guild_roles_slash(inter):
    return _check_access(inter.author, inter.application_command.qualified_name)

@bot.event
async def on_slash_command_completion(inter: disnake.ApplicationCommandInteraction):
    # Считаем по "чистому" qualified_name (группа+сабкоманда), без параметров
    # вызова — для статистики "какими командами пользуются" на /admin/command-usage.
    database.log_command_usage(inter.application_command.qualified_name)

@bot.event
async def on_slash_command_error(inter: disnake.ApplicationCommandInteraction, error: Exception):
    if hasattr(error, "original"):
        error = error.original
    if isinstance(error, (commands.MissingRole, commands.MissingAnyRole, commands.CheckFailure)):
        try:
            tier = guild_resolver.resolve_tier(inter.author)
            cmd_name = inter.application_command.qualified_name if inter.application_command else "?"
            print(f"🛑 [Доступ] {inter.author} ({inter.author.id}) tier={tier} команда=/{cmd_name}: {error}")
        except Exception as log_err:
            print(f"🛑 [Доступ] (не удалось залогировать детали: {log_err}): {error}")
        if not inter.response.is_done():
            await inter.response.send_message(
                "🛑 **Доступ заблокирован:** У вас нет прав для использования этого бота",
                ephemeral=True
            )
    else:
        print(f"❌ Непредвиденная ошибка при выполнении команды: {error}")

if __name__ == "__main__":
    if not os.path.exists("cogs"):
        os.makedirs("cogs")
    for filename in os.listdir("./cogs"):
        if filename.endswith(".py") and not filename.startswith("__"):
            bot.load_extension(f"cogs.{filename[:-3]}")
            print(f"📦 Модуль {filename} успешно подключен к боту")
    bot.run(TOKEN)