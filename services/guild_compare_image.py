"""Картинка для /вг_соперник — две гильдии в колонках, лучшее значение подсвечено.
Данные — services/guild_compare.py поверх сводок player_profile.summarize_guild; шрифты,
портреты и палитра — общие с /профиль (services/profile_image.py)."""

import io

from PIL import Image, ImageDraw

from services import guild_compare as gc
from services.profile_image import (
    BG, GOLD, HEADER_BOTTOM, HEADER_TOP, LABEL_BG, LABEL_FG, MUTED, PANEL, PANEL_ALT, TEXT,
    _dec, _font, _int, _portrait, compact,
)

W, PAD = 1000, 20
LABEL_W = 360
ROW_H = 34
UNIT_H = 52
BEST_BG = (46, 110, 60)


def _fmt(value, kind: str) -> str:
    if value is None:
        return "—"
    if kind == "compact":
        return compact(value)
    if kind == "dec":
        return _dec(value)
    if kind == "plus":
        return f"+{_int(value)}"
    return _int(value)


def _unit_text(row: dict | None, is_ship: bool) -> str:
    if not row:
        return "нет"
    parts = [f"{row['owners']} шт."]
    if is_ship:
        parts.append(f"7★ {row['stars7']}")
    else:
        parts.append(f"R8+ {row['r8_plus']}")
        if row.get("ultimates"):
            parts.append(f"ульта {row['ultimates']}")
    return " · ".join(parts)


def _better(va, vb) -> int | None:
    """0 — лучше первая, 1 — вторая, None — равны или сравнивать нечего. Кортежи
    сравниваются по порядку (у юнитов: владельцы, потом R8+/7★, потом ульты)."""
    if va is None or vb is None or va == vb:
        return None
    if not isinstance(va, tuple) and abs(va - vb) < 1e-9:
        return None
    return 0 if va > vb else 1


def _unit_key(row: dict | None, is_ship: bool) -> tuple:
    if not row:
        return (0, 0, 0)
    return (row["owners"], row["stars7"] if is_ship else row["r8_plus"], row.get("ultimates") or 0)


def render(a: dict, b: dict, note: str | None = None) -> bytes:
    unit_sections = [(t, gc.unit_rows(a, b, k)) for t, k in
                     (("Галактические легенды", "gls"), ("Мета", "meta_units"), ("Флот", "fleet"))]
    n_metric = sum(len(rows) for _t, rows in gc.SECTIONS)
    n_units = sum(len(rows) for _t, rows in unit_sections)
    height = PAD + 120 + (len(gc.SECTIONS) + len(unit_sections)) * 44 + n_metric * (ROW_H + 2) + n_units * (UNIT_H + 2) + 80
    img = Image.new("RGB", (W, height), BG)
    draw = ImageDraw.Draw(img)
    col_w = (W - 2 * PAD - LABEL_W) // 2
    y = PAD

    def fit(text, font, width):
        if draw.textlength(text, font=font) <= width:
            return text
        while text and draw.textlength(text + "…", font=font) > width:
            text = text[:-1]
        return text + "…"

    # Шапка: «Мы — против — Они».
    h = 96
    for i in range(h):
        t = i / h
        draw.line((PAD, y + i, W - PAD, y + i), fill=(int(70 - 50 * t), int(18 - 8 * t), int(16 - 6 * t)))
    draw.rectangle((PAD, y, W - PAD, y + h), outline=GOLD, width=2)
    draw.text((W // 2, y + 24), "Сравнение гильдий", font=_font(20), fill=MUTED, anchor="mm")
    for i, g in enumerate((a, b)):
        x = PAD + LABEL_W + i * col_w + col_w // 2
        draw.text((x, y + 60), fit(g["name"], _font(26, True), col_w - 16), font=_font(26, True), fill=TEXT, anchor="mm")
    draw.text((PAD + LABEL_W // 2, y + 60), "против", font=_font(20, True), fill=GOLD, anchor="mm")
    y += h + 12

    def section(title: str):
        nonlocal y
        sh = 36
        for i in range(sh):
            t = i / sh
            draw.line((PAD, y + i, W - PAD, y + i), fill=tuple(int(p + (q - p) * t) for p, q in zip(HEADER_TOP, HEADER_BOTTOM)))
        draw.text((W // 2, y + sh // 2), title, font=_font(20, True), fill=LABEL_FG, anchor="mm")
        y += sh + 6

    def cells(texts: list[str], best: int | None, row_h: int, fill):
        for i, text in enumerate(texts):
            x = PAD + LABEL_W + i * col_w
            draw.rectangle((x, y, x + col_w - 2, y + row_h), fill=BEST_BG if best == i else fill)
            font = _font(19, True, condensed=True)
            draw.text((x + col_w // 2, y + row_h // 2), fit(text, font, col_w - 16), font=font, fill=TEXT, anchor="mm")

    for title, rows in gc.SECTIONS:
        section(title)
        for k, (label, path, kind, higher) in enumerate(rows):
            va, vb = gc.value_at(a, path), gc.value_at(b, path)
            best = _better(va, vb) if higher else None
            draw.rectangle((PAD, y, PAD + LABEL_W - 2, y + ROW_H), fill=LABEL_BG)
            draw.text((PAD + LABEL_W - 12, y + ROW_H // 2), label, font=_font(18, condensed=True), fill=LABEL_FG, anchor="rm")
            cells([_fmt(va, kind), _fmt(vb, kind)], best, ROW_H, PANEL if k % 2 == 0 else PANEL_ALT)
            y += ROW_H + 2
        y += 6

    for title, rows in unit_sections:
        if not rows:
            continue
        section(f"{title} — владельцы")
        for k, r in enumerate(rows):
            fill = PANEL if k % 2 == 0 else PANEL_ALT
            draw.rectangle((PAD, y, PAD + LABEL_W - 2, y + UNIT_H), fill=fill)
            pic = UNIT_H - 8
            portrait = _portrait(r["base_id"], pic)
            if portrait is not None:
                mask = Image.new("L", (pic, pic), 0)
                ImageDraw.Draw(mask).rounded_rectangle((0, 0, pic - 1, pic - 1), radius=8, fill=255)
                img.paste(portrait, (PAD + 6, y + 4), Image.composite(portrait.getchannel("A"), mask, mask))
            font = _font(18, True, condensed=True)
            draw.text((PAD + pic + 16, y + UNIT_H // 2), fit(r["name"], font, LABEL_W - pic - 26), font=font, fill=TEXT, anchor="lm")
            best = _better(_unit_key(r["a"], r["is_ship"]), _unit_key(r["b"], r["is_ship"]))
            cells([_unit_text(r["a"], r["is_ship"]), _unit_text(r["b"], r["is_ship"])], best, UNIT_H, fill)
            y += UNIT_H + 2
        y += 6

    foot = "Зелёным — у кого больше (юниты: владельцы → R8+/7★ → ульты) · мы — ежечасный синк, соперник — данные из игры сейчас"
    if note:
        foot += f" · {note}"
    draw.text((W // 2, y + 14), fit(foot, _font(15), W - 2 * PAD), font=_font(15), fill=MUTED, anchor="mm")
    y += 34
    out = io.BytesIO()
    img.crop((0, 0, W, y + 10)).save(out, format="PNG", optimize=True)
    return out.getvalue()
