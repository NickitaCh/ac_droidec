"""Остальные ходовые команды Discord-бота в Telegram:
/unit (= /юнит), /guildunit (= /юнит_гильдия), /ready (= /готовность), /faction (= /фракция),
/omicrons (= /омикроны отчёт), /tasks (= /задачи отчёт), /warns (= /нарушения, офицеры).

Расчёты и картинки — те же services/* и функции cogs/*, что у Discord-команд.

Где в одном тексте и юнит/цель/фракция, и игрок, игрок пишется после «|»
(«/unit Рей | AC Ricardo, 123456789»), либо кодом союзника / @ником в конце, либо
ответом на сообщение игрока."""

import asyncio
import re
import types
from datetime import datetime

import database
import guild_resolver
from cogs.tasks import TasksCog
from cogs.unit_forecast import UnitForecastCog
from cogs.unit_test import current_levels_text, level_label, projected_player
from services import faction as faction_svc
from services import faction_image, guild_units, guild_units_image, omicron_priority
from services import toon, toon_image, unit_forecast, unit_forecast_image
from tg_bot import markup, search, store
from tg_bot.checks import GUILD_WORDS, REFRESH_WORDS, SELF_ONLY, _feature_off, _self_only_error, _split_flags
from tg_bot.core import TIER_OFFICER, Ctx, callback, command, esc, new_session, session_for_callback
from tg_bot.handlers import _resolve_target, _stat_calc

CHOICES_MAX = 10
_LEVEL_RES = (
    ("relic", re.compile(r"^[rр](\d{1,2})$", re.I)),
    ("gear", re.compile(r"^[gг](\d{1,2})$", re.I)),
    ("stars", re.compile(r"^(\d)(?:\*|★|з|зв)$", re.I)),
)


# ---------------------------------------------------------------- общий разбор

def _split_player(text: str) -> tuple[str, str]:
    """«что | кто» → (что, кто). Без «|» — код союзника или @ник в конце строки."""
    if "|" in text:
        left, _, right = text.partition("|")
        return left.strip(), right.strip()
    words = text.split()
    tail = []
    while words and (guild_resolver.normalize_ally_code(words[-1]) or words[-1].startswith("@")):
        tail.insert(0, words.pop())
    # «123 456 789» — три слова одного кода
    if not tail and len(words) >= 3 and guild_resolver.normalize_ally_code("".join(words[-3:])) and all(w.isdigit() for w in words[-3:]):
        tail, words = ["".join(words[-3:])], words[:-3]
    return " ".join(words), ", ".join(tail)


def _find_player(guild_id: int, text: str) -> tuple[str | None, str | None]:
    """Без кнопок: (код, ошибка). Для списков игроков через запятую."""
    code = guild_resolver.normalize_ally_code(text)
    if code:
        return code, None
    if text.startswith("@"):
        found = store.find_by_username(text)
        reg = database.get_any_main_registration(found["owner_id"]) if found else None
        return (reg[0], None) if reg else (None, f"{esc(text)} не привязал аккаунт к боту")
    roster = [(n, c) for _d, c, n in database.get_all_user_mappings(guild_id) if n]
    needle = text.casefold()
    hits = [r for r in roster if r[0].casefold() == needle] or [r for r in roster if needle in r[0].casefold()]
    if len(hits) == 1:
        return hits[0][1], None
    if not hits:
        return None, f"игрок «{esc(text)}» не найден в составе гильдии"
    return None, f"«{esc(text)}» — несколько игроков: " + esc(", ".join(h[0] for h in hits[:6]))


async def _targets(ctx: Ctx, guild_id: int, who: str, prefix: str, allow_many: bool = False) -> list[str] | None:
    """Коды игроков: «кто» (через запятую, если allow_many) / ответ на сообщение / сам."""
    if not who or not allow_many:
        code = await _resolve_target(ctx, guild_id, who, prefix)
        return [code] if code else None
    codes, errors = [], []
    for part in [p.strip() for p in who.split(",") if p.strip()]:
        code, error = _find_player(guild_id, part)
        if error:
            errors.append(error)
        else:
            codes.append(code)
    if errors:
        await ctx.reply("❌ " + "; ".join(errors))
        return None
    return list(dict.fromkeys(codes))


def _choice_keyboard(prefix: str, token: str, items: list[tuple[str, str]]) -> list:
    return [[{"text": label[:60], "callback_data": f"{prefix}|{token}|{value}"}] for label, value in items[:CHOICES_MAX]]


