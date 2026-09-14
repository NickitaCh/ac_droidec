"""Рассылка взводов ТБ игрокам в личку Discord — общая точка правды для веб-страницы
(/tb/platoons/notify) и слэш-команды (/тб_план разослать_взводы). По прямому запросу
пользователя 2026-09-14 ("подготовить на тест функционал рассылки взводов людям в личку").

Отправка сделана через голый Discord REST (httpx — тот же клиент, что уже используется
для REST-вызовов из веб-процесса в web/routes/guild_dashboard.py::_fetch_discord_channel
и в tb_plan_reader.py), а не через объект бота (disnake Client) — потому что веб-процесс
(web/app.py) не держит gateway-соединение с ботом, запускается отдельным процессом на
том же VPS и работает с БД напрямую (см. комментарий в web/app.py). REST-вызов "открыть
DM + отправить" не требует gateway, только токен — тем же путём отправляет и бот-процесс
(cogs/tb_order_image.py), чтобы не дублировать логику формирования сообщения и отправки
в двух местах.

Кому отправлять — решает вызывающая сторона (веб-чекбоксы / выбор в Discord), эта
модуль только считает "кому НАЗНАЧЕНО что-то на этом этапе" (build_player_rows) и
отправляет (send_broadcast) уже отфильтрованному множеству ally_code. Отслеживания
"донат уже реально сделан в игре" здесь нет и не планируется — Comlink это не отдаёт
надёжно; исключение получателей при повторной отправке — ручное решение офицера."""

import httpx

import database
import tb_plan_reader
import tb_platoon_data
import tb_platoon_engine

DISCORD_API_BASE = "https://discord.com/api/v10"


async def build_player_rows(guild_id: int, plan: dict, round_num: int) -> list[dict]:
    """Кому что назначено на выбранном этапе плана — [{"ally_code", "name", "discord_id",
    "entries": [{"planet", "operation", "unit"}, ...]}, ...], отсортировано по имени.
    discord_id может быть None (игрок есть в user_mapping гильдии, но не привязал Discord —
    такие пропускаются на отправке, не здесь, чтобы вызывающая сторона могла показать их
    в списке серым/с пометкой "нет привязки")."""
    entries, _error = await tb_plan_reader.fetch_plan_planets(plan)

    seen_planets = set()
    round_entries = []
    for e in entries:
        if e["round"] != round_num or not e["planet"] or e.get("no_platoons"):
            continue
        if e["planet"] in seen_planets:
            continue
        seen_planets.add(e["planet"])
        round_entries.append(e)

    assignments = database.get_tb_platoon_assignments(guild_id, plan["id"])
    # user_mapping.discord_id (get_all_user_mappings) НЕ значит "игрок привязал Discord" —
    # это ростер-зеркало из sync_guild_roster (cogs/violations.py), которое для ещё не
    # зарегистрированных игроков заполняет discord_id тем же значением, что и ally_code
    # (плейсхолдер "хотя бы не терять строку", см. комментарий там же) — реальный Discord
    # ID берётся из отдельной user_registration (заполняется только /регистрацией).
    # Перепутать эти два источника — реальный баг живьём: 400 "Invalid Recipient(s)" от
    # Discord при попытке открыть DM с 9-значным "ID" (это был ally_code, не snowflake),
    # см. database.py::get_discord_id_for_ally — тот же паттерн уже используется в
    # tasks.py для личных уведомлений о задачах.
    mappings = database.get_all_user_mappings(guild_id)
    name_by_ally = {ally_code: name for _discord_id, ally_code, name in mappings}
    discord_id_by_ally = {
        ally_code: discord_id for discord_id, ally_code, _name in database.get_all_main_registrations(guild_id)
    }

    rows: dict[str, dict] = {}
    for e in round_entries:
        for operation in range(1, 7):
            unit_names = tb_platoon_data.ROTE_PLATOON_SUGGESTIONS.get((e["planet"], operation)) or []
            for slot_index, unit_name in enumerate(unit_names):
                assignment = tb_platoon_engine.visible_assignment(
                    assignments.get((e["planet"], operation, slot_index)), round_num,
                )
                if not assignment:
                    continue
                ally_code = assignment["ally_code"]
                row = rows.setdefault(ally_code, {
                    "ally_code": ally_code,
                    "name": name_by_ally.get(ally_code, ally_code),
                    "discord_id": discord_id_by_ally.get(ally_code),
                    "entries": [],
                })
                row["entries"].append({"planet": e["planet"], "operation": operation, "unit": unit_name})

    for row in rows.values():
        row["entries"].sort(key=lambda i: (i["planet"], i["operation"]))
    return sorted(rows.values(), key=lambda r: r["name"])


def format_dm_text(guild_name: str, plan_name: str, round_num: int, entries: list[dict]) -> str:
    lines = [
        f"📋 Твои взводы на ТБ «{guild_name}» — этап {round_num} (план «{plan_name}»):",
        "",
    ]
    for entry in entries:
        lines.append(f"• {entry['planet']}, взвод {entry['operation']} — {entry['unit']}")
    lines.append("")
    lines.append("Если уже задонатил(а) — просто игнорируй это сообщение.")
    return "\n".join(lines)


async def _send_dm(client: httpx.AsyncClient, token: str, discord_id: str, text: str) -> tuple[bool, str]:
    headers = {"Authorization": f"Bot {token}"}
    resp = await client.post(
        f"{DISCORD_API_BASE}/users/@me/channels", headers=headers, json={"recipient_id": discord_id},
    )
    if resp.status_code != 200:
        return False, f"не удалось открыть DM ({resp.status_code}: {resp.text[:200]})"
    channel_id = resp.json()["id"]

    resp2 = await client.post(
        f"{DISCORD_API_BASE}/channels/{channel_id}/messages", headers=headers, json={"content": text},
    )
    if resp2.status_code not in (200, 201):
        return False, f"не удалось отправить ({resp2.status_code}: {resp2.text[:200]})"
    return True, "ok"


async def send_broadcast(
    token: str, guild_id: int, plan: dict, round_num: int, ally_codes: set[str],
) -> list[dict]:
    """Отправляет DM только тем ally_code из ally_codes, у кого есть назначение на этом
    этапе И привязанный discord_id. Возвращает отчёт по каждому затронутому игроку:
    [{"ally_code", "name", "status": "sent"|"failed"|"skipped_no_discord", "detail"}, ...]."""
    guild_cfg = database.get_guild_config(guild_id) or {}
    guild_name = guild_cfg.get("name") or "гильдия"
    rows = await build_player_rows(guild_id, plan, round_num)

    report = []
    async with httpx.AsyncClient(timeout=15.0) as client:
        for row in rows:
            if row["ally_code"] not in ally_codes:
                continue
            if not row["discord_id"]:
                report.append({
                    "ally_code": row["ally_code"], "name": row["name"],
                    "status": "skipped_no_discord", "detail": "нет привязки Discord",
                })
                continue
            text = format_dm_text(guild_name, plan["name"], round_num, row["entries"])
            ok, detail = await _send_dm(client, token, row["discord_id"], text)
            report.append({
                "ally_code": row["ally_code"], "name": row["name"],
                "status": "sent" if ok else "failed", "detail": detail,
            })
    return report
