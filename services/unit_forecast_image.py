"""Картинка для /юнит_прогноз (PNG через Pillow) — по результату services/unit_forecast.forecast.
Шрифты, портреты и палитра — общие с /профиль (services/profile_image.py)."""

import io

from PIL import Image, ImageDraw

from services.profile_image import (
    BG, GEAR_BG, GOLD, GREEN, LABEL_BG, LABEL_FG, MUTED, PANEL, PANEL_ALT, RED, RELIC_BG, TEXT,
    _font, _int, _portrait,
)

W, PAD = 760, 16
LABEL_W = 230
ROW_H = 34
PIC = 150

# Шанс крита — с сотыми (как в игре), прочие проценты — с десятыми.
_TWO_DECIMALS = {"Physical Critical Chance", "Special Critical Chance"}


def _dec(n: float, digits: int) -> str:
    return f"{n:.{digits}f}".replace(".", ",")


def _value(name: str, stat: dict, percent: set) -> str:
    v, mods = stat["value"], stat["mods"]
    if name in percent:
        digits = 2 if name in _TWO_DECIMALS else 1
        text = f"{_dec(v, digits)}%"
        if abs(mods) >= 0.005:
            text += f" ({_dec(mods, digits)})"
        return text
    text = _int(v)
    if abs(mods) >= 0.5:
        text += f" ({_int(mods)})"
    return text


def _level_text(level: dict) -> tuple[str, tuple]:
    if level["gear"] is None:  # корабль
        return "", GEAR_BG
    if level["relic"] is not None:
        return f"R{level['relic']}", RELIC_BG
    return f"G{level['gear']}", GEAR_BG