async def _card_players(ctx: Ctx, codes: list[str], guild_id: int, refresh: bool):
    stand_in = types.SimpleNamespace(bot=types.SimpleNamespace(comlink=ctx.app.comlink))
    return await UnitForecastCog._card_players(stand_in, codes, guild_id, refresh)


def _note(text: str) -> str:
    """Подписи из toon_image в Discord-разметке («-# …», **…**) → Telegram."""
    return markup.md_to_html(text)


# ======================================================================== /unit

def _parse_levels(words: list[str]) -> tuple[list[str], dict]:
    levels, rest = {}, []
    for w in words:
        for key, rx in _LEVEL_RES:
            m = rx.match(w)
            if m:
                levels[key] = int(m.group(1))
                break
        else:
            rest.append(w)
    return rest, levels


def _unit_matches(query: str) -> list[tuple[str, str]]:
    return [(b, f"{name}{' 🚀' if ship else ''}") for b, name, ship in search.units(query)]


def find_button(text: str, query: str) -> list:
    """Кнопка, открывающая инлайн-подсказки бота прямо в поле ввода (tg_bot/inline.py) —
    в Telegram это замена автодополнения параметров Discord."""
    return [[{"text": text, "switch_inline_query_current_chat": query}]]


@command("unit", "Персонаж: статы, модули, зеты; прогноз и сравнение (как /юнит)", aliases=("юнит",))
async def cmd_unit(ctx: Ctx):
    guild_id = ctx.access["guild_id"]
    what, who = _split_player(ctx.args)
    words, (refresh,) = _split_flags(what, REFRESH_WORDS)
    rest, levels = _parse_levels(words.split())
    query = " ".join(rest)
    if not query:
        await ctx.reply(
            "Укажите персонажа: <code>/unit Рей</code> — или нажмите «Найти» и начните вводить имя, бот подскажет.\n"
            "Прогноз: <code>/unit Рей r7</code> (r — реликвия, g — гир, 7* — звёзды)\n"
            "Другой игрок: <code>/unit Рей | Ник</code>, код союзника или ответ на сообщение; "
            "сравнение — несколько через запятую: <code>/unit Рей | Ник, 123456789</code>",
            keyboard=find_button("🔎 Найти персонажа", "юнит "),
        )
        return
    state = {"user_id": ctx.user["id"], "guild_id": guild_id, "who": who, "levels": levels, "refresh": refresh}
    matches = _unit_matches(query)
    if not matches:
        await ctx.reply(f"Персонаж «{esc(query)}» не найден — попробуйте подсказки:",
                        keyboard=find_button("🔎 Найти персонажа", f"юнит {query}"))
        return
    if len(matches) > 1:
        token = new_session(state)
        items = [(name, b) for b, name in matches]
        more = f"\n…и ещё {len(matches) - CHOICES_MAX} — уточните." if len(matches) > CHOICES_MAX else ""
        await ctx.reply(f"Какой персонаж?{more}", keyboard=_choice_keyboard("un", token, items))
        return
    await _unit_run(ctx, state, matches[0][0])


@callback("un", command_name="unit")
async def cb_unit(ctx: Ctx, payload: str):
    token, _, base_id = payload.partition("|")
    state = await session_for_callback(ctx, token)
    if state is None:
        return
    await ctx.edit(f"Персонаж: <b>{esc(database.get_game_unit_name(base_id) or base_id)}</b>")
    await _unit_run(ctx, state, base_id)


@callback("uw", command_name="unit")
async def cb_unit_who(ctx: Ctx, payload: str):
    token, _, code = payload.partition("|")
    state = await session_for_callback(ctx, token)
    if state is None:
        return
    await ctx.edit("Игрок выбран — собираю карточку…")
    await _unit_card(ctx, state, state["base_id"], [code])


async def _unit_run(ctx: Ctx, state: dict, base_id: str):
    lv = state["levels"]
    is_ship = database.get_unit_types([base_id]).get(base_id) == "ship"
    error = unit_forecast.validate(lv.get("stars"), None if is_ship else lv.get("gear"), None if is_ship else lv.get("relic"))
    if error:
        await ctx.reply(f"❌ {esc(error)}")
        return
    state["base_id"] = base_id
    token = new_session(state)
    codes = await _targets(ctx, state["guild_id"], state["who"], f"uw|{token}", allow_many=True)
    if not codes:
        return
    if is_ship and len(codes) > 1:
        await ctx.reply("❌ Корабль — только для одного игрока: у кораблей нет модулей.")
        return
    await _unit_card(ctx, state, base_id, codes)


