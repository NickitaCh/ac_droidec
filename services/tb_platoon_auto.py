"""Автоматическая личная рассылка взводов из активного плана, независимо от публикации ордера."""
import json
import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import database
import tb_platoon_notify
from services import bot_profiles, feature_flags, tb_schedule

MSK = ZoneInfo("Europe/Moscow")
LAST_STATE = "tb_platoon_auto_last"


def validate_time(value: str) -> str:
    if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", value):
        raise ValueError("Укажите время в формате ЧЧ:ММ, от 00:00 до 23:59 (МСК).")
    return value


def order_time(day) -> str:
    hour, minute = tb_schedule.apply_correction(20, 0, tb_schedule.effective_dst_offset_minutes(day))
    return f"{hour:02d}:{minute:02d}"


def settings(guild_id: int, cfg: dict | None = None) -> dict:
    cfg = cfg if cfg is not None else (database.get_guild_config(guild_id) or {})
    plan = database.get_tb_saved_plan(cfg["tb_active_plan_id"]) if cfg.get("tb_active_plan_id") else None
    if plan and plan["guild_id"] != guild_id:
        plan = None
    raw = database.get_bot_state(LAST_STATE, guild_id=guild_id)
    try:
        last = json.loads(raw) if raw else None
    except (ValueError, TypeError):
        last = None
    return {"enabled": bool(cfg.get("tb_platoon_auto_enabled")),
            "time": cfg.get("tb_platoon_auto_time") or "",
            "order_time": order_time(datetime.now(MSK).date()), "last": last,
            "active_plan": plan["name"] if plan else None}


def due(now: datetime, cfg: dict) -> int | None:
    if not cfg.get("tb_platoon_auto_enabled"):
        return None
    day = now.date()
    if ((day - tb_schedule.get_week_anchor_date()).days // 7) % 2:
        return None
    days = sorted(tb_schedule.STANDARD_ORDER_DAYS)
    if day.weekday() not in days:
        return None
    hour, minute = map(int, validate_time(cfg.get("tb_platoon_auto_time") or order_time(day)).split(":"))
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    # Короткий рестарт не теряет рассылку, но старый этап позже не догоняем.
    if not target <= now < target + timedelta(minutes=10):
        return None
    return days.index(day.weekday()) + 1


async def tick(now: datetime | None = None):
    now = (now or datetime.now(MSK)).astimezone(MSK)
    for cfg in bot_profiles.served_guilds():
        gid = cfg["id"]
        try:
            phase = due(now, cfg)
            if phase is None or not feature_flags.is_enabled(gid, "tb_plan_order"):
                continue
            plan = database.get_tb_saved_plan(cfg["tb_active_plan_id"]) if cfg.get("tb_active_plan_id") else None
            if not plan or plan["guild_id"] != gid:
                continue
            # Ключ не зависит от выбранного времени/плана: их правка не повторяет рассылку этапа.
            key = f"tb_platoon_auto:{now.date().isoformat()}:{phase}"
            if database.get_bot_state(key, guild_id=gid):
                continue
            rows = await tb_platoon_notify.build_player_rows(gid, plan, phase)
            if not rows:
                continue
            if not database.claim_bot_state(key, now.isoformat(), guild_id=gid):
                continue
            result = {"at": now.strftime("%d.%m.%Y %H:%M"), "phase": phase,
                      "plan": plan["name"], "status": "started"}
            database.set_bot_state(LAST_STATE, json.dumps(result, ensure_ascii=False), guild_id=gid)
            try:
                report = await tb_platoon_notify.send_broadcast(
                    bot_profiles.token("discord", bot_profiles.guild_profile(gid)) or "", gid, plan, phase,
                    {row["ally_code"] for row in rows})
                result.update(status="done", sent=sum(r["status"] == "sent" for r in report),
                              failed=sum(r["status"] != "sent" for r in report))
            except Exception:
                # Не повторяем неизвестно дошедшие DM; ручная рассылка остаётся доступной.
                result.update(status="failed")
                raise
            finally:
                database.set_bot_state(LAST_STATE, json.dumps(result, ensure_ascii=False), guild_id=gid)
            print(f"✅ [TBPlatoonAuto] [{cfg['name']}] Этап {phase}: {result['sent']} доставлено, {result['failed']} не полностью")
        except Exception as error:
            print(f"❌ [TBPlatoonAuto] [{cfg.get('name', gid)}] {type(error).__name__}")
