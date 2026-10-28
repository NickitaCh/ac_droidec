"""Бот не может писать в канал из настроек гильдии (2026-10-28, как в HotUtils V2).

Раньше такие ошибки (403 «Missing Access/Permissions», 404 «Unknown Channel») только
печатались в журнал контейнера, и офицеры не знали, что напоминания не доходят. Теперь:
- сбой отправки в канал, заданный в настройках гильдии (колонки guilds.*_channel_id),
  запоминается в bot_state "channel_error:<поле>" (бакет гильдии);
- офицеру, который этот канал задал (bot_state "channel_set_by:<поле>", пишется при
  сохранении /settings и /настройки), а если он неизвестен — лидеру гильдии, уходит личка,
  один раз на серию сбоев;
- на /settings у поля — пометка с причиной;
- первая успешная отправка в этот канал пометку снимает.

Перехват — в двух местах: disnake (main.py ставит install_disnake_hook на Messageable.send)
и services/discord_rest.py::send_message (веб/REST)."""

import json
import time

import database

FAIL_STATUSES = (403, 404)
LEADER_LEVEL = 4  # member_level лидера гильдии в Comlink (3 — офицер)


def channel_fields() -> list[str]:
    return [c for c in database.GUILD_CONFIG_COLUMNS if c.endswith("_channel_id")]


def _matches(channel_id: str) -> list[tuple[dict, str]]:
    """[(cfg, поле)] — где этот канал задан в настройках гильдий."""
    channel_id = str(channel_id)
    found = []
    for cfg in database.get_all_guild_configs():
        for field in channel_fields():
            if str(cfg.get(field) or "") == channel_id:
                found.append((cfg, field))
    return found


def record_setter(guild_id: int, field: str, discord_id: str | None) -> None:
    if discord_id:
        database.set_bot_state(f"channel_set_by:{field}", str(discord_id), guild_id=guild_id)


def get_error(guild_id: int, field: str) -> dict | None:
    raw = database.get_bot_state(f"channel_error:{field}", guild_id=guild_id)
    if not raw:
        return None
    try:
        return json.loads(raw)
    except ValueError:
        return None


def _reason(status: int) -> str:
    if status == 404:
        return "канал удалён или бот его не видит"
    return "у бота нет прав видеть этот канал или писать в него"


def _label(field: str) -> str:
    try:
        from cogs.guild_settings import SETTINGS_FIELDS
    except Exception:
        return field
    return dict(SETTINGS_FIELDS).get(field, field)


def _recipient(cfg: dict, field: str) -> str | None:
    who = database.get_bot_state(f"channel_set_by:{field}", guild_id=cfg["id"])
    if who:
        return who
    for ally_code, _name, level in database.get_all_user_mappings_with_rank(cfg["id"]):
        if level == LEADER_LEVEL:
            discord_id = database.get_discord_id_for_ally(ally_code, guild_id=cfg["id"])
            if discord_id:
                return discord_id
    return None


async def report_failure(channel_id, status: int) -> None:
    """Вызывается после неудачной отправки в канал. Не бросает исключений."""
    if status not in FAIL_STATUSES:
        return
    try:
        for cfg, field in _matches(channel_id):
            state = get_error(cfg["id"], field)
            if state and state.get("channel_id") == str(channel_id) and state.get("notified"):
                continue  # личка за эту серию сбоев уже ушла
            state = {"channel_id": str(channel_id), "status": status, "reason": _reason(status),
                     "since": (state or {}).get("since") or int(time.time()), "notified": False}
            recipient = _recipient(cfg, field)
            if recipient:
                from services import discord_rest
                text = (f"⚠️ Гильдия **{cfg['name']}**: бот не может написать в канал <#{channel_id}> "
                        f"(«{_label(field)}») — {state['reason']}. Сообщения туда сейчас не доходят. "
                        f"Выдайте боту права на канал или выберите другой в настройках гильдии на сайте.")
                try:
                    await discord_rest.send_dm(recipient, text)
                    state["notified"] = True
                except Exception as e:
                    print(f"⚠️ [Каналы] личка {recipient} о канале {channel_id} не ушла: {e!r}")
            database.set_bot_state(f"channel_error:{field}", json.dumps(state), guild_id=cfg["id"])
            print(f"⚠️ [Каналы] [{cfg['name']}] {field}={channel_id}: {state['reason']} ({status})")
    except Exception as e:
        print(f"⚠️ [Каналы] report_failure({channel_id}): {e!r}")


def report_success(channel_id) -> None:
    """Успешная отправка — снять пометку, если была. Не бросает исключений."""
    try:
        for cfg, field in _matches(channel_id):
            if database.get_bot_state(f"channel_error:{field}", guild_id=cfg["id"]):
                database.set_bot_state(f"channel_error:{field}", "", guild_id=cfg["id"])
    except Exception as e:
        print(f"⚠️ [Каналы] report_success({channel_id}): {e!r}")


def install_disnake_hook() -> None:
    """Оборачивает disnake.abc.Messageable.send: 403/404 в канал гильдии → report_failure,
    успех → report_success. Личка (DMChannel/User) не трогается. Исключение пробрасывается
    дальше как было — вызывающий код ведёт себя по-прежнему."""
    import disnake
    from disnake.abc import GuildChannel, Messageable

    if getattr(Messageable.send, "_channel_health", False):
        return
    original = Messageable.send

    async def send(self, *args, **kwargs):
        target = self
        if isinstance(self, disnake.Thread):
            target = self.parent or self
        is_guild = isinstance(self, (GuildChannel, disnake.Thread))
        try:
            message = await original(self, *args, **kwargs)
        except (disnake.Forbidden, disnake.NotFound) as e:
            if is_guild:
                await report_failure(self.id, e.status)
                if target is not self:
                    await report_failure(target.id, e.status)
            raise
        if is_guild:
            report_success(self.id)
        return message

    send._channel_health = True
    Messageable.send = send
