"""Команды Telegram-бота. Логика — общие services/* (как у Discord-команд в cogs/),
здесь только разбор аргументов и вывод в Telegram (HTML-разметка, кнопки)."""

import asyncio

import database
import guild_resolver
from services import player_profile as pp
from services import profile_image, registration, stat_forecast
from tg_bot import store
from tg_bot.core import TIER_ANY, TIER_MEMBER, TIER_OFFICER, Ctx, callback, command, esc

TIER_LABELS = {"officer": "офицер", "member": "участник", None: "нет доступа"}
GUILD_WORDS = {"гильдия", "guild", "г"}
ALT_WORDS = {"альт", "alt"}
NAME_CHOICES_MAX = 8


# ---------------------------------------------------------------- помощь

HELP_SECTIONS = (
    ("Аккаунт", (
        ("/reg <code>код</code> [альт]", "привязать игровой аккаунт (код союзника); «альт» — второй аккаунт"),
        ("/unreg [<code>код</code>]", "отвязать аккаунт"),
        ("/me", "мои аккаунты, гильдия и права"),
    )),
    ("Игроки", (
        ("/profile", "ваш профиль картинкой"),
        ("/profile <code>ник</code> | <code>код</code> | @user", "профиль другого игрока; можно ответить командой на его сообщение"),
        ("/profile гильдия", "сводка по гильдии"),
        ("/unit Рей [r7|g12|7*] [| Ник, код]", "персонаж картинкой, прогноз прокачки, сравнение игроков"),
        ("/guildunit Рей", "у кого в гильдии есть юнит"),
        ("/ready Лорд Вейдер [гильдия] [| Ник]", "готовность к цели «Руководства»"),
        ("/faction Империя [| Ник]", "персонажи фракции у игрока"),
    )),
    ("Проверки (участник — себя, офицер — любого)", (
        ("/stats [плейт] [игрок] [кресты] [обновить]", "статы по плейту, как /статы"),
        ("/stats гильдия [плейт]", "сводка по гильдии (офицеры)"),
        ("/dc [игрок]", "датакроны по требованиям гильдии"),
        ("/omicrons [игрок]", "какие приоритетные омикроны можно поставить"),
        ("/tasks [игрок | гильдия]", "задачи на прокачку"),
    )),
    ("Офицеры", (
        ("/warns [игрок]", "нарушения: таблица гильдии, досье, добавить и снять"),
    )),
    ("Чат гильдии (офицеры)", (
        ("/bind", "привязать эту группу к вашей гильдии — для оповещений"),
        ("/notify", "какие оповещения гильдии слать в группу (энка, задачи, омикроны)"),
        ("/unbind", "отвязать группу"),
    )),
)


def _help_text(ctx: Ctx) -> str:
    lines = ["<b>«Дроидец»</b> — бот гильдий SWGOH (тот же, что в Discord).", ""]
    for title, rows in HELP_SECTIONS:
        lines.append(f"<b>{title}</b>")
        lines.extend(f"{cmd} — {text}" for cmd, text in rows)
        lines.append("")
    lines.append(f"🔎 Подсказки имён: наберите <code>@{esc(ctx.app.username)} дарт джа</code> — бот покажет список; "
                 "в начале можно указать «гильдия», «готовность» или «фракция».")
    lines.append("")
    if ctx.access["tier"] is None:
        lines.append("Начните с привязки аккаунта: <code>/reg 123456789</code>")
    lines.append(f"Сайт с отчётами и настройками: {ctx.app.site_url}")
    return "\n".join(lines)


@command("start", "Начало работы", tier=TIER_ANY, menu=False)
async def cmd_start(ctx: Ctx):
    if ctx.args.startswith("link_") and ctx.is_private:
        await _link_discord(ctx, ctx.args[len("link_"):])
        return
    await ctx.reply(_help_text(ctx))


@command("help", "Список команд", tier=TIER_ANY, aliases=("помощь", "команды"))
async def cmd_help(ctx: Ctx):
    await ctx.reply(_help_text(ctx))


