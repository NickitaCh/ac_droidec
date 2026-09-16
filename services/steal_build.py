"""Ядро фичи "Спиздить билд" (/steal-build) — по запросу пользователя (чат, 2026-09-14):
вводишь код союзника ЛЮБОГО игрока (необязательно из наших обслуживаемых гильдий), резолвишь
его СВГО-гильдию через Comlink (тот же приём, что services/guild_admin.py::add_guild — просто
без записи в БД, чужая гильдия НЕ становится обслуживаемой) и смотришь, как в среднем эта
гильдия модит конкретного персонажа: по каждому участнику, у кого персонаж открыт, статы
проецируются на выбранный релик (stat_engine.project_unit_relic — тот же приём, что уже
использует /статы), затем арифметически усредняются по всем таким участникам и делятся на
"голую" базу того же персонажа на том же релике БЕЗ единого мода (7★/ур.85/G13 — см.
cogs.stat_requirements._build_synthetic_unit, тот же синтетический юнит, что уже строит
/статы_релик) — получившееся отношение и есть "прирост от модов" в среднем по гильдии.

Каждого участника ЧУЖОЙ гильдии приходится тянуть живым comlink.get_player(player_id=...) —
в отличие от /mod-search и /статы, эти игроки не лежат в нашей player_unit_cache (синк
покрывает только зарегистрированные у нас гильдии), поэтому расчёт на крупной гильдии
(~50 участников, сеть по одному) может занять до минуты."""

import asyncio
from dataclasses import dataclass, field

import stat_engine
from cogs.stat_requirements import STAT_CHOICES, _build_synthetic_unit

# Те же строки, что показывает /mod-builder (STAT_CHOICES без Relic — уровень реликвии тут
# не "стат", это отдельный параметр расчёта, а не колонка таблицы).
STAT_ROWS = [(c.name, c.value) for c in STAT_CHOICES if c.value != "Relic"]


@dataclass
class GuildLookupResult:
    ok: bool
    error: str = None
    swgoh_guild_id: str = None
    guild_name: str = None
    members: list = field(default_factory=list)  # [(player_id, player_name), ...]


def _fmt_value(value: float) -> str:
    """Не %g — тот уходит в научную нотацию за 1e6 (Health/Protection на релике легко
    больше миллиона), тот же формат, что web/routes/stat_builder.py::_fmt_value."""
    if value == int(value):
        return str(int(value))
    return f"{value:.2f}".rstrip("0").rstrip(".")


def _fmt_stat(value: float, stat_name: str) -> str:
    suffix = "%" if stat_name in stat_engine.PERCENT_STATS else ""
    return _fmt_value(value) + suffix


async def resolve_guild(comlink, ally_code_or_guild_id: str) -> GuildLookupResult:
    """Код союзника ЛЮБОГО игрока, ИЛИ напрямую raw ID гильдии SWGOH (как отдаёт
    comlink.get_player()["guildId"], например "-kJhCaGGQqGOjgbWpJFEIg" — не
    похож на код союзника, тот всегда ровно 9 цифр, см.
    [[feedback_swgoh_ally_code_no_zero_digit]]) -> гильдия целиком (имя + список
    участников). Добавлено 2026-09-16 для /mod-analysis (проверить чужую гильдию
    по её ID, если он уже известен, без обязательного промежуточного кода
    союзника). Не трогает нашу БД — в отличие от services/guild_admin.py::
    add_guild, эта гильдия не заводится как обслуживаемая, просто читается."""
    raw = (ally_code_or_guild_id or "").strip()
    if raw and not raw.isdigit():
        swgoh_guild_id = raw
    else:
        clean_code = "".join(filter(str.isdigit, raw))
        if len(clean_code) != 9:
            return GuildLookupResult(ok=False, error="Код союзника должен состоять ровно из 9 цифр (или укажите ID гильдии SWGOH)!")

        try:
            player_data = await asyncio.to_thread(comlink.get_player, clean_code)
        except Exception as e:
            return GuildLookupResult(ok=False, error=f"Не удалось проверить код из-за сбоя связи с сервером: {e}")
        if not player_data or "name" not in player_data:
            return GuildLookupResult(ok=False, error=f"Игрок с кодом союзника {clean_code} не найден на серверах EA/CG.")

        swgoh_guild_id = player_data.get("guildId")
        if not swgoh_guild_id:
            return GuildLookupResult(ok=False, error="Этот игрок не состоит ни в одной гильдии SWGOH.")

    try:
        guild = await asyncio.to_thread(
            comlink.get_guild, swgoh_guild_id, include_recent_guild_activity_info=True
        )
    except Exception as e:
        return GuildLookupResult(ok=False, error=f"Не удалось получить данные гильдии из Comlink: {e}")
    guild = guild.get("guild", guild)
    profile = guild.get("profile", {})
    name = profile.get("name") or f"Гильдия {swgoh_guild_id}"
    # comlink.get_guild's member[] не содержит allyCode вообще (проверено вживую 2026-09-14,
    # тот же приём, что cogs/violations.py::update_roster_cache уже использует для СВОИХ гильдий) —
    # только playerId, по нему и тянем ростер участника в build_report.
    members = [
        (m["playerId"], m.get("playerName") or "")
        for m in guild.get("member", []) if m.get("playerId")
    ]
    return GuildLookupResult(ok=True, swgoh_guild_id=str(swgoh_guild_id), guild_name=name, members=members)


