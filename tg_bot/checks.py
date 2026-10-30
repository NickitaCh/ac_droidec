"""«Проверочные» команды — /stats (= /статы) и /dc (= /дк_требования проверить).
Расчёт — те же функции, что у Discord-команд (cogs/stat_requirements.py через
services/stat_forecast.py, cogs/datacron_requirements.py::build_player_check), здесь
только разбор аргументов, кнопки и перевод разметки (tg_bot/markup.py).

Участник проверяет только себя (и свои альты), чужих игроков и гильдию — офицеры,
как в Discord (guild_resolver.self_only_error)."""

import asyncio

import database
from cogs.datacron_requirements import build_player_check
from cogs.stat_requirements import SCENARIO_FULL, SCENARIO_RAW, SCENARIO_UP
from services import datacron_catalog, feature_flags, stat_forecast
from tg_bot import markup
from tg_bot.api import TelegramError
from tg_bot.core import Ctx, callback, command, esc, new_session, session_for_callback
from tg_bot.handlers import _resolve_target, _stat_calc

GUILD_WORDS = {"гильдия", "guild"}
FAILED_WORDS = {"кресты", "крест", "x", "❌"}
REFRESH_WORDS = {"обновить", "refresh"}
UP_WORDS = {"вверх", "up"}
FULL_WORDS = {"подгон", "full"}
SQUAD_WORDS = {"отряд", "squad"}
# Сценарии — как в Discord /статы (SCENARIO_CHOICES), короткие подписи для кнопок.
SCENARIO_BUTTONS = ((SCENARIO_RAW, "Как есть"), (SCENARIO_UP, "Релик вверх"), (SCENARIO_FULL, "Полный подгон"))
SCENARIO_TITLES = {SCENARIO_RAW: "как есть", SCENARIO_UP: "релик вверх", SCENARIO_FULL: "полный подгон релика"}
SELF_ONLY = (
    "🔒 Участники проверяют только себя (и свои альты). Чужих игроков и всю гильдию проверяют офицеры.\n"
    "Чтобы проверить себя — не указывайте игрока."
)


def _self_only_error(ctx: Ctx, ally_code: str) -> str | None:
    if ctx.access["tier"] == "officer":
        return None
    own = {row[1] for row in database.get_all_registrations_for_discord_id(ctx.owner_id)}
    return None if ally_code in own else SELF_ONLY


def _split_flags(text: str, *flag_sets) -> tuple[str, list[bool]]:
    words = text.split()
    found = [False] * len(flag_sets)
    rest = []
    for word in words:
        for i, flags in enumerate(flag_sets):
            if word.casefold() in flags:
                found[i] = True
                break
        else:
            rest.append(word)
    return " ".join(rest), found


def _player_name(guild_id: int, ally_code: str) -> str:
    return database.get_player_names(guild_id).get(ally_code) or ally_code


def _feature_off(guild_id: int, key: str) -> str | None:
    if feature_flags.is_enabled(guild_id, key):
        return None
    return f"❌ Функция «{esc(feature_flags.label(key))}» отключена для вашей гильдии."


# ======================================================================== /stats

def _match_plate(text: str, plates: list[str]) -> tuple[str | None, str, list[str]]:
    """(плейт, остаток текста, кандидаты). Плейт — самое длинное имя, с которого
    начинается текст; иначе — единственный плейт, в имени которого есть весь текст."""
    folded = text.casefold()
    best = None
    for plate in plates:
        p = plate.casefold()
        if folded == p or folded.startswith(p + " "):
            if best is None or len(plate) > len(best):
                best = plate
    if best:
        return best, text[len(best):].strip(), []
    if not text:
        return None, "", plates
    first = text.split()[0].casefold()
    candidates = [p for p in plates if folded in p.casefold()] or [p for p in plates if first in p.casefold()]
    if len(candidates) == 1 and folded in candidates[0].casefold():
        return candidates[0], "", []
    return None, text, candidates or plates


def _plate_keyboard(token: str, plates: list[str], names: list[str]) -> list:
    # Telegram держит до 100 кнопок на сообщение; у AC ~50 плейтов, поэтому по три в ряд.
    buttons = [{"text": name, "callback_data": f"sp|{token}|{plates.index(name)}"} for name in names[:99]]
    return [buttons[i:i + 3] for i in range(0, len(buttons), 3)]