async def _unit_card(ctx: Ctx, state: dict, base_id: str, codes: list[str]):
    await ctx.action("upload_photo")
    stat_calc = await _stat_calc(ctx)
    if stat_calc is None:
        return
    lv = state["levels"]
    stars, gear, relic = lv.get("stars"), lv.get("gear"), lv.get("relic")
    if database.get_unit_types([base_id]).get(base_id) == "ship":
        await _ship_forecast(ctx, stat_calc, codes[0], base_id, stars)
        return
    players, ages = await _card_players(ctx, codes, state["guild_id"], state["refresh"])
    for code, player in zip(codes, players):
        if not player or not player.get("rosterUnit"):
            await ctx.reply(f"❌ Не удалось получить игрока <code>{code}</code> из игры.")
            return
    note_lines = []
    if stars is None and (gear is not None or relic is not None):
        stars = 7  # как в /юнит: реликвии и высокий гир — только на 7★
    if any(v is not None for v in (stars, gear, relic)):
        suffix = level_label(stars, gear, relic)
        note_lines.append(f"🔮 <b>Прогноз на {esc(suffix)}</b> — уровень пересчитан, модули текущие. "
                          f"Сейчас: {esc(current_levels_text(players, [base_id]))}")
        suffix = f"прогноз {suffix}"
        if len(players) == 1:
            players = [{**players[0], "name": f"{players[0].get('name') or '?'} (сейчас)"},
                       projected_player(players[0], [base_id], stars, gear, relic, suffix)]
        else:
            players = [projected_player(p, [base_id], stars, gear, relic, suffix) for p in players]
    oldest = max((a for a in ages if a is not None), default=None)
    note_lines.append(_note(toon_image.freshness_note(oldest)).replace("«обновить»", "добавьте «обновить»"))
    try:
        png = await asyncio.to_thread(lambda: toon_image.render(toon.build(stat_calc, players, [base_id])))
    except Exception as e:
        print(f"⚠️ [TG юнит] {base_id} {codes}: {e!r}")
        await ctx.reply("❌ Не удалось собрать карточку.")
        return
    await ctx.reply_photo(png, "toon.png", caption="\n".join(note_lines),
                          keyboard=_unit_compare_keyboard(ctx, state, base_id, codes), replace=True)


UNIT_COMPARE_MAX = 4  # как панель сравнения /юнит в Discord


def _own_ally_code(ctx: Ctx) -> str | None:
    rows = database.get_all_registrations_for_discord_id(ctx.owner_id)
    main = [r for r in rows if r[0] == ctx.access["guild_id"]] or rows
    return main[0][1] if main else None


def _unit_compare_keyboard(ctx: Ctx, state: dict, base_id: str, codes: list[str]):
    """Кнопки под карточкой — добавить к сравнению игрока гильдии или себя, не набирая
    «/unit Рей | Ник, код» (NicolozZ 2026-10-24: «подсказку не нашёл»)."""
    if len(codes) >= UNIT_COMPARE_MAX or database.get_unit_types([base_id]).get(base_id) == "ship":
        return
    state = {**state, "base_id": base_id, "codes": list(codes)}
    token = new_session(state)
    row = [{"text": "➕ Игрок гильдии", "callback_data": f"ua|{token}|g"}]
    own = _own_ally_code(ctx)
    if own and own not in codes:
        row.append({"text": "➕ Я", "callback_data": f"ua|{token}|{own}"})
    return [row]


@callback("ua", command_name="unit")
async def cb_unit_add(ctx: Ctx, payload: str):
    token, _, target = payload.partition("|")
    state = await session_for_callback(ctx, token)
    if state is None:
        return
    if target == "g":
        roster = sorted(((name, ac) for _d, ac, name in database.get_all_user_mappings(state["guild_id"])
                         if ac not in state["codes"]), key=lambda x: (x[0] or "").casefold())
        buttons = [{"text": (name or ac)[:30], "callback_data": f"ua|{token}|{ac}"} for name, ac in roster[:99]]
        await ctx.edit("Кого добавить к сравнению?", keyboard=[buttons[i:i + 3] for i in range(0, len(buttons), 3)])
        return
    if not target.isdigit() or target in state["codes"]:
        return
    await ctx.edit("Добавляю к сравнению…")
    await _unit_card(ctx, state, state["base_id"], state["codes"] + [target])


