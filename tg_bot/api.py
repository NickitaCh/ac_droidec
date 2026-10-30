"""Минимальный клиент Telegram Bot API на httpx (уже в зависимостях проекта) —
без aiogram, чтобы не тянуть его версии aiohttp/pydantic в общий requirements.txt,
который ставят и бот, и веб. Нам нужны только long polling, отправка текста/картинок,
кнопки и правка сообщений."""

import html
import json
import re

import httpx

API_URL = "https://api.telegram.org/bot{token}/{method}"

# Лимиты Telegram: текст сообщения 4096 символов, подпись к фото 1024.
MESSAGE_LIMIT = 4096
CAPTION_LIMIT = 1024


def _strip_tags(text: str) -> str:
    return html.unescape(re.sub(r"</?(b|i|u|s|code|pre|a)( [^>]*)?>", "", text))


class TelegramError(Exception):
    def __init__(self, method: str, description: str, error_code: int | None = None):
        super().__init__(f"{method}: {description}")
        self.description = description
        self.error_code = error_code


class TelegramAPI:
    def __init__(self, token: str, timeout: float = 70):
        self._token = token
        self._client = httpx.AsyncClient(timeout=timeout)

    async def close(self):
        await self._client.aclose()

    async def call(self, method: str, files: dict | None = None, **params):
        params = {k: v for k, v in params.items() if v is not None}
        url = API_URL.format(token=self._token, method=method)
        if files:
            # multipart: вложенные структуры (reply_markup) передаются JSON-строкой
            data = {k: json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else str(v) for k, v in params.items()}
            resp = await self._client.post(url, data=data, files=files)
        else:
            resp = await self._client.post(url, json=params)
        payload = resp.json()
        if not payload.get("ok"):
            raise TelegramError(method, payload.get("description", resp.text), payload.get("error_code"))
        return payload["result"]

    async def get_me(self) -> dict:
        return await self.call("getMe")

    async def get_updates(self, offset: int | None, timeout: int = 50) -> list[dict]:
        return await self.call(
            "getUpdates", offset=offset, timeout=timeout,
            allowed_updates=["message", "callback_query", "my_chat_member", "inline_query"],
        )

    async def send_message(self, chat_id, text: str, reply_to: int | None = None, keyboard: list | None = None,
                           thread_id: int | None = None) -> dict:
        try:
            return await self._send_message(chat_id, text, "HTML", reply_to, keyboard, thread_id)
        except TelegramError as e:
            # Текст ошибки из Comlink/исключения с «<» ломает HTML-разметку — шлём как есть.
            if "can't parse entities" not in e.description:
                raise
            return await self._send_message(chat_id, _strip_tags(text), None, reply_to, keyboard, thread_id)

    async def _send_message(self, chat_id, text, parse_mode, reply_to, keyboard, thread_id) -> dict:
        return await self.call(
            "sendMessage", chat_id=chat_id, text=text[:MESSAGE_LIMIT], parse_mode=parse_mode,
            link_preview_options={"is_disabled": True},
            reply_parameters={"message_id": reply_to, "allow_sending_without_reply": True} if reply_to else None,
            reply_markup={"inline_keyboard": keyboard} if keyboard else None,
            message_thread_id=thread_id,
        )

    async def send_photo(self, chat_id, png: bytes, filename: str, caption: str | None = None,
                         reply_to: int | None = None, thread_id: int | None = None, keyboard: list | None = None) -> dict:
        return await self.call(
            "sendPhoto", files={"photo": (filename, png, "image/png")},
            chat_id=chat_id, caption=(caption or "")[:CAPTION_LIMIT] or None, parse_mode="HTML",
            reply_parameters={"message_id": reply_to, "allow_sending_without_reply": True} if reply_to else None,
            message_thread_id=thread_id,
            reply_markup={"inline_keyboard": keyboard} if keyboard else None,
        )

    async def send_document(self, chat_id, data: bytes, filename: str, mime: str, caption: str | None = None,
                            reply_to: int | None = None, thread_id: int | None = None, keyboard: list | None = None) -> dict:
        return await self.call(
            "sendDocument", files={"document": (filename, data, mime)},
            chat_id=chat_id, caption=(caption or "")[:CAPTION_LIMIT] or None, parse_mode="HTML",
            reply_parameters={"message_id": reply_to, "allow_sending_without_reply": True} if reply_to else None,
            message_thread_id=thread_id,
            reply_markup={"inline_keyboard": keyboard} if keyboard else None,
        )

    async def edit_message_text(self, chat_id, message_id: int, text: str, keyboard: list | None = None) -> dict:
        return await self.call(
            "editMessageText", chat_id=chat_id, message_id=message_id, text=text[:MESSAGE_LIMIT],
            parse_mode="HTML", link_preview_options={"is_disabled": True},
            reply_markup={"inline_keyboard": keyboard} if keyboard else None,
        )

    async def delete_message(self, chat_id, message_id: int):
        return await self.call("deleteMessage", chat_id=chat_id, message_id=message_id)

    async def answer_callback(self, callback_id: str, text: str | None = None, alert: bool = False):
        return await self.call("answerCallbackQuery", callback_query_id=callback_id, text=text, show_alert=alert or None)

    async def send_chat_action(self, chat_id, action: str = "typing", thread_id: int | None = None):
        return await self.call("sendChatAction", chat_id=chat_id, action=action, message_thread_id=thread_id)

    async def set_my_commands(self, commands: list[tuple[str, str]], scope: dict | None = None):
        return await self.call(
            "setMyCommands", commands=[{"command": c, "description": d} for c, d in commands], scope=scope,
        )

    async def set_my_description(self, description: str):
        """Текст на пустом экране бота до первого /start (до 512 символов)."""
        return await self.call("setMyDescription", description=description)

    async def set_my_short_description(self, short_description: str):
        """Текст в профиле бота и при пересылке ссылки на него (до 120 символов)."""
        return await self.call("setMyShortDescription", short_description=short_description)