@command("stats", "Статы по плейту: свои (чужие и гильдия — офицеры)", aliases=("статы",))
async def cmd_stats(ctx: Ctx):
    guild_id = ctx.access["guild_id"]
    off = _feature_off(guild_id, "stat_requirements")
    if off:
        await ctx.reply(off)
        return
    text, (guild_mode, only_failed, refresh, up, full, squad_only) = _split_flags(
        ctx.args, GUILD_WORDS, FAILED_WORDS, REFRESH_WORDS, UP_WORDS, FULL_WORDS, SQUAD_WORDS)
    plates = database.get_all_stat_requirement_plates(guild_id=guild_id)
    if not plates:
        await ctx.reply("У гильдии нет плейтов — их заводят офицеры на сайте.")
        return
    plate, target_text, candidates = _match_plate(text, plates)
    state = {
        "user_id": ctx.user["id"], "guild_id": guild_id, "plates": plates, "target_text": target_text,
        "guild_mode": guild_mode, "only_failed": only_failed, "refresh": refresh,
        "scenario": SCENARIO_FULL if full else SCENARIO_UP if up else SCENARIO_RAW, "squad_only": squad_only,
    }
    if plate is None:
        token = new_session(state)
        head = f"Плейт «{esc(text)}» не найден — выберите:" if text else "Выберите плейт:"
        hint = "\n<i>Можно сразу: /stats Имя плейта [игрок] [кресты] [вверх | подгон] [обновить]</i>"
        await ctx.reply(head + hint, keyboard=_plate_keyboard(token, plates, candidates))
        return
    await _stats_run(ctx, state, plate)


@callback("sp", command_name="stats")
async def cb_stats_plate(ctx: Ctx, payload: str):
    token, _, idx = payload.partition("|")
    state = await session_for_callback(ctx, token)
    if state is None or not idx.isdigit() or int(idx) >= len(state["plates"]):
        return
    plate = state["plates"][int(idx)]
    await ctx.edit(f"Плейт: <b>{esc(plate)}</b>")
    await _stats_run(ctx, state, plate)


@callback("st", command_name="stats")
async def cb_stats_target(ctx: Ctx, payload: str):
    token, _, code = payload.partition("|")
    state = await session_for_callback(ctx, token)
    if state is None:
        return
    await ctx.edit(f"Игрок: <b>{esc(_player_name(state['guild_id'], code))}</b>")
    await _stats_player(ctx, state, state["plate"], code)


async def _stats_run(ctx: Ctx, state: dict, plate: str):
    state["plate"] = plate
    guild_id = state["guild_id"]
    if state["guild_mode"]:
        if ctx.access["tier"] != "officer":
            await ctx.reply(SELF_ONLY)
            return
        await _stats_guild(ctx, state, plate)
        return
    token = new_session(state)
    ally_code = await _resolve_target(ctx, guild_id, state["target_text"], f"st|{token}")
    if ally_code:
        await _stats_player(ctx, state, plate, ally_code)


async def _stats_guild(ctx: Ctx, state: dict, plate: str):
    await ctx.action()
    stat_calc = await _stat_calc(ctx)
    if stat_calc is None:
        return
    char_keys = database.get_stat_requirement_characters(plate, guild_id=state["guild_id"])
    report = await stat_forecast.build_guild_report(ctx.app.comlink, stat_calc, plate, char_keys, guild_id=state["guild_id"],
                                                    scenario=state["scenario"], squad_only=state["squad_only"])
    if report["error"]:
        await ctx.reply(f"❌ {esc(report['error'])}")
        return
    lines = [f"📋 <b>{esc(plate)}</b> — гильдия · {SCENARIO_TITLES[state['scenario']]}", f"✅ Полностью соответствуют: {len(report['compliant'])}/{report['total_players']}"]
    if report["no_data"]:
        lines.append(f"⚠️ Нет данных: {len(report['no_data'])}")
    if report["skipped"]:
        lines.append(f"⚪ Нет отряда: {len(report['skipped'])}")
    if report["compliant"]:
        lines += ["", "✅ " + esc(", ".join(r["name"] for r in report["compliant"]))]
    if report["problem"]:
        lines += ["", "<i>дробь — только обязательные требования</i>"]
        lines += [f"❌ {esc(r['name'])} — {r['matched']}/{r['total']}" for r in report["problem"]]
    else:
        lines += ["", "Все закрыли все требования! 🎉"]
    await _reply_with_options(ctx, state, lines)