# ---------------------------------------------------------------- аккаунт

def _link_html(ctx: Ctx) -> str:
    """Ссылка «связать с Discord»: сайт попросит войти через Discord и сразу вернёт
    в бота с одноразовым токеном (web/routes/telegram.py)."""
    return f'<a href="{ctx.app.site_url}/me/telegram">связать Telegram с Discord</a>'


def _tg_conflict_error(existing_owner: str, link: str) -> str:
    if store.is_tg_owner(existing_owner):
        return (
            "Этот код союзника уже привязан к другому игроку в Telegram.\n"
            "Если это ошибка, офицер может перепривязать: ответить на ваше сообщение командой <code>/reg код</code>."
        )
    return (
        "Этот код союзника уже привязан к аккаунту Discord.\n"
        f"Если это ваш Discord — {link} (вход на сайт через Discord, один клик). "
        "Тогда бот в Telegram и в Discord будет знать вас как одного игрока."
    )


def _accounts_lines(owner_id: str) -> list[str]:
    guild_names = {g["id"]: g.get("name") for g in database.get_all_guild_configs(active_only=False)}
    lines = []
    for guild_id, ally_code, name, is_main in database.get_all_registrations_for_discord_id(owner_id):
        mark = "основной" if is_main else "альт"
        lines.append(f"• {esc(name)} — <code>{ally_code}</code> ({mark}, {esc(guild_names.get(guild_id) or '?')})")
    return lines


@command("reg", "Привязать игровой аккаунт (код союзника)", tier=TIER_ANY, aliases=("регистрация",))
async def cmd_reg(ctx: Ctx):
    words = ctx.args.split()
    is_alt = any(w.lower() in ALT_WORDS for w in words)
    code = guild_resolver.normalize_ally_code(" ".join(w for w in words if w.lower() not in ALT_WORDS))
    if code is None:
        await ctx.reply("Укажите код союзника из 9 цифр: <code>/reg 123456789</code> (для второго аккаунта — <code>/reg 123456789 альт</code>).")
        return

    owner_id, allow_reassign, whose, officer_guild_id = ctx.owner_id, False, "Ваш аккаунт", None
    target = ctx.reply_user
    if target is not None:
        # Офицер регистрирует другого игрока — ответом на его сообщение (как «участник:» в Discord).
        if ctx.access["tier"] != "officer":
            await ctx.reply("🔒 Регистрировать других игроков могут только офицеры. Свой аккаунт — без ответа на сообщение.")
            return
        owner_id, allow_reassign = store.touch_user(target), True
        officer_guild_id = None if ctx.access.get("is_super_admin") else ctx.access["guild_id"]
        whose = f"Аккаунт для {esc(target.get('first_name') or target.get('username') or 'игрока')}"

    registered_by = None
    if owner_id != ctx.owner_id:
        registered_by = " ".join(p for p in (ctx.user.get("first_name"), ctx.user.get("last_name")) if p) or ctx.user.get("username") or "Офицер"

    await ctx.action()
    result = await registration.register_player(
        ctx.app.comlink, owner_id, code, is_alt=is_alt, allow_reassign=allow_reassign,
        conflict_error=lambda owner: _tg_conflict_error(owner, _link_html(ctx)), officer_guild_id=officer_guild_id,
        registered_by=registered_by,
    )
    if not result.ok:
        await ctx.reply(f"❌ {result.error}")
        return
    access = guild_resolver.resolve_access(owner_id)
    kind = "основной" if result.is_main else "альт"
    lines = [f"✅ {whose} привязан: <b>{esc(result.ingame_name)}</b> (<code>{result.ally_code}</code>, {kind})."]
    if access["tier"] is None:
        lines.append("Права появятся, когда бот обновит состав гильдии (раз в час).")
    else:
        lines.append(f"Права: {TIER_LABELS[access['tier']]}. Список команд — /help")
    if registered_by:
        lines.append(esc(registration.notified_note(result)))
    else:
        lines.append(registration.CONSENT_NOTE)
    await ctx.reply("\n".join(lines))


