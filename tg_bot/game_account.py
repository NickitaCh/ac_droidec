"""Personal EA reports. Sessions and account choices belong to the caller."""
import asyncio
import re
from datetime import datetime
from zoneinfo import ZoneInfo

import database
from services import (arena_map_image, farm_inventory, farm_report_image, game_account as ga,
                      game_account_readonly as reader, game_personal_modes, guild_units,
                      guild_units_image, unit_shards)
from tg_bot import command_access, search
from tg_bot.api import TelegramError
from tg_bot.core import Ctx, command, callback, esc, new_session, session_for_callback


_COMMANDS = {"arena": "ga", "conquest": "conquest", "farm": "farm", "ready": "ready"}
_PREFIXES = {"arena": "eag", "conquest": "eac", "farm": "eaf", "ready": "eas"}


def _session(ctx, **data):
    origin = None
    if not ctx.is_private:
        origin = {key: ctx.message[key] for key in ("chat", "message_id", "message_thread_id", "is_topic_message")
                  if key in ctx.message}
    return new_session({"user_id": ctx.user["id"], "origin": origin, **data})


async def _callback_context(ctx, state):
    origin = state.get("origin")
    if origin:
        origin_thread = origin.get("message_thread_id") if origin.get("is_topic_message") else None
        if not ctx.is_private and (ctx.chat_id != origin["chat"]["id"] or
                (ctx.thread_id or 0) != (origin_thread or 0)):
            await ctx.api.answer_callback(ctx.callback_query["id"], "Откройте исходное сообщение отчёта.", alert=True)
            return None
        target = Ctx(ctx.app, origin, ctx.user) if ctx.is_private else ctx
    elif ctx.is_private:
        target = ctx
    else:
        await ctx.api.answer_callback(ctx.callback_query["id"], "Эта кнопка — из личного отчёта.", alert=True)
        return None
    if command_access.denied(target, _COMMANDS[state["spec"]["mode"]]):
        await ctx.api.answer_callback(ctx.callback_query["id"], "Команда отключена в исходной теме.", alert=True)
        return None
    return target


async def _account_error(ctx, error):
    text = (esc(error) if ctx.is_private else
            "Не удалось получить данные своего аккаунта. Проверьте регистрацию и подключение EA в личке бота.")
    await ctx.reply(text)


async def _private_prompt(ctx, text, keyboard):
    if ctx.is_private:
        await ctx.reply(text, keyboard)
        return
    try:
        await ctx.api.send_message(ctx.user["id"], text, keyboard=keyboard)
        notice = "Подключение или подтверждение входа отправлено в личку бота."
    except TelegramError:
        notice = "Откройте личку бота и нажмите Start, затем повторите команду или обновление отчёта."
    await ctx.reply(notice, [[{"text": "Открыть личку бота", "url": f"https://t.me/{ctx.app.username}"}]])


def _scope(ctx):
    args = (ctx.owner_id, ctx.access.get("guild_id"), ctx.app.profile, ctx.access.get("tier"))
    ga.require_account_read(*args)
    return args


async def _account(ctx, raw=None):
    raw = raw.strip() if raw else None
    args = await asyncio.to_thread(_scope, ctx)
    if raw and raw.casefold() in ("альт", "alt"):
        accounts = await asyncio.to_thread(database.get_user_registrations, args[0], guild_id=args[1])
        codes = [str(code) for code, _, main in accounts if not main]
        if len(codes) != 1:
            raise ga.AccountError("Укажите код своего альта после | или выберите аккаунт кнопкой.")
        raw = codes[0]
    code = await asyncio.to_thread(ga.select_account, args[0], args[1], ally_code=raw or None)
    return args, code


async def _link(ctx, code=None, *, alt=False):
    args = await asyncio.to_thread(_scope, ctx)
    path = (await asyncio.to_thread(ga.link_path, args[0], args[1], code)
            if code else f"/game-account/link?alt={int(alt)}")
    # A short-lived, one-use website login works for Telegram-only owners too.
    from services.telegram_web_login import create_url
    url = await asyncio.to_thread(create_url, ctx.user["id"], args[0], args[2], path, ctx.app.site_url)
    await _private_prompt(ctx, "Откройте сайт и подключите аккаунт через почту EA и код из письма. Затем повторите команду отчёта.",
                          [[{"text": "Подключить через EA", "url": url}]])


