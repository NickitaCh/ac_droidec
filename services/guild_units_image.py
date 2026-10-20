"""Картинка готовности гильдии к цели «Руководства» (как WookieeBot `guild ready journey`) —
по результату services/guild_units.readiness. Строка — игрок (процент, ник, уровень самой
цели, если уже открыта), колонки — требуемые юниты с порогом над портретом. Фон строки —
от зелёного (готов) к красному. Шрифты и портреты — общие с /профиль."""

import io
import math

from PIL import Image, ImageDraw

from services.profile_image import BG, GOLD, MUTED, TEXT, _font, _portrait

PAD = 12
PCT_W, NAME_W, GUIDE_W, CELL_W = 64, 230, 90, 86
HEAD_H, ROW_H = 118, 28
PIC = 64

MET_FG = (236, 236, 240)
MISS_FG = (255, 170, 120)
NONE_FG = (140, 144, 154)


def _row_color(pct: float):
    """100% — зелёный, ниже — к оливковому и красно-бурому, как у Wookiee."""
    t = max(0.0, min(1.0, pct / 100))
    if t >= 0.999:
        return (22, 74, 30)
    if t >= 0.5:
        k = (t - 0.5) / 0.5
        return (int(92 - 50 * k), int(84 - 6 * k), int(20 + 6 * k))
    k = t / 0.5
    return (int(96 - 4 * k), int(32 + 52 * k), int(26 - 6 * k))


def render(r: dict, guild_name: str, updated_note: str | None = None) -> bytes:
    units, players = r["units"], r["players"]
    width = PAD * 2 + PCT_W + NAME_W + GUIDE_W + CELL_W * len(units)
    title_h = 64
    height = PAD + title_h + HEAD_H + ROW_H * len(players) + 100
    img = Image.new("RGB", (width, height), BG)
    draw = ImageDraw.Draw(img)

    ready = sum(1 for p in players if p["ready"])
    y = PAD
    draw.text((width // 2, y + 16), f"Готовность к «{r['guide']['name']}» — {guild_name}", font=_font(22, True), fill=TEXT, anchor="mm")
    draw.text((width // 2, y + 44), f"Готовы: {ready} · Не готовы: {len(players) - ready}", font=_font(18), fill=GOLD, anchor="mm")
    y += title_h

    # Шапка: цель, затем требуемые юниты с порогом.
    gx = PAD + PCT_W + NAME_W
    portrait = _portrait(r["guide"]["base_id"], PIC)
    if portrait is not None:
        img.paste(portrait, (gx + (GUIDE_W - PIC) // 2, y + HEAD_H - PIC - 8), portrait)
    draw.text((gx + GUIDE_W // 2, y + 22), "Цель", font=_font(16, True), fill=GOLD, anchor="mm")
    for i, u in enumerate(units):
        x = gx + GUIDE_W + i * CELL_W
        draw.text((x + CELL_W // 2, y + 12), "Нужно", font=_font(14, True), fill=(230, 80, 80), anchor="mm")
        draw.text((x + CELL_W // 2, y + 32), u["label"], font=_font(15, True), fill=TEXT, anchor="mm")
        p = _portrait(u["base_id"], PIC)
        if p is not None:
            img.paste(p, (x + (CELL_W - PIC) // 2, y + HEAD_H - PIC - 8), p)
    draw.line((gx + GUIDE_W - 3, y + 6, gx + GUIDE_W - 3, y + HEAD_H + ROW_H * len(players)), fill=(200, 200, 200), width=2)
    y += HEAD_H

    for p in players:
        draw.rectangle((PAD, y, width - PAD, y + ROW_H - 1), fill=_row_color(p["pct"]))
        cy = y + ROW_H // 2
        draw.text((PAD + 6, cy), f"{math.floor(p['pct'] + 1e-9)}%", font=_font(16), fill=TEXT, anchor="lm")
        name_font = _font(16)
        name = p["name"]
        while name and draw.textlength(name, font=name_font) > NAME_W - 10:
            name = name[:-1]
        draw.text((PAD + PCT_W, cy), name, font=name_font, fill=TEXT, anchor="lm")
        draw.text((gx + GUIDE_W // 2, cy), p["guide_level"] or "—", font=_font(16), fill=TEXT, anchor="mm")
        for i, c in enumerate(p["cells"]):
            x = gx + GUIDE_W + i * CELL_W
            color = MET_FG if c["met"] else (NONE_FG if c["text"] == "—" else MISS_FG)
            draw.text((x + CELL_W // 2, cy), c["text"], font=_font(16, c["met"] is False and c["text"] != "—"), fill=color, anchor="mm")
        y += ROW_H

    notes = ["Оранжевым — ещё не дотягивает до нужного", "% — средний прогресс по всем юнитам"]
    if updated_note:
        notes.append(updated_note)
    lines = [" · ".join(notes)]
    if draw.textlength(lines[0], font=_font(14)) > width - 2 * PAD:
        lines = notes
    for line in lines:
        draw.text((width // 2, y + 16), line, font=_font(14), fill=MUTED, anchor="mm")
        y += 20
    out = io.BytesIO()
    img.crop((0, 0, width, y + 20)).save(out, format="PNG", optimize=True)
    return out.getvalue()