@command("unreg", "Отвязать игровой аккаунт", tier=TIER_ANY, aliases=("регистрация_отмена",))
async def cmd_unreg(ctx: Ctx):
    regs = database.get_all_registrations_for_discord_id(ctx.owner_id)
    if not regs:
        await ctx.reply("У вас нет привязанных аккаунтов.")
        return
    code = guild_resolver.normalize_ally_code(ctx.args) if ctx.args else None
    if code is None and len(regs) > 1:
        await ctx.reply("У вас несколько аккаунтов — укажите, какой отвязать: <code>/unreg код</code>\n" + "\n".join(_accounts_lines(ctx.owner_id)))
        return
    match = next((r for r in regs if r[1] == code), None) if code else regs[0]
    if match is None:
        await ctx.reply(f"Код <code>{code}</code> к вам не привязан.")
        return
    result = await registration.unregister_player(ctx.owner_id, match[0], match[1])
    if not result.ok:
        await ctx.reply(f"❌ {esc(result.error)}")
        return
    await ctx.reply(f"✅ Отвязан {esc(result.ingame_name)} (<code>{result.ally_code}</code>).")


@command("me", "Мои аккаунты и права", tier=TIER_ANY, aliases=("я",))
async def cmd_me(ctx: Ctx):
    access = ctx.access
    lines = []
    accounts = _accounts_lines(ctx.owner_id)
    if accounts:
        lines.append("<b>Аккаунты</b>")
        lines.extend(accounts)
    else:
        lines.append("Аккаунт не привязан — <code>/reg 123456789</code>")
    if access["guild_id"]:
        cfg = database.get_guild_config(access["guild_id"]) or {}
        lines.append(f"\nГильдия: <b>{esc(cfg.get('name') or '?')}</b>, права: {TIER_LABELS[access['tier']]}")
    if store.is_tg_owner(ctx.owner_id):
        lines.append(f"\nDiscord не связан. Если вы есть и в Discord-боте — {_link_html(ctx)}.")
    else:
        lines.append("\nСвязан с Discord — это один и тот же игрок в обоих ботах. Разорвать связь: /unlink")
    await ctx.reply("\n".join(lines))


async def _link_discord(ctx: Ctx, token: str):
    discord_id = store.consume_link_token(token)
    if discord_id is None:
        await ctx.reply(f"Ссылка устарела или уже использована — получите новую: {_link_html(ctx)}.")
        return
    moved = store.link_to_discord(ctx.user["id"], discord_id)
    ctx.owner_id = discord_id
    ctx._access = None
    lines = ["✅ Telegram связан с вашим Discord — теперь это один игрок с теми же аккаунтами и правами."]
    if moved:
        lines.append(f"Перенесено привязок из Telegram: {moved}.")
    accounts = _accounts_lines(discord_id)
    if accounts:
        lines.append("")
        lines.extend(accounts)
    await ctx.reply("\n".join(lines))


@command("unlink", "Разорвать связь с Discord", tier=TIER_ANY, menu=False, where="private")
async def cmd_unlink(ctx: Ctx):
    if store.is_tg_owner(ctx.owner_id):
        await ctx.reply("Telegram и так не связан с Discord.")
        return
    store.unlink_from_discord(ctx.user["id"])
    await ctx.reply("Связь с Discord разорвана: аккаунты остались за Discord, здесь привяжите заново через /reg.")


# ---------------------------------------------------------------- выбор игрока