@command("ea", "Подключить игровой аккаунт через EA на сайте", aliases=("игровой_аккаунт",))
async def cmd_ea(ctx):
    try:
        await _link(ctx, alt=ctx.args.casefold() in ("альт", "alt"))
    except ga.AccountError as error:
        await _account_error(ctx, error)


@command("ga", "Великая арена: рейтинг, раунд, защита", aliases=("arena", "ва"))
async def cmd_arena(ctx):
    await start(ctx, {"mode": "arena"}, ctx.args)


@command("conquest", "Завоевание: энергия, секторы, диски, выносливость", aliases=("завоевание",))
async def cmd_conquest(ctx):
    await start(ctx, {"mode": "conquest"}, ctx.args)


@command("farm", "Фарм персонажа или цели Руководства по своим запасам", aliases=("фарм",))
async def cmd_farm(ctx):
    query, _, account = ctx.args.partition("|")
    words = query.split()
    guide = bool(words and words[0].casefold() in ("guide", "руководство", "цель"))
    if words and words[0].casefold() in ("guide", "руководство", "цель", "unit", "персонаж"):
        words.pop(0)
    level, kind, reward, explicit = 5, "relic", False, False
    clean = []
    for word in words:
        match = re.fullmatch(r"([rgрг])(\d+)", word, re.I)
        if match:
            if explicit:
                await ctx.reply("Укажите одну цель прокачки: G1–G13 или R1–R10.")
                return
            kind = "relic" if match[1].lower() in ("r", "р") else "gear"
            level, explicit = int(match[2]), True
        elif word.casefold() in ("награда", "reward"):
            reward = True
        else:
            clean.append(word)
    if not clean:
        await ctx.reply("<code>/farm персонаж Рей r7</code>\n<code>/farm руководство Лорд Вейдер [награда]</code>\n"
                        "Для альта добавьте <code>| альт</code> или <code>| код</code>. Без уровня — G13 и R5.")
        return
    if not 1 <= level <= (10 if kind == "relic" else 13):
        await ctx.reply("Допустимые уровни: G1–G13 или R1–R10.")
        return
    if guide and explicit:
        await ctx.reply("Для «Руководства» уровни берутся из требований цели; уберите G/R.")
        return
    matches = ([(g["base_id"], g["name"]) for g in await asyncio.to_thread(search.guides, " ".join(clean))]
               if guide else [(b, n) for b, n, ship in await asyncio.to_thread(search.units, " ".join(clean)) if not ship])
    if not matches:
        await ctx.reply("Цель не найдена. Уточните имя персонажа или цели «Руководства».")
        return
    spec = {"mode": "farm", "guide": guide, "kind": kind, "level": level, "reward": reward}
    if len(matches) > 1:
        await _choose(ctx, spec, matches, account)
    else:
        await start(ctx, {**spec, "target": matches[0][0]}, account)


async def _choose(ctx, spec, matches, account):
    token = _session(ctx, spec=spec, matches=matches, account=account)
    await ctx.reply("Выберите цель:", [[{"text": name[:60], "callback_data": f"eat|{token}|{index}"}]
                                        for index, (_, name) in enumerate(matches[:25])])


@callback("eat", command_name="farm")
async def choose_target(ctx, payload):
    token, _, index = payload.partition("|")
    state = await session_for_callback(ctx, token)
    if state is None:
        return
    target = await _callback_context(ctx, state)
    if target is None:
        return
    if not index.isdigit() or int(index) >= len(state["matches"]):
        return
    await start(target, {**state["spec"], "target": state["matches"][int(index)][0]}, state["account"])


async def ready(ctx, guide, account=None):
    await start(ctx, {"mode": "ready", "target": guide}, account)


async def start(ctx, spec, raw=None):
    try:
        _, code = await _account(ctx, raw)
        await run(ctx, spec, code)
    except ga.AccountError as error:
        await _account_error(ctx, error)


def _buttons(token, mode):
    prefix = _PREFIXES[mode]
    return [[{"text": "Обновить", "callback_data": f"{prefix}|{token}|read"},
             {"text": "Другой аккаунт", "callback_data": f"{prefix}|{token}|accounts"}]]