async def _ship_forecast(ctx: Ctx, stat_calc, ally_code: str, base_id: str, stars):
    try:
        player = await asyncio.to_thread(ctx.app.comlink.get_player, allycode=str(ally_code))
    except Exception as e:
        print(f"⚠️ [TG юнит] get_player {ally_code}: {e!r}")
        player = None
    if not player or not player.get("rosterUnit"):
        await ctx.reply(f"❌ Не удалось получить игрока <code>{ally_code}</code> из игры.")
        return
    unit = next((u for u in player["rosterUnit"] if u.get("definitionId", "").split(":")[0] == base_id), None)
    name = database.get_game_unit_name(base_id) or base_id
    if unit is None:
        await ctx.reply(f"❌ У <b>{esc(player.get('name'))}</b> нет корабля <b>{esc(name)}</b>.")
        return
    updated = datetime.now().strftime("%d.%m.%Y %H:%M")

    def work():
        crew = unit_forecast.ship_crew(stat_calc, player["rosterUnit"], base_id)
        return unit_forecast_image.render(name, base_id, player.get("name") or ally_code, updated,
                                          unit_forecast.forecast_ship(stat_calc, unit, crew, stars))

    await ctx.reply_photo(await asyncio.to_thread(work), f"unit_{base_id}.png")


# ======================================================================== /guildunit

@command("guildunit", "У кого в гильдии есть юнит и на какой прокачке", aliases=("юнит_гильдия",))
async def cmd_guildunit(ctx: Ctx):
    query = ctx.args.strip()
    if not query:
        await ctx.reply("Укажите юнит: <code>/guildunit Рей</code>", keyboard=find_button("🔎 Найти юнит", "гильдия "))
        return
    matches = _unit_matches(query)
    if not matches:
        await ctx.reply(f"Юнит «{esc(query)}» не найден — попробуйте подсказки:",
                        keyboard=find_button("🔎 Найти юнит", f"гильдия {query}"))
        return
    state = {"user_id": ctx.user["id"], "guild_id": ctx.access["guild_id"]}
    if len(matches) > 1:
        token = new_session(state)
        await ctx.reply("Какой юнит?", keyboard=_choice_keyboard("gu", token, [(n, b) for b, n in matches]))
        return
    await _guildunit_run(ctx, state, matches[0][0])


@callback("gu", command_name="guildunit")
async def cb_guildunit(ctx: Ctx, payload: str):
    token, _, base_id = payload.partition("|")
    state = await session_for_callback(ctx, token)
    if state is None:
        return
    await ctx.edit(f"Юнит: <b>{esc(database.get_game_unit_name(base_id) or base_id)}</b>")
    await _guildunit_run(ctx, state, base_id)


async def _guildunit_run(ctx: Ctx, state: dict, base_id: str):
    await ctx.action()
    stat_calc = await _stat_calc(ctx)
    if stat_calc is None:
        return
    r = await asyncio.to_thread(guild_units.unit_report, state["guild_id"], base_id, stat_calc)
    if not r["members"]:
        await ctx.reply("❌ Нет данных по составу гильдии — дождитесь ежечасного обновления.")
        return
    lines = [f"<b>{esc(r['name'])}</b> — есть у {len(r['owners'])} из {r['members']}"]
    if not r["is_ship"]:
        lines.append("<i>з/о — зеты/омикроны, А — абсолютная способность</i>")
    groups: dict[int, list] = {}
    for o in r["owners"]:
        groups.setdefault(o["stars"], []).append(o)
    for stars in sorted(groups, reverse=True):
        rows = []
        for o in groups[stars]:
            level = guild_units.level_text(o, r["is_ship"])
            extra = "" if r["is_ship"] else f" {o['zetas']}/{o['omicrons']}" + ("·А" if o["ultimate"] else "")
            gp = f"{o['gp'] / 1000:.0f}K" if o["gp"] else "—"
            rows.append(f"{gp:>5} {level:<5}{extra:<6} {o['name'][:16]}")
        lines.append(f"\n<b>{stars}★ ({len(rows)})</b>\n<pre>" + esc("\n".join(rows)) + "</pre>")
    if r["missing"]:
        lines.append(f"\n<b>Нет у ({len(r['missing'])})</b>: " + esc(", ".join(r["missing"])))
    for chunk in _html_chunks(lines):
        await ctx.reply(chunk)


def _html_chunks(blocks: list[str], limit: int = 3900) -> list[str]:
    """Куски по целым блокам (блок с <pre> не режется)."""
    out, cur = [], ""
    for b in blocks:
        if cur and len(cur) + len(b) + 1 > limit:
            out.append(cur)
            cur = ""
        cur = f"{cur}\n{b}" if cur else b
    if cur:
        out.append(cur)
    return out


# ======================================================================== /ready



