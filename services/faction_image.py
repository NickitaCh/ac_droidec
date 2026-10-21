"""Картинка /фракция (как HotBot `/faction`): плитка на каждого персонажа фракции — звёзды
дугой над портретом, кольцо по цвету снаряжения, внизу реликвия или гир, слева зеты,
справа омикроны, золотая рамка у ГЛ с абсолютной способностью; кого нет — серым в конце.
Данные — services/faction.build, шрифты и портреты — общие с /профиль."""

import io
import math

from PIL import Image, ImageDraw

from services.profile_image import BG, GOLD, MUTED, TEXT, _font, _portrait
from services.toon_image import _badge as _badge_icon

PAD = 14
COLS = 6
TILE_W, TILE_H, GAP = 150, 198, 8
PIC = 96
SKILL_ICON = 34
HEAD_H = 76

# Цвет кольца — как рамка снаряжения в игре.
GEAR_RING = [(13, (90, 170, 255)), (12, (230, 184, 92)), (7, (170, 110, 240)), (4, (70, 140, 230)),
             (2, (90, 200, 90)), (1, (170, 174, 184))]
RELIC_BG, GEAR_BG = (140, 34, 34), (40, 70, 120)
ZETA_BG, OMICRON_BG = (110, 50, 170), (30, 110, 170)
STAR_OFF = (70, 72, 80)
TILE_BG, MISSING_BG = (12, 13, 16), (26, 27, 31)


def _ring_color(gear: int):
    return next(color for tier, color in GEAR_RING if gear >= tier)


def _badge(draw, cx: int, cy: int, r: int, fill, text: str):
    draw.ellipse((cx - r, cy - r, cx + r, cy + r), fill=fill, outline=(225, 225, 230), width=2)
    draw.text((cx, cy), text, font=_font(14 if len(text) > 2 else 15, True), fill=TEXT, anchor="mm")


def _skill_icon(img, draw, name: str, cx: int, cy: int, count: int):
    """Иконка зеты/омикрона (та же, что в /юнит) с числом поверх, как в игре; нет файла — кружок."""
    icon = _badge_icon(name, SKILL_ICON)
    if icon is None:
        _badge(draw, cx, cy, 12, ZETA_BG if name == "zeta" else OMICRON_BG, str(count))
        return
    img.paste(icon, (cx - SKILL_ICON // 2, cy - SKILL_ICON // 2), icon)
    draw.text((cx, cy + 1), str(count), font=_font(15, True), fill=TEXT, anchor="mm",
              stroke_width=2, stroke_fill=(10, 10, 14))


def _wrap(draw, text: str, font, width: int) -> list[str]:
    lines, cur = [], ""
    for word in text.split():
        if draw.textlength((cur + " " + word).strip(), font=font) <= width:
            cur = (cur + " " + word).strip()
        else:
            if cur:
                lines.append(cur)
            cur = word
    lines.append(cur)
    if len(lines) > 2:  # третья строка не влезает — обрезаем вторую с многоточием
        tail = " ".join(lines[1:])
        while tail and draw.textlength(tail + "…", font=font) > width:
            tail = tail[:-1]
        lines = [lines[0], tail + "…"]
    return lines


def _tile(img, draw, x: int, y: int, u: dict):
    owned = u["owned"]
    frame = GOLD if owned and u["gl"] and u["ultimate"] else (60, 62, 70)
    draw.rounded_rectangle((x, y, x + TILE_W, y + TILE_H), radius=8, fill=TILE_BG if owned else MISSING_BG,
                           outline=frame, width=3 if frame == GOLD else 1)
    cx = x + TILE_W // 2
    py = y + 30
    # Звёзды дугой над портретом.
    stars = u["stars"] if owned else 0
    for k in range(7):
        angle = math.radians(-90 + (k - 3) * 22)
        sx, sy = cx + 64 * math.cos(angle), py + PIC // 2 + 64 * math.sin(angle)
        draw.text((sx, sy), "★", font=_font(15, True), fill=GOLD if k < stars else STAR_OFF, anchor="mm")

    portrait = _portrait(u["base_id"], PIC)
    px = cx - PIC // 2
    if portrait is not None:
        if not owned:
            gray = portrait.convert("LA").convert("RGBA")
            gray.putalpha(portrait.getchannel("A").point(lambda v: v * 4 // 10))
            portrait = gray
        mask = Image.new("L", (PIC, PIC), 0)
        ImageDraw.Draw(mask).ellipse((0, 0, PIC - 1, PIC - 1), fill=255)
        img.paste(portrait, (px, py), Image.composite(portrait.getchannel("A"), mask, mask))
    if owned:
        draw.ellipse((px - 3, py - 3, px + PIC + 2, py + PIC + 2), outline=_ring_color(u["gear"]), width=4)
        by = py + PIC - 2
        if u["relic"] is not None:
            _badge(draw, cx, by, 16, RELIC_BG, f"R{u['relic']}")
        else:
            _badge(draw, cx, by, 16, GEAR_BG, f"G{u['gear']}")
        if u["zetas"]:
            _skill_icon(img, draw, "zeta", px - 2, by - 8, u["zetas"])
        if u["omicrons"]:
            _skill_icon(img, draw, "omicron", px + PIC + 2, by - 8, u["omicrons"])
    else:
        draw.text((cx, py + PIC - 2), "нет", font=_font(15, True), fill=MUTED, anchor="mm")

    nf = _font(14, True, condensed=True)
    ny = py + PIC + 26
    for k, line in enumerate(_wrap(draw, u["name"], nf, TILE_W - 12)):
        draw.text((cx, ny + k * 17), line, font=nf, fill=TEXT if owned else MUTED, anchor="mm")


def render(r: dict, player_name: str, updated_note: str | None = None) -> bytes:
    units = r["units"]
    cols = min(COLS, max(1, len(units)))
    rows = (len(units) + cols - 1) // cols
    width = max(PAD * 2 + cols * TILE_W + (cols - 1) * GAP, 560)
    height = PAD + HEAD_H + rows * (TILE_H + GAP) + 80
    img = Image.new("RGB", (width, height), BG)
    draw = ImageDraw.Draw(img)

    title = f"{player_name} — {r['name']}"
    tf = _font(21, True)
    while draw.textlength(title, font=tf) > width - 2 * PAD and len(title) > 10:
        title = title[:-2]
    draw.text((width // 2, PAD + 14), title, font=tf, fill=TEXT, anchor="mm")
    sub = f"Открыто {r['owned']} из {r['total']} · 7★: {r['seven']} · R5+: {r['relic5']}"
    draw.text((width // 2, PAD + 46), sub, font=_font(17), fill=GOLD, anchor="mm")

    y0 = PAD + HEAD_H
    for i, u in enumerate(units):
        row, col = divmod(i, cols)
        _tile(img, draw, PAD + col * (TILE_W + GAP), y0 + row * (TILE_H + GAP), u)

    y = y0 + rows * (TILE_H + GAP) + 4
    notes = ["Золотая рамка — ГЛ с абсолютной способностью"]
    if updated_note:
        notes.append(updated_note)
    for line in notes:
        lf = _font(13)
        while draw.textlength(line, font=lf) > width - 2 * PAD and len(line) > 10:
            line = line[:-2]
        draw.text((width // 2, y + 10), line, font=lf, fill=MUTED, anchor="mm")
        y += 20
    out = io.BytesIO()
    img.crop((0, 0, width, y + 10)).save(out, format="PNG", optimize=True)
    return out.getvalue()
