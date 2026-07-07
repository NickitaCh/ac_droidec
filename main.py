import os
import disnake
from disnake.ext import commands
from dotenv import load_dotenv
import database
from swgoh_comlink import SwgohComlink

load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")

if not TOKEN:
    raise ValueError("❌ Ошибка: Переменная DISCORD_TOKEN не найдена в файле .env!")

COMLINK_URL = "http://localhost:3000" 

TUSA_GUILD_ID = 1105914797054238830
SNG_GUILD_ID = 931280548402442310
test_guilds_list = [TUSA_GUILD_ID, SNG_GUILD_ID]

ALLY_CODE = "572624393"  
N_LIMIT = 3              

ALLOWED_ROLE_IDS = [1153753506772164629] 
ALLOWED_USER_IDS = [291656027659698176]  

# === Настройки ротационного тега ===
PING_CHANNEL_ID = 1222515211659907204  # ID канала, куда слать теги
PING_ROLE_ID = 1165274669394436197     # ID роли, которую тегать
PING_START_DATE = "2026-07-06"        # Первый понедельник первой тегаемой недели (ГГГГ-ММ-ДД)

# Расписание тегов: список словарей
#   time  – время UTC в формате "ЧЧ:ММ"
#   text  – текст сообщения (можно использовать @роль, но лучше добавлять роль через mention)
#   days  – список дней недели (0=пн, 1=вт, ..., 6=вс), когда тег активен
PING_SCHEDULE = [
    {"time": "18:50", "text": "взводы", "days": [0,1,2,3,4,5,6]},   # вт и пт
    {"time": "19:50", "text": "ордер", "days": [0,1,2,3,4,5,6]}    # вт и пт
]

# === Настройки поздравлений с днём рождения ===
BIRTHDAY_CHANNEL_ID = 1153767794203172874  # ID канала для поздравлений
BIRTHDAY_ROLE_ID = 1524054148734259230     # ID роли, которая будет выдаваться в ДР

comlink = SwgohComlink(url=COMLINK_URL)

intents = disnake.Intents.default()
intents.members = True 
intents.message_content = True 

class GuildManagerBot(commands.Bot):
    def __init__(self):
        super().__init__(
            command_prefix="!", 
            intents=intents, 
            test_guilds=test_guilds_list
        )
        self.comlink = comlink
        self.ally_code = ALLY_CODE
        self.n_limit = N_LIMIT
        self.allowed_user_ids = ALLOWED_USER_IDS
        self.allowed_role_ids = ALLOWED_ROLE_IDS
        self.guild_roster_cache = {}  
        
        # === Новые атрибуты для ротационного тега ===
        self.PING_CHANNEL_ID = PING_CHANNEL_ID
        self.PING_ROLE_ID = PING_ROLE_ID
        self.PING_START_DATE = PING_START_DATE
        self.PING_SCHEDULE = PING_SCHEDULE
        
        self.BIRTHDAY_CHANNEL_ID = BIRTHDAY_CHANNEL_ID
        self.BIRTHDAY_ROLE_ID = BIRTHDAY_ROLE_ID

bot = GuildManagerBot()

@bot.event
async def on_ready():
    # 1. Инициализация таблиц (создает таблицы, если их нет)
    database.init_db()
    database.init_birthday_table()
    
    # 2. Установка статуса бота
    await bot.change_presence(
        status=disnake.Status.online,
        activity=disnake.Activity(type=disnake.ActivityType.watching, name="Следит за игроками AC")
    )
    print(f"🤖 Бот {bot.user} успешно запущен в мультисерверном режиме!")

@bot.check
async def check_guild_roles(ctx):
    if ctx.author.id in bot.allowed_user_ids:
        return True
    if not ctx.guild:
        return False
    user_role_ids = [role.id for role in ctx.author.roles]
    has_permission = any(role_id in bot.allowed_role_ids for role_id in user_role_ids)
    if not has_permission:
        raise commands.MissingAnyRole(bot.allowed_role_ids)
    return True

@bot.event
async def on_slash_command_error(inter: disnake.ApplicationCommandInteraction, error: Exception):
    if hasattr(error, "original"):
        error = error.original
    if isinstance(error, (commands.MissingRole, commands.MissingAnyRole, commands.CheckFailure)):
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