async def run(ctx, spec, code, *, reconnect=False):
    args = await asyncio.to_thread(_scope, ctx)
    code = await asyncio.to_thread(ga.select_account, args[0], args[1], ally_code=code)
    token = _session(ctx, spec=spec, code=code)
    await ctx.action()
    try:
        if reconnect:
            await ga.reconnect(*args, code, optimizer_read=True)
        mode = spec["mode"]
        state = await reader.read(*args, code, member_read=True,
                                  event=mode if mode in ("arena", "conquest") else None,
                                  include_supplies=mode == "farm")
        if mode in ("arena", "conquest"):
            await _personal(ctx, mode, state, code, token)
        elif mode == "farm":
            target = spec["target"]
            reqs = (await asyncio.to_thread(database.get_journey_guide_requirements, target)) if spec["guide"] else None
            if spec["guide"] and not reqs:
                raise ga.AccountError("У цели больше нет требований. Выберите другую цель.")
            report = await asyncio.to_thread(farm_inventory.build_report, state,
                None if spec["guide"] else [(target, spec["kind"], spec["level"])],
                guide_id=target if spec["guide"] else None, requirements=reqs, include_reward=spec["reward"])
            accounts = await asyncio.to_thread(database.get_user_registrations, args[0], guild_id=args[1])
            name = next((n for c, n, _ in accounts if str(c) == code and n), code)
            images = await asyncio.to_thread(farm_report_image.render, report, name, target)
            for index, png in enumerate(images):
                await ctx.reply_photo(png.getvalue() if hasattr(png, "getvalue") else png,
                                      f"farm-{index + 1}.png", keyboard=_buttons(token, mode) if index == len(images) - 1 else None)
        else:
            recipes = await unit_shards.catalog(ctx.app.comlink)
            roster = unit_shards.private_roster(state)
            report = await asyncio.to_thread(guild_units.player_readiness, args[1], spec["target"], roster)
            unit_shards.add_progress(report, state, recipes)
            stamp = datetime.fromtimestamp(state["read_at"], ZoneInfo("Europe/Moscow"))
            png = await asyncio.to_thread(guild_units_image.render_player, report, code,
                                          f"Данные игрового аккаунта: {stamp:%d.%m %H:%M} МСК")
            await ctx.reply_photo(png, "ready-shards.png", keyboard=_buttons(token, mode))
    except ga.SessionExpired:
        await _private_prompt(ctx, ga.LOGIN_WARNING,
                              [[{"text": "Войти и обновить", "callback_data": f"{_PREFIXES[spec['mode']]}|{token}|login"}]])
    except ga.ConnectionRequired:
        await _link(ctx, code)
    except ga.AccountError as error:
        await _account_error(ctx, error)


@callback("ear", command_name="ea")
@callback("eag", command_name="ga")
@callback("eac", command_name="conquest")
@callback("eaf", command_name="farm")
@callback("eas", command_name="ready")
async def refresh(ctx, payload):
    token, _, action = payload.partition("|")
    state = await session_for_callback(ctx, token)
    if state is None:
        return
    target = await _callback_context(ctx, state)
    if target is None:
        return
    try:
        args = await asyncio.to_thread(_scope, ctx)
        if action == "accounts":
            accounts = await asyncio.to_thread(database.get_user_registrations, args[0], guild_id=args[1])
            await ctx.reply("Выберите свой аккаунт:", [[{"text": f"{name or code} · {code}"[:64],
                         "callback_data": f"{_PREFIXES[state['spec']['mode']]}|{token}|a:{code}"}] for code, name, _ in accounts])
            return
        if action == "login":
            if not ctx.is_private:
                await _private_prompt(ctx, ga.LOGIN_WARNING,
                    [[{"text": "Войти и обновить", "callback_data": f"{_PREFIXES[state['spec']['mode']]}|{token}|login"}]])
                return
            # Consume before awaiting the login: double taps never log in twice.
            if state.get("login_used"):
                await ctx.reply("Этот вход уже запущен. Дождитесь результата или вызовите команду заново.")
                return
            state["login_used"] = True
        elif action != "read" and not action.startswith("a:"):
            return
        await run(target, state["spec"], action[2:] if action.startswith("a:") else state["code"], reconnect=action == "login")
    except ga.AccountError as error:
        await _account_error(ctx, error)