async def _stats_player(ctx: Ctx, state: dict, plate: str, ally_code: str):
    guild_id = state["guild_id"]
    denial = _self_only_error(ctx, ally_code)
    if denial:
        await ctx.reply(denial)
        return
    await ctx.action()
    stat_calc = await _stat_calc(ctx)
    if stat_calc is None:
        return
    player = _player_name(guild_id, ally_code)
    char_keys = database.get_stat_requirement_characters(plate, guild_id=guild_id)
    rows, pages, failed_by_char, updated_ats = [], [], [], []
    for base_id in char_keys:
        result = await stat_forecast.evaluate_character_player(
            ctx.app.comlink, stat_calc, plate, base_id, ally_code, state["refresh"], player, guild_id=guild_id,
            scenario=state["scenario"],
        )
        state["refresh"] = False  # обновляем ростер один раз, а не на каждого персонажа
        if result is None:
            continue
        char_name, block, matched, total, updated_at, failed_required, *_ = result
        rows.append((char_name, matched, total, bool(failed_required)))
        if state["only_failed"] and matched == total:
            continue
        pages.append((char_name, block))
        if updated_at:
            updated_ats.append(updated_at)
        if failed_required:
            failed_by_char.append((char_name, failed_required))
    if not rows:
        await ctx.reply("❌ Нет сохранённых требований для этого плейта.")
        return

    rows.sort(key=lambda r: not r[3])  # сперва с ❌ по обязательным, дальше — порядок плейта
    order = {r[0]: i for i, r in enumerate(rows)}
    pages.sort(key=lambda p: order.get(p[0], len(order)))
    summary = [f"📋 <b>{esc(plate)}</b> — {esc(player)} · {SCENARIO_TITLES[state['scenario']]}", "", "<b>Обязательные статы</b>"]
    if failed_by_char:
        for char_name, items in sorted(failed_by_char, key=lambda f: order.get(f[0], 0)):
            parts = "; ".join(f"{esc(i['stat'])}: {esc(i['current'])} (нужно {esc(i['requirement'])})" for i in items)
            summary.append(f"❌ <b>{esc(char_name)}</b> — {parts}")
    else:
        summary.append("✅ Все обязательные статы в норме.")
    summary += ["", f"<b>Персонажи</b> ({len(rows)})"]
    summary += [f"{'❌' if failed else '✅'} {esc(name)} — {m}/{t}" for name, m, t, failed in rows]
    if updated_ats:
        summary += ["", f"<i>Данные игрока: {esc(updated_ats[0])}. Свежие из игры — добавьте «обновить».</i>"]
    state["ally_code"] = ally_code
    await _reply_with_options(ctx, state, summary)
    old_picker = [state["picker_id"]] if state.get("picker_id") else []
    if not pages:
        state["picker_id"] = None
        await _send_or_edit(ctx, old_picker, [])
        return
    # Персонажи — отдельным сообщением с кнопками: итог большого плейта (AC_ALL, ~100
    # персонажей) занимает несколько сообщений, а листать удобно в одном месте.
    picker = f"Открыть персонажа ({len(pages)}):" if len(pages) <= PICKER_MAX else \
        f"Открыть персонажа — первые {PICKER_MAX} из {len(pages)} (сначала с ❌):"
    page_state = {"user_id": ctx.user["id"], "picker": picker, "pages": pages}
    token = new_session(page_state)
    state["picker_id"] = (await _send_or_edit(ctx, old_picker, [(picker, _picker_keyboard(token, pages))]))[0]


PICKER_MAX = 99  # Telegram: до 100 кнопок на сообщение