@command("ready", "Готовность к цели «Руководства» картинкой (как /готовность)", aliases=("готовность",))
async def cmd_ready(ctx: Ctx):
    guild_id = ctx.access["guild_id"]
    what, who = _split_player(ctx.args)
    what, (guild_mode,) = _split_flags(what, GUILD_WORDS)
    if not what:
        await ctx.reply("Укажите цель: <code>/ready Лорд Вейдер</code>; гильдия — <code>/ready Лорд Вейдер гильдия</code>; "
                        "другой игрок — <code>/ready Лорд Вейдер | Ник</code>",
                        keyboard=find_button("🔎 Найти цель", "готовность "))
        return
    matches = search.guides(what, guild_id)
    if not matches:
        await ctx.reply(f"Цель «{esc(what)}» не найдена в «Руководстве» — попробуйте подсказки:",
                        keyboard=find_button("🔎 Найти цель", f"готовность {what}"))
        return
    state = {"user_id": ctx.user["id"], "guild_id": guild_id, "who": who, "guild_mode": guild_mode}
    if len(matches) > 1:
        token = new_session(state)
        items = [(f"{g['name']} · {g['kind_label']}", g["base_id"]) for g in matches]
        await ctx.reply("Какая цель?", keyboard=_choice_keyboard("rd", token, items))
        return
    await _ready_run(ctx, state, matches[0]["base_id"])


@callback("rd", command_name="ready")
async def cb_ready(ctx: Ctx, payload: str):
    token, _, guide = payload.partition("|")
    state = await session_for_callback(ctx, token)
    if state is None:
        return
    await ctx.edit(f"Цель: <b>{esc(database.get_game_unit_name(guide) or guide)}</b>")
    await _ready_run(ctx, state, guide)


@callback("rw", command_name="ready")
async def cb_ready_who(ctx: Ctx, payload: str):
    token, _, code = payload.partition("|")
    state = await session_for_callback(ctx, token)
    if state is None:
        return
    await ctx.edit("Игрок выбран — собираю…")
    await _ready_player(ctx, state, code)


async def _ready_run(ctx: Ctx, state: dict, guide: str):
    state["guide"] = guide
    guild_id = state["guild_id"]
    if state["guild_mode"]:
        await ctx.action("upload_photo")
        r = await asyncio.to_thread(guild_units.readiness, guild_id, guide)
        if not r["players"]:
            await ctx.reply("❌ Нет данных по ростерам гильдии — дождитесь ежечасного обновления.")
            return
        cfg = database.get_guild_config(guild_id) or {}
        png = await asyncio.to_thread(guild_units_image.render, r, cfg.get("name") or "Гильдия", "данные ежечасного обновления")
        ready = sum(1 for p in r["players"] if p["ready"])
        await ctx.reply_photo(png, f"ready_{guide}.png", caption=f"<b>{esc(r['guide']['name'])}</b> — готовы {ready} из {len(r['players'])}")
        return
    token = new_session(state)
    codes = await _targets(ctx, guild_id, state["who"], f"rw|{token}")
    if codes:
        await _ready_player(ctx, state, codes[0])


async def _player_roster(ctx: Ctx, guild_id: int, code: str):
    players, ages = await _card_players(ctx, [code], guild_id, False)
    player = players[0]
    if not player or not player.get("rosterUnit"):
        await ctx.reply(f"❌ Не удалось получить игрока <code>{code}</code> из игры.")
        return None
    roster = {guild_units.unit_base_id(u): u for u in player["rosterUnit"]}
    note = (f"данные ежечасного обновления, {max(1, int(ages[0]) // 60)} мин назад" if ages[0] is not None
            else "данные из игры на момент запроса")
    return player, roster, note


async def _ready_player(ctx: Ctx, state: dict, code: str):
    await ctx.action("upload_photo")
    got = await _player_roster(ctx, state["guild_id"], code)
    if got is None:
        return
    player, roster, note = got
    r = await asyncio.to_thread(guild_units.player_readiness, state["guild_id"], state["guide"], roster)
    name = player.get("name") or code
    png = await asyncio.to_thread(guild_units_image.render_player, r, name, note)
    met = sum(1 for u in r["units"] if u["status"] == "met")
    await ctx.reply_photo(png, f"ready_{code}.png", caption=f"<b>{esc(name)}</b> — {esc(r['guide']['name'])}: выполнено {met} из {len(r['units'])}")


# ======================================================================== /faction