async def _personal(ctx, mode, state, code, token):
    heading = "Великая арена" if mode == "arena" else "Завоевание"
    lines = [f"<b>{heading} · {code}</b>"]
    board = None
    if state is None:
        lines.append("Игра не вернула активное событие.")
    elif mode == "arena":
        await asyncio.to_thread(game_personal_modes.name_arena_units, state)
        lines.append("Вы участвуете в событии." if state["joined"] else "Вы не записаны на событие.")
        for label, key in (("Рейтинг", "rating"), ("Лига", "league"), ("Дивизион", "division"), ("Конец матча", "match_end")):
            if state.get(key) is not None:
                lines.append(f"{label}: {esc(state[key])}")
        match = next((m for m in state["matches"] if m["current"]), None)
        if match:
            for label, key in (("Соперник", "opponent"), ("Раунд", "round"), ("Конец раунда", "round_end")):
                if match.get(key) is not None:
                    lines.append(f"{label}: {esc(match[key])}")
            if match["defense"] is not None:
                lines.append("Выставлено в защиту: %s из %s" % match["defense"])
            board = await asyncio.to_thread(arena_map_image.render, match)
            # Keep full teams in the caller's session for owner-only controls.
            from tg_bot.core import get_session
            get_session(token)["match"] = match
    else:
        energy = state["energy"]
        for label, value in (("Сложность", state["difficulty"]), ("Энергия", energy.get("Сейчас")),
                             ("Следующая энергия через", energy.get("Следующая единица через")),
                             ("Диски установлено", state["equipped_disks"]), ("Диски свободно", state["available_disks"])):
            if value is not None:
                lines.append(f"{label}: {esc(value)}")
        lines += [f"Сектор {esc(r['id'])}: {r['completed']} из {r['nodes']}" for r in state["sectors"]]
        if state["stamina"]:
            names = await asyncio.to_thread(database.get_game_unit_names, [r["unit"] for r in state["stamina"]])
            lines.append("<b>Выносливость</b>")
            lines += [f"{esc(names.get(r['unit'], r['unit']))}: {esc(r['value'])}" for r in state["stamina"]]
    buttons = _buttons(token, mode)
    if mode == "arena" and state and any(m["current"] for m in state["matches"]):
        buttons.append([{"text": label, "callback_data": f"ead|{token}|{side}"}
                        for side, label in (("home", "Моя защита"), ("away", "Зоны атаки"))])
    if board:
        await ctx.reply_photo(board.getvalue(), "arena-map.png")
    # Chunk by lines without breaking HTML entities or tags.
    await _send_lines(ctx, lines, buttons)


async def _send_lines(ctx, lines, buttons=None):
    chunk = []
    for line in lines:
        if sum(map(len, chunk)) + len(line) + len(chunk) > 3500:
            await ctx.reply("\n".join(chunk))
            chunk = []
        chunk.append(line)
    await ctx.reply("\n".join(chunk) or "Игра не передала данные.", buttons)


@callback("ead", command_name="ga")
async def arena_details(ctx, payload):
    token, _, side = payload.partition("|")
    state = await session_for_callback(ctx, token)
    if state is None or side not in ("home", "away"):
        return
    target = await _callback_context(ctx, state)
    if target is None:
        return
    try:
        args = await asyncio.to_thread(_scope, ctx)
        await asyncio.to_thread(ga.select_account, args[0], args[1], ally_code=state["code"])
        lines = []
        for index, zone in enumerate(state["match"][side], 1):
            summary = game_personal_modes.arena_zone_summary(zone)
            amount = "—" if summary["remaining"] is None else summary["remaining"]
            capacity = "—" if summary["capacity"] is None else summary["capacity"]
            lines.append(f"<b>Зона {index}</b> · осталось {amount} из {capacity}")
            for team in zone["teams"]:
                status = game_personal_modes.arena_team_status(team)
                icon = {1: "🛡️", 2: "⚔️", 3: "❌"}.get(status["state"], "❔")
                lines.append(f"{icon} {esc(status['label'])} · " + esc(" · ".join(team["names"]) or "Состав скрыт игрой"))
            if not zone["teams"]:
                lines.append("Игра не передала составы.")
        await _send_lines(target, lines)
    except ga.AccountError as error:
        await _account_error(ctx, error)