async def _send_or_edit(ctx: Ctx, old_ids: list, messages: list) -> list:
    """Показать messages [(текст, клавиатура)] правкой старых сообщений old_ids: лишние старые
    удалить, недостающие дослать. Кнопки сценария и «только ❌» меняют те же сообщения, а не
    шлют сводку заново (NicolozZ 2026-10-26). Возвращает id сообщений по порядку."""
    ids = []
    for i, (text, keyboard) in enumerate(messages):
        if i < len(old_ids):
            try:
                await ctx.api.edit_message_text(ctx.chat_id, old_ids[i], text, keyboard=keyboard)
                ids.append(old_ids[i])
                continue
            except TelegramError as e:
                if "not modified" in e.description:
                    ids.append(old_ids[i])
                    continue
        ids.append((await ctx.reply(text, keyboard=keyboard))["message_id"])
    for message_id in old_ids[len(messages):]:
        try:
            await ctx.api.delete_message(ctx.chat_id, message_id)
        except TelegramError:
            pass
    return ids


async def _reply_with_options(ctx: Ctx, state: dict, lines: list):
    """Итог + кнопки под последним сообщением: сценарий (как в Discord /статы) и «только ❌»
    (игрок) / «только с отрядом» (гильдия) — пересчитать, не набирая команду заново."""
    chunks = markup.chunk_md(lines)
    token = new_session(state)
    row = [{"text": ("✓ " if key == state["scenario"] else "") + label, "callback_data": f"so|{token}|{key}"}
           for key, label in SCENARIO_BUTTONS]
    if state["guild_mode"]:
        toggle = {"text": ("✓ " if state["squad_only"] else "") + "Только с отрядом", "callback_data": f"so|{token}|squad"}
    else:
        toggle = {"text": ("✓ " if state["only_failed"] else "") + "Только ❌", "callback_data": f"so|{token}|failed"}
    messages = [(chunk, None) for chunk in chunks[:-1]] + [(chunks[-1], [row, [toggle]])]
    state["summary_ids"] = await _send_or_edit(ctx, state.get("summary_ids") or [], messages)


@callback("so", command_name="stats")
async def cb_stats_option(ctx: Ctx, payload: str):
    token, _, option = payload.partition("|")
    state = await session_for_callback(ctx, token)
    if state is None:
        return
    state = dict(state)
    if option in SCENARIO_TITLES:
        state["scenario"] = option
    elif option == "squad":
        state["squad_only"] = not state["squad_only"]
    elif option == "failed":
        state["only_failed"] = not state["only_failed"]
    else:
        return
    state["refresh"] = False
    await ctx.api.answer_callback(ctx.callback_query["id"], "Пересчитываю…")
    if state["guild_mode"]:
        await _stats_guild(ctx, state, state["plate"])
    else:
        await _stats_player(ctx, state, state["plate"], state["ally_code"])


def _picker_keyboard(token: str, pages: list) -> list:
    buttons = [{"text": name[:30], "callback_data": f"sc|{token}|{i}"} for i, (name, _block) in enumerate(pages[:PICKER_MAX])]
    return [buttons[i:i + 3] for i in range(0, len(buttons), 3)]


@callback("sc", command_name="stats")
async def cb_stats_page(ctx: Ctx, payload: str):
    token, _, target = payload.partition("|")
    state = await session_for_callback(ctx, token)
    if state is None:
        return
    pages = state["pages"]
    if target == "s" or not target.isdigit():
        await ctx.edit(state["picker"], keyboard=_picker_keyboard(token, pages))
        return
    i = max(0, min(int(target), len(pages) - 1))
    char_name, block = pages[i]
    text = markup.md_to_html(block)
    if len(text) > 4000:
        text = markup.md_to_html(markup.chunk_md([block])[0]) + "\n…"
    nav = []
    if i > 0:
        nav.append({"text": "◀", "callback_data": f"sc|{token}|{i - 1}"})
    nav.append({"text": "Список", "callback_data": f"sc|{token}|s"})
    if i < len(pages) - 1:
        nav.append({"text": "▶", "callback_data": f"sc|{token}|{i + 1}"})
    await ctx.edit(f"<b>{esc(char_name)}</b> · {i + 1}/{len(pages)}\n{text}", keyboard=[nav])


# ======================================================================== /dc

async def _catalog(ctx: Ctx):
    try:
        return await datacron_catalog.get_catalog(ctx.app.comlink)
    except Exception as e:
        print(f"⚠️ [TG] каталог датакронов: {e!r}")
        await ctx.reply("⏳ Справочник датакронов ещё загружается — попробуйте через пару минут.")
        return None