@command("faction", "Все персонажи фракции у игрока картинкой (как /фракция)", aliases=("фракция",))
async def cmd_faction(ctx: Ctx):
    what, who = _split_player(ctx.args)
    if not what:
        await ctx.reply("Укажите фракцию: <code>/faction Империя</code>; другой игрок — <code>/faction Империя | Ник</code>",
                        keyboard=find_button("🔎 Найти фракцию", "фракция "))
        return
    state = {"user_id": ctx.user["id"], "guild_id": ctx.access["guild_id"], "who": who}
    hits = search.factions(what, limit=CHOICES_MAX + 1)
    if len(hits) != 1:
        if not hits:
            await ctx.reply(f"Фракция «{esc(what)}» не найдена — попробуйте подсказки:",
                            keyboard=find_button("🔎 Найти фракцию", f"фракция {what}"))
            return
        token = new_session(state)
        await ctx.reply("Какая фракция?", keyboard=_choice_keyboard("fc", token, [(f"{n} ({c})", cid) for cid, n, c in hits]))
        return
    await _faction_run(ctx, state, hits[0][0])


@callback("fc", command_name="faction")
async def cb_faction(ctx: Ctx, payload: str):
    token, _, cid = payload.partition("|")
    state = await session_for_callback(ctx, token)
    if state is None:
        return
    await ctx.edit("Фракция выбрана — собираю…")
    await _faction_run(ctx, state, cid)


@callback("fw", command_name="faction")
async def cb_faction_who(ctx: Ctx, payload: str):
    token, _, code = payload.partition("|")
    state = await session_for_callback(ctx, token)
    if state is None:
        return
    await ctx.edit("Игрок выбран — собираю…")
    await _faction_player(ctx, state, code)


async def _faction_run(ctx: Ctx, state: dict, faction_id: str):
    state["faction"] = faction_id
    token = new_session(state)
    codes = await _targets(ctx, state["guild_id"], state["who"], f"fw|{token}")
    if codes:
        await _faction_player(ctx, state, codes[0])


async def _faction_player(ctx: Ctx, state: dict, code: str):
    await ctx.action("upload_photo")
    got = await _player_roster(ctx, state["guild_id"], code)
    if got is None:
        return
    player, roster, note = got
    r = await asyncio.to_thread(faction_svc.build, state["faction"], roster)
    name = player.get("name") or code
    png = await asyncio.to_thread(faction_image.render, r, name, note)
    await ctx.reply_photo(png, f"faction_{code}.png", caption=f"<b>{esc(name)}</b> — {esc(r['name'])}: открыто {r['owned']} из {r['total']}")


# ======================================================================== /omicrons

@command("omicrons", "Какие приоритетные для ВГ омикроны вы готовы поставить", aliases=("омикроны",))
async def cmd_omicrons(ctx: Ctx):
    guild_id = ctx.access["guild_id"]
    off = _feature_off(guild_id, "omicron")
    if off:
        await ctx.reply(off)
        return
    state = {"user_id": ctx.user["id"], "guild_id": guild_id}
    token = new_session(state)
    code = await _resolve_target(ctx, guild_id, ctx.args, f"om|{token}")
    if code:
        await _omicrons_player(ctx, guild_id, code)


@callback("om", command_name="omicrons")
async def cb_omicrons(ctx: Ctx, payload: str):
    token, _, code = payload.partition("|")
    state = await session_for_callback(ctx, token)
    if state is None:
        return
    await ctx.edit("Игрок выбран.")
    await _omicrons_player(ctx, state["guild_id"], code)


async def _omicrons_player(ctx: Ctx, guild_id: int, code: str):
    denial = _self_only_error(ctx, code)
    if denial:
        await ctx.reply(denial)
        return
    name = database.get_player_names(guild_id).get(code) or code
    missing = await asyncio.to_thread(omicron_priority.missing_omicrons_for_player, code, guild_id)
    if not missing:
        await ctx.reply(f"✅ У <b>{esc(name)}</b> нет готовых, но не поставленных приоритетных омикронов "
                        "(или приоритеты ВГ не настроены на сайте).")
        return
    lines = [f"🧬 <b>{esc(name)}</b> — что можно поставить"]
    for item in missing:
        mode = f" ({esc(item['omicron_mode'])})" if item.get("omicron_mode") else ""
        lines.append(f"• <b>{esc(item['unit_name'])}</b> — {esc(item['skill_name'])}{mode}")
    for chunk in _html_chunks(lines):
        await ctx.reply(chunk)


# ======================================================================== /tasks