def render(unit_name: str, base_id: str, player_name: str, updated_at: str | None, result: dict) -> bytes:
    cur, proj = result["current"], result["projected"]
    is_ship = result.get("is_ship", False)
    cols = [cur] + ([proj] if proj else [])
    col_w = (W - 2 * PAD - LABEL_W) // len(cols)
    n_rows = (2 if is_ship else 3) + sum(len(g) for g in result["groups"])
    height = PAD + PIC + 20 + ROW_H + n_rows * (ROW_H + 2) + len(result["groups"]) * 6 + 60
    img = Image.new("RGB", (W, height), BG)
    draw = ImageDraw.Draw(img)

    # Шапка: портрет + имя юнита и игрока.
    y = PAD
    portrait = _portrait(base_id, PIC)
    if portrait is not None:
        mask = Image.new("L", (PIC, PIC), 0)
        ImageDraw.Draw(mask).rounded_rectangle((0, 0, PIC - 1, PIC - 1), radius=16, fill=255)
        img.paste(portrait, (PAD, y), Image.composite(portrait.getchannel("A"), mask, mask))
    draw.rounded_rectangle((PAD - 1, y - 1, PAD + PIC, y + PIC), radius=16, outline=GOLD, width=2)
    tx = PAD + PIC + 20
    name_w = W - PAD - tx
    size = 32
    while size > 22 and draw.textlength(unit_name, font=_font(size, True)) > name_w:
        size -= 2
    name_lines = _wrap(draw, unit_name, _font(size, True), name_w)
    if len(name_lines) == 1:
        draw.text((tx, y + 30), name_lines[0], font=_font(size, True), fill=TEXT, anchor="lm")
    else:
        for k, line in enumerate(name_lines):
            draw.text((tx, y + 4 + k * (size + 4)), line, font=_font(size, True), fill=TEXT, anchor="la")
    draw.text((tx, y + 72), player_name, font=_font(22), fill=MUTED, anchor="lm")
    if proj:
        a, b = _level_text(cur["level"])[0], _level_text(proj["level"])[0]
        stars = f"{cur['level']['stars']}★ {a}  →  {proj['level']['stars']}★ {b}".strip()
        if is_ship:
            stars = f"{cur['level']['stars']}★  →  {proj['level']['stars']}★"
        draw.text((tx, y + 110), stars, font=_font(24, True), fill=GOLD, anchor="lm")
        draw.text((tx, y + 140), "при текущем экипаже" if is_ship else "в текущих модулях", font=_font(18), fill=MUTED, anchor="lm")
    y += PIC + 20

    def row(label: str, cells: list, fill, bold_label=False):
        nonlocal y
        draw.rectangle((PAD, y, PAD + LABEL_W - 1, y + ROW_H), fill=LABEL_BG)
        draw.text((PAD + 10, y + ROW_H // 2), label, font=_font(19, bold_label, condensed=True), fill=LABEL_FG, anchor="lm")
        for i, cell in enumerate(cells):
            x = PAD + LABEL_W + i * col_w
            draw.rectangle((x, y, x + col_w - 1, y + ROW_H), fill=fill)
            if cell is None:
                continue
            text, color, badge = cell
            font = _font(20, True, condensed=True)
            if badge:
                tw = int(draw.textlength(text, font=font)) + 18
                draw.rounded_rectangle((x + 10, y + 4, x + 10 + tw, y + ROW_H - 4), radius=10, fill=badge)
                draw.text((x + 10 + tw // 2, y + ROW_H // 2), text, font=font, fill=TEXT, anchor="mm")
            else:
                draw.text((x + 12, y + ROW_H // 2), text, font=font, fill=color, anchor="lm")
        y += ROW_H + 2

    # Заголовки колонок.
    heads = ["Сейчас"] + (["Прогноз"] if proj else [])
    for i, h in enumerate(heads):
        x = PAD + LABEL_W + i * col_w
        draw.text((x + 12, y + ROW_H // 2), h, font=_font(20, True), fill=GOLD, anchor="lm")
    y += ROW_H

    row("Звёзды", [None] * len(cols), PANEL, True)
    for i, c in enumerate(cols):  # 7 звёзд, недостающие серым
        x = PAD + LABEL_W + i * col_w + 12
        font = _font(22, True)
        for k in range(7):
            draw.text((x + k * 24, y - 2 - ROW_H // 2), "★", font=font, fill=GOLD if k < c["level"]["stars"] else (90, 94, 104), anchor="lm")
    if not is_ship:
        row("Гир / Реликвия", [(*_level_text(c["level"])[:1], TEXT, _level_text(c["level"])[1]) for c in cols], PANEL_ALT, True)
    gp_cells = [(_int(cur["gp"]) if cur["gp"] else "—", TEXT, None)]
    if proj:
        gp_cells.append((_int(proj["gp"]) if proj["gp"] else "—", _delta_color(cur["gp"], proj["gp"]), None))
    row("ГМ", gp_cells, PANEL, True)

    percent = result["percent"]
    for gi, group in enumerate(result["groups"]):
        y += 6
        fill = PANEL if gi % 2 == 0 else PANEL_ALT
        for name in group:
            cells = [(_value(name, cur["stats"][name], percent), TEXT, None)]
            if proj:
                cells.append((_value(name, proj["stats"][name], percent),
                              _delta_color(cur["stats"][name]["value"], proj["stats"][name]["value"]), None))
            row(result["labels"][name], cells, fill)

    y += 10
    foot = "Статы — от звёзд и экипажа" if is_ship else "В скобках — вклад модулей"
    if updated_at:
        foot += f"  ·  данные игрока: {updated_at}"
    draw.text((W // 2, y + 12), foot, font=_font(16), fill=MUTED, anchor="mm")
    y += 30

    out = io.BytesIO()
    img.crop((0, 0, W, y + PAD)).save(out, format="PNG", optimize=True)
    return out.getvalue()


def _wrap(draw, text: str, font, width: int) -> list[str]:
    """Не больше двух строк по словам; что не влезло — с многоточием."""
    if draw.textlength(text, font=font) <= width:
        return [text]
    words, first = text.split(), ""
    while words and draw.textlength((first + " " + words[0]).strip(), font=font) <= width:
        first = (first + " " + words.pop(0)).strip()
    rest = " ".join(words)
    while rest and draw.textlength(rest + "…", font=font) > width:
        rest = rest[:-1]
    if rest != " ".join(words):
        rest += "…"
    return [first, rest] if first else [rest]


def _delta_color(before, after):
    if before is None or after is None or abs(after - before) < 1e-6:
        return TEXT
    return GREEN if after > before else RED
