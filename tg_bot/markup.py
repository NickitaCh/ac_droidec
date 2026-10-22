"""Отчёты из общих функций cogs/* собраны в разметке Discord (```таблица```, **жирный**,
_курсив_, `код`). Здесь — перевод в HTML-разметку Telegram и нарезка на сообщения
(лимит 4096 символов) без разрыва таблиц."""

import html
import re

LIMIT = 3900  # запас под теги, которые добавит перевод

_CUSTOM_EMOJI_RE = re.compile(r"<a?:(\w+):\d+>")
_MENTION_RE = re.compile(r"<@[!&]?(?:tg:)?\d+>")
_INLINE_CODE_RE = re.compile(r"`([^`\n]+)`")
_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")
_UNDERLINE_RE = re.compile(r"__(.+?)__")
_STRIKE_RE = re.compile(r"~~(.+?)~~")
# Курсив — только _целые слова_, чтобы не задеть base_id вроде GLLEIA_X или snake_case.
_ITALIC_RE = re.compile(r"(?<![\w*])[_*](?![\s_*])(.+?)(?<![\s_*])[_*](?![\w*])")
_HEADER_RE = re.compile(r"^#{1,3} +(.+)$", re.M)
_SUBTEXT_RE = re.compile(r"^-# +", re.M)


def _inline(text: str) -> str:
    text = _CUSTOM_EMOJI_RE.sub("", text)
    text = _MENTION_RE.sub("", text)
    codes = []

    def keep_code(m):
        codes.append(f"<code>{html.escape(m.group(1), quote=False)}</code>")
        return f"\x00{len(codes) - 1}\x00"

    text = _INLINE_CODE_RE.sub(keep_code, text)
    text = html.escape(text, quote=False)
    text = _SUBTEXT_RE.sub("", text)
    text = _HEADER_RE.sub(r"<b>\1</b>", text)
    text = _BOLD_RE.sub(r"<b>\1</b>", text)
    text = _UNDERLINE_RE.sub(r"<u>\1</u>", text)
    text = _STRIKE_RE.sub(r"<s>\1</s>", text)
    text = _ITALIC_RE.sub(r"<i>\1</i>", text)
    return re.sub("\x00(\\d+)\x00", lambda m: codes[int(m.group(1))], text)


def md_to_html(text: str) -> str:
    parts = text.split("```")
    out = []
    for i, part in enumerate(parts):
        if i % 2:  # внутри ```...```
            body = part.split("\n", 1)[1] if "\n" in part and part.split("\n", 1)[0].strip().isalpha() else part
            out.append(f"<pre>{html.escape(body.strip(chr(10)), quote=False)}</pre>")
        else:
            out.append(_inline(part))
    return "".join(out).strip()


def chunk_md(lines: list[str], limit: int = LIMIT) -> list[str]:
    """Режет строки (Discord-разметка) на куски ≤ limit; таблица ``` не рвётся — если
    кусок кончается внутри неё, блок закрывается и открывается заново в следующем."""
    chunks, cur, size, in_code = [], [], 0, False
    for raw in lines:
        for line in str(raw).split("\n"):
            add = len(line) + 1
            if cur and size + add > limit:
                if in_code:
                    cur.append("```")
                chunks.append("\n".join(cur))
                cur, size = (["```"], 4) if in_code else ([], 0)
            cur.append(line)
            size += add
            if line.strip().startswith("```") and line.strip() != "``````":
                in_code = not in_code
    if cur:
        chunks.append("\n".join(cur))
    return [c for c in chunks if c.strip()]
