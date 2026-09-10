"""Предупреждения "канал/роль не настроены" для фич, зависящих от per-guild
настроек (`guilds.*_channel_id`/`*_role_id`) — общий источник для веб-страниц и
Discord-команд, чтобы офицер видел это ДО того, как что-то не сработает молча
(запрошено пользователем 2026-09-10: "если я ввожу фразу для омикрона — у меня
на этой страничке заранее должно быть указано, что канал не настроен").

Информационное, не блокирующее: страница/команда всё равно работает (фразу
можно сохранить, ДР — задать) — просто видно, что реального эффекта не будет,
пока канал/роль не заданы через /настройки (или /омикрон_текст канал, который
не часть /настройки, но по смыслу та же настройка).

Источник подписей — cogs.guild_settings.SETTINGS_FIELDS (те же названия, что и
в /настройки список), плюс omicron_channel_id отдельным полем, т.к. он живёт в
отдельной команде /омикрон_текст."""

from cogs.guild_settings import SETTINGS_FIELDS

_EXTRA_FIELD_LABELS = [
    ("omicron_channel_id", "Канал для автообъявлений об омикронах"),
]

_FIELD_LABELS = dict(SETTINGS_FIELDS + _EXTRA_FIELD_LABELS)

# feature_key -> (список полей guilds, которые фиче нужны, чтобы реально что-то делать)
FEATURE_FIELDS = {
    "tb_rotation": ["ping_channel_id", "ping_role_id"],
    "tb_plan_order": ["tb_plan_channel_id", "tb_order_source_channel_id", "tb_order_role_id"],
    "tb_report": ["officer_channel_id"],
    "birthday": ["birthday_channel_id", "birthday_role_id"],
    "tw_guide": ["tw_guide_forum_channel_id"],
    "tasks": ["tasks_log_channel_id"],
    "omicron": ["omicron_channel_id"],
}

# Человекочитаемое название фичи — для сводки на дашборде (missing_summary), где
# перечисляются все недонастроенные фичи разом, а не одна конкретная.
FEATURE_LABELS = {
    "tb_rotation": "Тег на ротацию/взводы перед ТБ",
    "tb_plan_order": "План и автоордер ТБ",
    "tb_report": "Автоотчёт по итогам ТБ",
    "birthday": "Дни рождения",
    "tw_guide": "Контры по ВГ",
    "tasks": "Уведомления по задачам",
    "omicron": "Автообъявления об омикронах",
}


def missing_config_labels(guild_cfg: dict, feature: str) -> list[str]:
    """Подписи полей из FEATURE_FIELDS[feature], которые в guild_cfg пустые/NULL."""
    if not guild_cfg:
        return [_FIELD_LABELS[f] for f in FEATURE_FIELDS[feature]]
    return [_FIELD_LABELS[f] for f in FEATURE_FIELDS[feature] if not guild_cfg.get(f)]


def config_warning_html(guild_cfg: dict, feature: str) -> str | None:
    """Готовый HTML для веб-страницы (рендерить через `|safe`) либо None, если всё настроено."""
    missing = missing_config_labels(guild_cfg, feature)
    if not missing:
        return None
    items = "; ".join(missing)
    return f'⚠️ Не настроено: {items}. Настройте в <a href="/settings">Настройках</a> — до этого функция не будет работать.'


def config_warning_text(guild_cfg: dict, feature: str) -> str | None:
    """То же для Discord-ответа (обычный текст, без HTML) либо None."""
    missing = missing_config_labels(guild_cfg, feature)
    if not missing:
        return None
    items = "; ".join(missing)
    return f"⚠️ Не настроено: {items}. Настройте через `/настройки` (или `/омикрон_текст канал`) — до этого функция не будет работать."


def missing_summary(guild_cfg: dict) -> list[dict]:
    """Сводка по ВСЕМ фичам сразу — [{"feature": ключ, "label": название фичи,
    "missing": [подписи полей]}, ...] только для тех, у кого чего-то не хватает.

    Нужна для фич без отдельной страницы/команды-триггера (tb_rotation, tb_report —
    их запускают только фоновые циклы бота, не разовое действие офицера), поэтому
    единственное разумное место предупредить — общая сводка на главной /, которую
    офицер видит при каждом заходе, а не point-in-time предупреждение конкретной
    команды/страницы."""
    result = []
    for feature, label in FEATURE_LABELS.items():
        missing = missing_config_labels(guild_cfg, feature)
        if missing:
            result.append({"feature": feature, "label": label, "missing": missing})
    return result