@command("tasks", "Прогресс по задачам на прокачку (как /задачи отчёт)", aliases=("задачи",))
async def cmd_tasks(ctx: Ctx):
    guild_id = ctx.access["guild_id"]
    off = _feature_off(guild_id, "tasks")
    if off:
        await ctx.reply(off)
        return
    text, (guild_mode,) = _split_flags(ctx.args, GUILD_WORDS)
    if guild_mode:
        if ctx.access["tier"] != "officer":
            await ctx.reply(SELF_ONLY)
            return
        await _tasks_guild(ctx, guild_id)
        return
    state = {"user_id": ctx.user["id"], "guild_id": guild_id}
    token = new_session(state)
    code = await _resolve_target(ctx, guild_id, text, f"tk|{token}")
    if code:
        await _tasks_player(ctx, guild_id, code)


@callback("tk", command_name="tasks")
async def cb_tasks(ctx: Ctx, payload: str):
    token, _, code = payload.partition("|")
    state = await session_for_callback(ctx, token)
    if state is None:
        return
    await ctx.edit("Игрок выбран.")
    await _tasks_player(ctx, state["guild_id"], code)


async def _tasks_guild(ctx: Ctx, guild_id: int):
    departed = set(database.get_departed_players(guild_id))
    rows = [r for r in database.get_all_tasks(guild_id)
            if r[6] in ("ACTIVE", "FAILED") and not database.is_task_archived(r[12], r[6], r[13])
            and r[1] not in departed and not r[14]]
    if not rows:
        await ctx.reply("🎉 Активных или просроченных задач нет.")
        return
    names = database.get_player_names(guild_id)
    by_player: dict[str, list] = {}
    for r in rows:
        by_player.setdefault(r[1], []).append(r[6])
    lines = ["📋 <b>Задачи — гильдия</b>"]
    for code, statuses in sorted(by_player.items(), key=lambda kv: -len(kv[1])):
        parts = []
        if statuses.count("ACTIVE"):
            parts.append(f"в работе: {statuses.count('ACTIVE')}")
        if statuses.count("FAILED"):
            parts.append(f"просрочено: {statuses.count('FAILED')}")
        lines.append(f"• {esc(names.get(code, code))} — {', '.join(parts)}")
    for chunk in _html_chunks(lines):
        await ctx.reply(chunk)


async def _tasks_player(ctx: Ctx, guild_id: int, code: str):
    denial = _self_only_error(ctx, code)
    if denial:
        await ctx.reply(denial)
        return
    name = database.get_player_names(guild_id).get(code) or code
    rows = [r for r in database.get_tasks_for_ally(code, guild_id=guild_id) if not database.is_task_archived(r[11], r[6], r[12])]
    if not rows:
        await ctx.reply(f"У <b>{esc(name)}</b> нет текущих задач.")
        return
    lines = [f"📋 <b>Задачи — {esc(name)}</b>"]
    for (_id, _code, base_id, target_type, target_value, deadline, status, initial, current, in_progress,
         _created_by, _resolved, _archived, is_personal, *_rest) in rows:
        unit = database.get_game_unit_name(base_id) or base_id
        target = TasksCog._format_target_label(None, target_type, target_value)
        emoji, status_text = TasksCog._status_label(status, in_progress)
        progress = TasksCog._progress_label(None, initial, current)
        tail = (f"до {deadline}" if deadline else "без срока") + (" · личная" if is_personal else "")
        lines.append(f"{emoji} <b>{esc(unit)}</b> — {esc(target)} ({status_text}) · {esc(progress)} · {esc(tail)}")
    for chunk in _html_chunks(lines):
        await ctx.reply(chunk)


# ======================================================================== /warns (офицеры)

WARN_DAYS = 90
WARN_LIMIT = 3  # = main.N_LIMIT (main.py не импортировать — он запускает Discord-бота)


@command("warns", "Нарушения: таблица гильдии, досье игрока, добавить/снять (офицеры)", tier=TIER_OFFICER, aliases=("нарушения",))
async def cmd_warns(ctx: Ctx):
    guild_id = ctx.access["guild_id"]
    off = _feature_off(guild_id, "violations")
    if off:
        await ctx.reply(off)
        return
    if not ctx.args and ctx.reply_user is None:
        await ctx.reply(_guild_warns_table(guild_id))
        return
    state = {"user_id": ctx.user["id"], "guild_id": guild_id}
    token = new_session(state)
    code = await _resolve_target(ctx, guild_id, ctx.args, f"wp|{token}")
    if code:
        state["code"] = code
        await ctx.reply(*_dossier_view(state, token))