async def _resolve_target(ctx: Ctx, guild_id: int, text: str, choice_prefix: str) -> str | None:
    """Код союзника игрока, о котором спрашивают: ответ на сообщение / @ник (привязка в
    любой гильдии бота, как «участник:» в Discord) / код / имя из состава своей гильдии /
    пусто = сам. None — уже ответили (ошибка или кнопки выбора)."""
    replied = ctx.reply_user
    if replied is not None and not text:
        owner = store.get_owner_id(replied["id"])
        reg = database.get_any_main_registration(owner) if owner else None
        if not reg:
            await ctx.reply("Этот игрок не привязал аккаунт к боту (/reg). Укажите его ник или код союзника.")
            return None
        return reg[0]

    if not text:
        reg = database.get_user_registration(ctx.owner_id, guild_id=guild_id)
        if not reg:
            await ctx.reply("Ваш аккаунт не привязан — <code>/reg код</code>, или укажите ник/код игрока.")
            return None
        return reg[0]

    code = guild_resolver.normalize_ally_code(text)
    if code:
        return code

    if text.startswith("@"):
        found = store.find_by_username(text)
        reg = database.get_any_main_registration(found["owner_id"]) if found else None
        if not reg:
            await ctx.reply(f"{esc(text)} не привязал аккаунт к боту — укажите ник в игре или код союзника.")
            return None
        return reg[0]

    roster = [(name, ally_code) for _d, ally_code, name in database.get_all_user_mappings(guild_id) if name]
    needle = text.casefold()
    exact = [r for r in roster if r[0].casefold() == needle]
    matches = exact or [r for r in roster if needle in r[0].casefold()]
    if len(matches) == 1:
        return matches[0][1]
    if not matches:
        await ctx.reply(f"Игрок «{esc(text)}» не найден в составе гильдии. Можно указать код союзника.")
        return None
    matches.sort(key=lambda r: r[0].casefold())
    keyboard = [[{"text": name, "callback_data": f"{choice_prefix}|{ally_code}"}] for name, ally_code in matches[:NAME_CHOICES_MAX]]
    more = f"\n…и ещё {len(matches) - NAME_CHOICES_MAX}, уточните ник." if len(matches) > NAME_CHOICES_MAX else ""
    await ctx.reply(f"Нашлось несколько игроков по «{esc(text)}» — выберите:{more}", keyboard=keyboard)
    return None


# ---------------------------------------------------------------- /profile

async def _guild_gp(comlink, swgoh_guild_id: str | None) -> int | None:
    if not swgoh_guild_id:
        return None
    try:
        resp = await asyncio.to_thread(comlink.get_guild, swgoh_guild_id)
        payload = (resp or {}).get("guild", resp or {})
        return int(payload.get("profile", {}).get("guildGalacticPower") or 0) or None
    except Exception as e:
        print(f"⚠️ [TG профиль] ГМ гильдии {swgoh_guild_id}: {e!r}")
        return None


async def _stat_calc(ctx: Ctx, timeout: float = 240):
    """Калькулятор статов: после рестарта собирается в фоне пару минут (__main__) —
    ждём его, как stat_engine.wait_stat_calc в Discord, а не строим второй параллельно."""
    stat_forecast.start_background_build(ctx.app.comlink)  # продлит устаревший (раз в 12 ч)
    waited, warned = 0.0, False
    while stat_forecast.cached_stat_calc() is None and waited < timeout:
        status = stat_forecast.build_status()
        if not status["loading"] and status["error"]:
            break  # сборка упала (Comlink недоступен) — не ждём зря
        if not warned:
            await ctx.reply("⏳ Бот только что перезапустился — калькулятор статов загружается, ответ будет через минуту-две…")
            warned = True
        await asyncio.sleep(2)
        waited += 2
    calc = stat_forecast.cached_stat_calc()
    if calc is None:
        await ctx.reply("⏳ Калькулятор статов не загрузился — попробуйте через пару минут.")
    return calc


@command("profile", "Профиль игрока или гильдии картинкой", aliases=("профиль",))
async def cmd_profile(ctx: Ctx):
    guild_id = ctx.access["guild_id"]
    if ctx.args.lower() in GUILD_WORDS:
        await _guild_profile(ctx, guild_id)
        return
    ally_code = await _resolve_target(ctx, guild_id, ctx.args, "pf")
    if ally_code:
        await _player_profile(ctx, ally_code)


