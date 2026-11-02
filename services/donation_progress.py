"""Шкала пожертвований на обслуживание сервера."""
import database
from datetime import datetime, timedelta, timezone

GOAL = 5000
MSK = timezone(timedelta(hours=3))


def current(now=None):
    now = (now or datetime.now(MSK)).astimezone(MSK)
    start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    end = start.replace(year=start.year + 1, month=1) if start.month == 12 else start.replace(month=start.month + 1)
    total = database.get_confirmed_donation_total(
        start.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
        end.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
    )
    return {"total": total, "goal": GOAL, "value": min(GOAL, max(0, total))}