def _seasons_with_requirements(catalog: dict, guild_id: int) -> list[tuple[int, str]]:
    seasons = []
    for set_id, data in sorted(catalog["seasons"].items(), key=lambda kv: -kv[0]):
        if database.get_datacron_requirements_by_set(set_id, guild_id=guild_id) or \
                database.get_datacron_focused_requirements_by_set(set_id, guild_id=guild_id):
            seasons.append((set_id, data["display_name"]))
    return seasons


@command("dc", "Проверка датакронов: свои (чужие — офицеры)", aliases=("дк", "датакроны"))
async def cmd_dc(ctx: Ctx):
    guild_id = ctx.access["guild_id"]
    off = _feature_off(guild_id, "datacrons")
    if off:
        await ctx.reply(off)
        return
    text, (guild_mode,) = _split_flags(ctx.args, GUILD_WORDS)
    if guild_mode:
        await ctx.reply(f"Сводка по всей гильдии — на сайте: {ctx.app.site_url}/datacrons/check")
        return
    catalog = await _catalog(ctx)
    if catalog is None:
        return
    seasons = _seasons_with_requirements(catalog, guild_id)
    if not seasons:
        await ctx.reply("У гильдии нет требований по датакронам.")
        return
    state = {"user_id": ctx.user["id"], "guild_id": guild_id, "target_text": text}
    if len(seasons) == 1:
        state["set_id"] = seasons[0][0]
        await _dc_target(ctx, state)
        return
    token = new_session(state)
    keyboard = [[{"text": f"{name} · {set_id}", "callback_data": f"ds|{token}|{set_id}"}] for set_id, name in seasons[:8]]
    await ctx.reply("Какой сезон проверить?", keyboard=keyboard)


@callback("ds", command_name="dc")
async def cb_dc_season(ctx: Ctx, payload: str):
    token, _, set_id = payload.partition("|")
    state = await session_for_callback(ctx, token)
    if state is None or not set_id.isdigit():
        return
    state["set_id"] = int(set_id)
    await ctx.edit(f"Сезон {set_id} — проверяю…")
    await _dc_target(ctx, state)


@callback("dt", command_name="dc")
async def cb_dc_target(ctx: Ctx, payload: str):
    token, _, code = payload.partition("|")
    state = await session_for_callback(ctx, token)
    if state is None:
        return
    await ctx.edit(f"Игрок: <b>{esc(_player_name(state['guild_id'], code))}</b>")
    await _dc_player(ctx, state, code)


async def _dc_target(ctx: Ctx, state: dict):
    token = new_session(state)
    ally_code = await _resolve_target(ctx, state["guild_id"], state["target_text"], f"dt|{token}")
    if ally_code:
        await _dc_player(ctx, state, ally_code)


async def _dc_player(ctx: Ctx, state: dict, ally_code: str):
    denial = _self_only_error(ctx, ally_code)
    if denial:
        await ctx.reply(denial)
        return
    await ctx.action()
    catalog = await _catalog(ctx)
    if catalog is None:
        return
    guild_id, set_id = state["guild_id"], state["set_id"]
    try:
        player = await asyncio.wait_for(asyncio.to_thread(ctx.app.comlink.get_player, allycode=ally_code), timeout=20)
    except Exception as e:
        print(f"⚠️ [TG дк] get_player {ally_code}: {e!r}")
        player = None
    if not player:
        await ctx.reply(f"❌ Не удалось получить игрока <code>{ally_code}</code> из игры.")
        return
    requirements = database.get_datacron_requirements_by_set(set_id, guild_id=guild_id)
    focused = database.get_datacron_focused_requirements_by_set(set_id, guild_id=guild_id)
    lines, matched, total = build_player_check(catalog, player, set_id, requirements, focused, guild_id)
    season = catalog["seasons"].get(set_id, {}).get("display_name") or f"Сезон {set_id}"
    head = f"📋 Датакроны: **{player.get('name') or ally_code}** — {season} ({matched}/{total})"
    for chunk in markup.chunk_md([head, ""] + lines):
        await ctx.reply(markup.md_to_html(chunk))