@callback("pf")
async def cb_profile(ctx: Ctx, ally_code: str):
    code = guild_resolver.normalize_ally_code(ally_code)
    if code is None:
        return
    try:
        await ctx.edit(f"Профиль <code>{code}</code> — готовлю…")
    except Exception:
        pass
    await _player_profile(ctx, code)


async def _player_profile(ctx: Ctx, ally_code: str):
    await ctx.action("upload_photo")
    stat_calc = await _stat_calc(ctx)
    if stat_calc is None:
        return
    try:
        player = await asyncio.to_thread(ctx.app.comlink.get_player, allycode=str(ally_code))
    except Exception as e:
        print(f"⚠️ [TG профиль] get_player {ally_code}: {e!r}")
        player = None
    if not player or not player.get("rosterUnit"):
        await ctx.reply(f"❌ Не удалось получить игрока с кодом <code>{ally_code}</code> из игры.")
        return
    ctx_data = await asyncio.to_thread(pp.load_context)
    meta = pp.player_meta(player)
    guild_gp = await _guild_gp(ctx.app.comlink, player.get("guildId"))

    def work():
        summary = pp.summarize(player["rosterUnit"], player.get("datacron") or [], meta, stat_calc, ctx_data)
        return profile_image.render_player(summary, guild_gp)

    png = await asyncio.to_thread(work)
    await ctx.reply_photo(png, f"profile_{ally_code}.png", caption=f"{esc(player.get('name'))} · <code>{ally_code}</code>")


async def _guild_profile(ctx: Ctx, guild_id: int):
    await ctx.action("upload_photo")
    stat_calc = await _stat_calc(ctx)
    if stat_calc is None:
        return
    cfg = database.get_guild_config(guild_id) or {}
    ctx_data = await asyncio.to_thread(pp.load_context)
    players = await asyncio.to_thread(pp.guild_players_from_cache, guild_id, stat_calc, ctx_data)
    if not players:
        await ctx.reply("❌ Нет данных по ростерам гильдии — дождитесь ежечасного обновления.")
        return
    guild_gp = await _guild_gp(ctx.app.comlink, cfg.get("swgoh_guild_id"))

    def work():
        summary = pp.summarize_guild(cfg.get("name") or "Гильдия", guild_gp, players, ctx_data)
        return profile_image.render_guild(summary)

    png = await asyncio.to_thread(work)
    await ctx.reply_photo(png, f"guild_profile_{guild_id}.png", caption=esc(cfg.get("name") or "Гильдия"))


# ---------------------------------------------------------------- чат гильдии

@command("bind", "Привязать группу к гильдии (офицеры)", tier=TIER_OFFICER, where="group", aliases=("привязать",))
async def cmd_bind(ctx: Ctx):
    guild_id = ctx.access["guild_id"]
    current = store.chat_guild(ctx.chat_id)
    if current and current != guild_id and not ctx.access["is_super_admin"]:
        other = (database.get_guild_config(current) or {}).get("name") or "другой гильдии"
        await ctx.reply(f"Группа уже привязана к гильдии «{esc(other)}» — перепривязать может её офицер (/unbind).")
        return
    store.bind_chat(ctx.chat_id, guild_id, ctx.chat.get("title"), ctx.chat.get("type"), ctx.owner_id)
    name = (database.get_guild_config(guild_id) or {}).get("name") or "?"
    await ctx.reply(f"✅ Группа привязана к гильдии <b>{esc(name)}</b>. Какие оповещения гильдии слать сюда — /notify")


@command("unbind", "Отвязать группу от гильдии (офицеры)", tier=TIER_OFFICER, where="group", menu=False)
async def cmd_unbind(ctx: Ctx):
    current = store.chat_guild(ctx.chat_id)
    if not current:
        await ctx.reply("Группа ни к какой гильдии не привязана.")
        return
    if current != ctx.access["guild_id"] and not ctx.access["is_super_admin"]:
        await ctx.reply("Отвязать группу может только офицер той гильдии, к которой она привязана.")
        return
    store.unbind_chat(ctx.chat_id)
    await ctx.reply("Группа отвязана от гильдии.")
