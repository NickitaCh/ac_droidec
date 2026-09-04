"""Общие хелперы для команд, которые достают картинку из сообщения по ссылке на
него (тред/канал) для распознавания через Mistral vision — раньше жили только в
cogs/tb_order_image.py, вынесены сюда, когда тот же паттерн понадобился
cogs/gear_farm.py, чтобы не дублировать код между двумя cogs."""

import mimetypes
import re

import disnake

LINK_RE = re.compile(r"discord(?:app)?\.com/channels/(\d+)/(\d+)(?:/(\d+))?")


def extract_channel_id(link: str):
    link = link.strip()
    m = LINK_RE.search(link)
    if m:
        return int(m.group(2))
    if link.isdigit():
        return int(link)
    return None


def extract_message_id(link: str):
    link = link.strip()
    m = LINK_RE.search(link)
    if m and m.group(3):
        return int(m.group(3))
    return None


def is_image_attachment(a: disnake.Attachment) -> bool:
    if a.content_type and a.content_type.startswith("image/"):
        return True
    return a.filename.lower().endswith((".png", ".jpg", ".jpeg", ".webp", ".gif"))


def guess_mime_type(a: disnake.Attachment) -> str:
    if a.content_type:
        return a.content_type.split(";")[0].strip()
    guessed, _ = mimetypes.guess_type(a.filename)
    return guessed or "image/png"
