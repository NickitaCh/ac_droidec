"""«Кто что не сделал» по итогам ВИ (2026-10-28, по мотивам автонарушений HotUtils V2).

Решение пользователя: это НЕ нарушения (в /нарушения не пишется), а список в сообщении
вместе с автоотчётом ВИ; включается галочкой в настройках ВИ (флаг tb_misses_report,
по умолчанию выкл.). Всё из сохранённой расшифровки битвы (tb_player_detail), без живых
запросов:
- залив по дням: 0 — «не заливал», меньше FULL_DEPLOY от своего самого большого дня —
  «залил не полностью» (тот же порог, что «залил полностью» в «Итогах» /tb);
- бои: день без единой попытки боя, хотя гильдия в этот день бои вела;
- ОЗ: «мог, но не пошёл» — по текущему составу, как /tb/missions для последней ВИ;
- взводы: за всю битву ни одного юнита;
- текущие участники гильдии без единой записи — «не участвовал».
"""

import json

import database
from services import tb_missions
from services.tb_results import FULL_DEPLOY


def _days_text(days: list[int]) -> str:
    return ", ".join(f"д.{d}" for d in days)


def build(guild_id: int, event_id: int, members: dict) -> list[str]:
    """members — {member_id: имя} текущего состава (get_guild на момент конца ВИ).
    Возвращает строки сообщения или [] если все всё сделали."""
    rows = database.get_tb_details_for_event(event_id)
    players = {}
    guild_attempt_days = set()
    all_days = set()
    for member_id, name, _zone, round_raw, global_raw in rows:
        try:
            rounds = {int(d): v for d, v in (json.loads(round_raw or "{}") or {}).items()}
            totals = json.loads(global_raw or "{}") or {}
        except (TypeError, ValueError):
            continue
        players[member_id] = (name, rounds, totals)
        for d, v in rounds.items():
            all_days.add(d)
            if int(v.get("strike_attempt") or 0) > 0:
                guild_attempt_days.add(d)
    if not all_days:
        return []
    days = sorted(all_days)

    skipped_covert = {}
    try:
        covert = tb_missions.covert_event(guild_id, event_id, with_skipped=True)
        for p in covert["players"]:
            labels = [c["label"] for c, cell in zip(covert["cols"], p["cells"]) if cell == "skipped"]
            if labels:
                skipped_covert[p["member_id"]] = labels
    except Exception as e:
        print(f"⚠️ [ВИ: кто что не сделал] ОЗ не посчитаны: {e!r}")

    entries = []  # (число пунктов, имя, текст)
    for member_id, name in members.items():
        if member_id not in players:
            entries.append((99, name, f"**{name}** — не участвовал в битве"))
            continue
        _name, rounds, totals = players[member_id]
        powers = {d: int((rounds.get(d) or {}).get("power") or 0) for d in days}
        top = max(powers.values(), default=0)
        no_deploy = [d for d in days if powers[d] == 0]
        partial = [d for d in days if 0 < powers[d] < FULL_DEPLOY * top]
        no_battles = [d for d in sorted(guild_attempt_days)
                      if int((rounds.get(d) or {}).get("strike_attempt") or 0) == 0]
        parts = []
        if no_deploy:
            parts.append(f"не заливал: {_days_text(no_deploy)}")
        if partial:
            parts.append(f"залил не полностью: {_days_text(partial)}")
        if no_battles:
            parts.append(f"без боёв: {_days_text(no_battles)}")
        if skipped_covert.get(member_id):
            parts.append(f"ОЗ мог, но не пошёл: {', '.join(skipped_covert[member_id])}")
        if int(totals.get("unit_donated") or 0) == 0:
            parts.append("ни одного юнита во взводы")
        if parts:
            count = len(no_deploy) + len(partial) + len(no_battles) + len(skipped_covert.get(member_id, [])) \
                + (int(totals.get("unit_donated") or 0) == 0)
            entries.append((count, name, f"**{name}** — " + "; ".join(parts)))

    if not entries:
        return ["✅ **Кто что не сделал:** все залили полностью, ходили в бои и ставили взводы."]
    entries.sort(key=lambda e: (-e[0], e[1].lower()))
    head = (f"🧾 **Кто что не сделал** (дней в битве: {len(days)}; «не полностью» — меньше "
            f"{int(FULL_DEPLOY * 100)} % от своего самого большого дня; ОЗ — по текущему составу)")
    return [head] + [e[2] for e in entries]
