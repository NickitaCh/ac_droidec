"""Шкала пожертвований на обслуживание сервера."""
import database

GOAL = 5000


def current():
    total = database.get_confirmed_donation_total()
    return {"total": total, "goal": GOAL, "value": min(GOAL, max(0, total))}
