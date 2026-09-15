"""Per-guild вкл/выкл отдельных функциональных блоков бота — супер-админ
управляет через веб /admin/features. Хранилище — тот же generic bot_state
(database.get_bot_state/set_bot_state), которое уже поддерживает произвольный
guild_id (используется и как "бот-вайд" бакет guild_id=1 для fun_toggle, см.
database.py) — под ключом "feature:<key>" в бакете самой гильдии. Новой
таблицы не требуется. Не задано -> включено (opt-out модель: сегодня всё
работает для всех гильдий, тумблер только выключает).

"antispam" — единственное исключение: у него уже есть собственное хранилище
(guilds.antispam_enabled + свой веб/дискорд UI, см. cogs/antispam.py,
cogs/guild_settings.py) — здесь только диспетчер поверх той же колонки, чтобы
он попал в общую матрицу /admin/features, без дублирования состояния в двух
местах.

Гранулярность — по функциональным блокам (см. command_catalog.py), крупные
разделы (ТБ, мод-инструменты) разбиты на подпункты. Намеренно НЕ включены сюда
(всегда доступны): регистрация (main.py::ALWAYS_ALLOWED_COMMANDS), /настройки,
/гильдия, /админы, и все общие/глобальные фоновые задачи, обслуживающие
несколько гильдий разом одним кэшем (update_roster_cache, units_sync_loop,
equipment_sync_loop, stat_calc_loop, datacron_cache_loop,
player_units_sync_loop, refresh_member_cache_loop, subscription_expiry_loop) —
их отключение по гильдии сломало бы общий кэш для остальных гильдий тоже."""

from fastapi import Depends, HTTPException

import database

FEATURES: dict[str, dict] = {
    "tb_ping": {"label": "ТБ — тег на подготовку", "category": "ТБ"},
    "tb_plan_order": {"label": "ТБ — план, автоордер, взвода", "category": "ТБ"},
    "tb_reports": {"label": "ТБ — итоговые отчёты", "category": "ТБ"},
    "tw_order": {"label": "Территориальная Война (ВГ)", "category": "Прочее"},
    "violations": {"label": "Нарушения", "category": "Прочее"},
    "birthdays": {"label": "Дни рождения", "category": "Прочее"},
    "tasks": {"label": "Задачи на прокачку", "category": "Прочее"},
    "datacrons": {"label": "Датакроны", "category": "Прочее"},
    "omicron": {"label": "Омикрон-объявления", "category": "Прочее"},
    "stat_requirements": {"label": "Статы и плейты", "category": "Мод-инструменты"},
    "mod_search": {"label": "Поиск модов", "category": "Мод-инструменты"},
    "mod_builder": {"label": "Калькулятор модов", "category": "Мод-инструменты"},
    "steal_build": {"label": "Стил-билд", "category": "Мод-инструменты"},
    "gear_farm": {"label": "Фарм снаряжения", "category": "Прочее"},
    "antispam": {"label": "Антиспам", "category": "Прочее"},
}


def label(key: str) -> str:
    return FEATURES.get(key, {}).get("label", key)


def is_enabled(guild_id: int, key: str) -> bool:
    if guild_id is None:
        return True
    if key == "antispam":
        cfg = database.get_guild_config(guild_id)
        return bool(cfg and cfg.get("antispam_enabled"))
    raw = database.get_bot_state(f"feature:{key}", guild_id=guild_id)
    return raw != "0"


def set_enabled(guild_id: int, key: str, enabled: bool, updated_by: str):
    if key == "antispam":
        database.update_guild_config(guild_id, antispam_enabled=int(enabled))
        return
    database.set_bot_state(f"feature:{key}", "1" if enabled else "0", guild_id=guild_id)
    database.set_bot_state(f"feature:{key}:updated_by", updated_by, guild_id=guild_id)


def matrix(guild_ids: list[int]) -> list[dict]:
    """Для /admin/features: список фич сгруппированных по категории, с
    состоянием на каждую переданную гильдию — {key, label, category,
    states: {guild_id: bool}}."""
    rows = []
    for key, meta in FEATURES.items():
        rows.append({
            "key": key,
            "label": meta["label"],
            "category": meta["category"],
            "states": {gid: is_enabled(gid, key) for gid in guild_ids},
        })
    return rows


# ---- Веб-гейтинг (FastAPI Depends) ----
def require_feature(key: str):
    """Фабрика FastAPI-зависимости — использовать как
    Depends(feature_flags.require_feature("mod_search")) вместо
    Depends(require_officer_access) в роутах, которые нужно гейтить."""
    from web.deps import require_officer_access

    async def _dep(user: dict = Depends(require_officer_access)) -> dict:
        if not is_enabled(user.get("guild_id"), key):
            raise HTTPException(status_code=403, detail=f"Функция «{label(key)}» отключена для вашей гильдии.")
        return user

    return _dep