def _guild_warns_table(guild_id: int) -> str:
    from cogs.violations import ViolationsCog
    cache = {name: code for _d, code, name in database.get_all_user_mappings(guild_id) if name}
    stand_in = types.SimpleNamespace(bot=types.SimpleNamespace(n_limit=WARN_LIMIT))
    table = ViolationsCog.guild_table(stand_in, guild_id, cache)
    if not table:
        return "😇 Нарушений в гильдии за последние 3 месяца нет."
    return f"<pre>{esc(table)}</pre>\nДосье игрока: <code>/warns Ник</code>"


def _dossier_view(state: dict, token: str) -> tuple[str, list]:
    from cogs.violations import ViolationsCog
    gid, code = state["guild_id"], state["code"]
    name = database.get_player_names(gid).get(code) or code
    history, total, recent, _rows = ViolationsCog.dossier(None, gid, code, name)
    mark = " 🚨" if recent >= WARN_LIMIT else ""
    text = f"📊 <b>Досье: {esc(name)}</b> — всего {total}, за {WARN_DAYS} дней {recent}{mark}\n" + (markup.md_to_html(history) or "😇 Нарушений нет.")
    keyboard = [[{"text": "➕ Зафиксировать", "callback_data": f"wa|{token}|m"},
                 {"text": "➖ Снять", "callback_data": f"wr|{token}|l"}]]
    return text[:4000], keyboard


@callback("wp", tier=TIER_OFFICER, command_name="warns")
async def cb_warns_player(ctx: Ctx, payload: str):
    token, _, code = payload.partition("|")
    state = await session_for_callback(ctx, token)
    if state is None:
        return
    state["code"] = code
    text, keyboard = _dossier_view(state, token)
    await ctx.edit(text, keyboard=keyboard)


@callback("wa", tier=TIER_OFFICER, command_name="warns")
async def cb_warns_add(ctx: Ctx, payload: str):
    """Добавление: режим → нарушение → запись сегодняшним числом."""
    from cogs.violations import WARNS_STRUCTURE
    token, _, step = payload.partition("|")
    state = await session_for_callback(ctx, token)
    if state is None:
        return
    modes = list(WARNS_STRUCTURE)
    if step == "m":
        kb = [[{"text": m, "callback_data": f"wa|{token}|M{i}"} for i, m in enumerate(modes)],
              [{"text": "◀ Досье", "callback_data": f"wp|{token}|{state['code']}"}]]
        await ctx.edit("Режим нарушения:", keyboard=kb)
        return
    if step.startswith("M") and step[1:].isdigit():
        state["mode"] = modes[int(step[1:])]
        options = WARNS_STRUCTURE[state["mode"]]
        kb = [[{"text": v, "callback_data": f"wa|{token}|V{i}"}] for i, v in enumerate(options)]
        kb.append([{"text": "◀ Режим", "callback_data": f"wa|{token}|m"}])
        await ctx.edit(f"<b>{esc(state['mode'])}</b> — какое нарушение?", keyboard=kb)
        return
    if step.startswith("V") and step[1:].isdigit() and state.get("mode"):
        violation = WARNS_STRUCTURE[state["mode"]][int(step[1:])]
        date = datetime.now().strftime("%d.%m.%Y")
        database.add_warn(state["code"], state["mode"], violation, date, None, guild_id=state["guild_id"])
        name = database.get_player_names(state["guild_id"]).get(state["code"]) or state["code"]
        text, keyboard = _dossier_view(state, token)
        await ctx.edit(text, keyboard=keyboard)
        await ctx.reply(f"✅ Нарушение зафиксировано: <b>{esc(name)}</b> — {esc(state['mode'])}: {esc(violation)} ({date})")


@callback("wr", tier=TIER_OFFICER, command_name="warns")
async def cb_warns_remove(ctx: Ctx, payload: str):
    token, _, step = payload.partition("|")
    state = await session_for_callback(ctx, token)
    if state is None:
        return
    warns = database.get_player_warns(state["code"], guild_id=state["guild_id"])
    if step == "l":
        if not warns:
            await ctx.api.answer_callback(ctx.callback_query["id"], "Нарушений нет.", alert=True)
            return
        kb = [[{"text": f"{d} — {cat}: {sub}"[:60], "callback_data": f"wr|{token}|{i}"}] for i, (_id, cat, sub, d, _c) in enumerate(warns[:20])]
        kb.append([{"text": "◀ Досье", "callback_data": f"wp|{token}|{state['code']}"}])
        await ctx.edit("Какое нарушение снять?", keyboard=kb)
        return
    if step.isdigit() and int(step) < len(warns):
        warn_id = warns[int(step)][0]
        database.remove_warn_by_id(warn_id, guild_id=state["guild_id"])
        text, keyboard = _dossier_view(state, token)
        await ctx.edit(text, keyboard=keyboard)