async def build_report(comlink, stat_calc, base_id: str, guild: GuildLookupResult, target_relic: int) -> dict:
    """Возвращает {"error"} либо {"error": None, "guild_name", "swgoh_guild_id", "target_relic",
    "total_members", "contributors", "fetch_errors", "rows"} — rows: [{"label", "avg_fmt",
    "pct_fmt"}, ...] в порядке STAT_ROWS."""
    sums: dict[str, float] = {}
    counts: dict[str, int] = {}
    contributors = 0
    fetch_errors = 0

    for player_id, _player_name in guild.members:
        try:
            player_data = await asyncio.to_thread(comlink.get_player, player_id=player_id)
        except Exception:
            fetch_errors += 1
            continue
        roster = player_data.get("rosterUnit") or player_data.get("roster") or []
        units = {}
        for u in roster:
            unit_base_id = u.get("baseId") or (u.get("definitionId", "") or "").split(":")[0]
            if unit_base_id:
                units[unit_base_id] = u
        unit = units.get(base_id)
        if not unit:
            continue  # персонаж не открыт у этого участника — не считаем его в среднее

        contributors += 1
        projected = stat_engine.project_unit_relic(unit, target_relic)
        values = stat_engine.calc_final_stats(stat_calc, projected)
        for stat_name, value in values.items():
            if value is None:
                continue
            sums[stat_name] = sums.get(stat_name, 0.0) + value
            counts[stat_name] = counts.get(stat_name, 0) + 1
        # Тот же троттлинг, что player_units_sync_loop/update_roster_cache — comlink-сайдкар
        # сам ходит наружу к EA/CG на каждый вызов, не заваливаем его частым потоком.
        await asyncio.sleep(0.1)

    if contributors == 0:
        return {"error": f"Ни у кого в гильдии «{guild.guild_name}» этот персонаж не открыт."}

    base_unit = _build_synthetic_unit(base_id, target_relic)
    base_values = stat_engine.calc_final_stats(stat_calc, base_unit)

    rows = []
    for label, stat_name in STAT_ROWS:
        count = counts.get(stat_name, 0)
        if count == 0:
            rows.append({"label": label, "avg_fmt": "нет данных", "pct_fmt": "—"})
            continue
        avg = sums[stat_name] / count
        base_val = base_values.get(stat_name)
        pct_fmt = "—"
        if base_val:
            pct = (avg / base_val - 1) * 100
            pct_fmt = f"{pct:+.1f}%"
        rows.append({"label": label, "avg_fmt": _fmt_stat(avg, stat_name), "pct_fmt": pct_fmt})

    return {
        "error": None,
        "guild_name": guild.guild_name,
        "swgoh_guild_id": guild.swgoh_guild_id,
        "target_relic": target_relic,
        "total_members": len(guild.members),
        "contributors": contributors,
        "fetch_errors": fetch_errors,
        "rows": rows,
    }
