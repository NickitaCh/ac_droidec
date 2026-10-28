"""Discord-разметка → HTML для предпросмотра на сайте «как будет выглядеть в Discord»
(редактор ордера ВИ, web /tb/order). Покрывает то, что реально встречается в ордерах:
# / ## / ### заголовки, -# подпись, > цитаты, ``` блоки, `код`, **жирный**, *курсив*/_курсив_,
__подчёркнутый__, ~~зачёркнутый~~, ||спойлер||, ссылки, упоминания ролей/каналов/людей и
свои эмодзи сервера (<:имя:id> — картинка с CDN Discord). Всё экранируется до разметки."""

import html
import re

_CUSTOM_EMOJI_RE = re.compile(r"&lt;(a?):(\w+):(\d+)&gt;")
_ROLE_RE = re.compile(r"&lt;@&amp;(\d+)&gt;")
_USER_RE = re.compile(r"&lt;@!?(\d+)&gt;")
_CHANNEL_RE = re.compile(r"&lt;#(\d+)&gt;")
_CODE_RE = re.compile(r"`([^`\n]+)`")
_URL_RE = re.compile(r"(https?://[^\s<]+)")
_INLINE = [
    (re.compile(r"\*\*(.+?)\*\*"), r"<strong>\1</strong>"),
    (re.compile(r"__(.+?)__"), r"<u>\1</u>"),
    (re.compile(r"~~(.+?)~~"), r"<s>\1</s>"),
    (re.compile(r"\|\|(.+?)\|\|"), r'<span class="dc-spoiler">\1</span>'),
    (re.compile(r"(?<![\w*])\*(?![\s*])(.+?)(?<![\s*])\*(?![\w*])"), r"<em>\1</em>"),
    (re.compile(r"(?<![\w_])_(?![\s_])(.+?)(?<![\s_])_(?![\w_])"), r"<em>\1</em>"),
]


def _inline(text: str, names: dict) -> str:
    codes = []

    def keep(m):
        codes.append(f"<code>{m.group(1)}</code>")
        return f"\x00{len(codes) - 1}\x00"

    text = _CODE_RE.sub(keep, text)
    text = _URL_RE.sub(r'<a href="\1" target="_blank" rel="noopener">\1</a>', text)
    for rx, rep in _INLINE:
        text = rx.sub(rep, text)
    text = _CUSTOM_EMOJI_RE.sub(
        lambda m: f'<img class="dc-emoji" alt=":{m.group(2)}:" title=":{m.group(2)}:" '
                  f'src="https://cdn.discordapp.com/emojis/{m.group(3)}.{"gif" if m.group(1) else "webp"}?size=48">', text)
    text = _ROLE_RE.sub(lambda m: f'<span class="dc-mention">@{html.escape(names.get(m.group(1), "роль"))}</span>', text)
    text = _USER_RE.sub(lambda m: f'<span class="dc-mention">@{html.escape(names.get(m.group(1), "участник"))}</span>', text)
    text = _CHANNEL_RE.sub(lambda m: f'<span class="dc-mention">#{html.escape(names.get(m.group(1), "канал"))}</span>', text)
    return re.sub("\x00(\\d+)\x00", lambda m: codes[int(m.group(1))], text)


def render(text: str, names: dict | None = None) -> str:
    """HTML одного сообщения. names — {id: имя} для упоминаний (роль ордера и т.п.)."""
    names = names or {}
    out, quote = [], []
    parts = text.split("```")

    def flush_quote():
        if quote:
            out.append('<blockquote class="dc-quote">' + "".join(quote) + "</blockquote>")
            quote.clear()

    for i, part in enumerate(parts):
        if i % 2:  # блок кода
            flush_quote()
            body = part.split("\n", 1)[1] if "\n" in part and part.split("\n", 1)[0].strip().isalpha() else part
            out.append(f'<pre class="dc-pre">{html.escape(body.strip(chr(10)))}</pre>')
            continue
        for line in html.escape(part, quote=False).split("\n"):
            target = out
            if line.startswith("&gt; ") or line == "&gt;":
                line = line[5:]
                target = quote
            else:
                flush_quote()
            m = re.match(r"^(#{1,3}) +(.+)$", line)
            if m:
                target.append(f'<div class="dc-h{len(m.group(1))}">{_inline(m.group(2), names)}</div>')
            elif line.startswith("-# "):
                target.append(f'<div class="dc-sub">{_inline(line[3:], names)}</div>')
            elif line.strip():
                target.append(f"<div>{_inline(line, names)}</div>")
            else:
                target.append('<div class="dc-empty"></div>')
        flush_quote()
    return "".join(out)
