"""Картинка для /юнит (PNG через Pillow) — по результату services/toon.build.
Строка — персонаж, колонка — игрок. Шрифты, портреты и палитра — общие с /профиль
(services/profile_image.py); иконки модов, сетов, зет и омикронов — те же файлы, что у
сайта (web/static/img/game), эмблема сета красится в цвет тира мода, как в игре."""

import io
import math
from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageDraw

from services import toon
from services.profile_image import (
    BG, GEAR_BG, GOLD, LABEL_FG, MUTED, PANEL, PANEL_ALT, RELIC_BG, TEXT, _dec, _font, _int, _portrait,
)
from services.unit_forecast import STAT_LABELS

GAME_IMG = Path(__file__).resolve().parent.parent / "web" / "static" / "img" / "game"

CARD_W, LEFT_W = 578, 300
PAD, GAP = 12, 8
TITLE_H = 34
STAT_H = 25
MOD_H = 84
ABIL_H = 26
PIC = 190
PIC_TOP = 44  # место над портретом под дугу звёзд

BEST_BG = (46, 110, 60)
MISSING_FG = (120, 124, 134)
CROSS = (220, 80, 80)
LEVEL_BG = (36, 58, 82)

# Цвет эмблемы сета по тиру мода E..A (как .mod-tier-N в style.css).
TIER_COLORS = {1: (216, 221, 227), 2: (152, 253, 51), 3: (0, 189, 254), 4: (178, 123, 255), 5: (255, 204, 51)}
# Центр эмблемы сета на форме слота: (top, left, width) в долях — как .mod-shape-* в style.css;
# размер у всех слотов один (SET_SIDE), иначе эмблемы разного размера (замечание из треда 2026-10-21).
SET_SIDE = 0.30
SET_POS = {
    "square": (0.25, 0.40, 0.36), "arrow": (0.23, 0.49, 0.27), "diamond": (0.35, 0.35, 0.32),
    "triangle": (0.46, 0.38, 0.25), "circle": (0.35, 0.35, 0.32), "cross": (0.37, 0.35, 0.29),
}
# Число прокачек вторички: 5 и 4 — заметнее, как у HotBot.
ROLL_COLORS = {5: (255, 110, 90), 4: (255, 190, 80)}

_TWO_DECIMALS = {"Physical Critical Chance", "Special Critical Chance"}
_PERCENT = {"Physical Critical Chance", "Special Critical Chance", "Critical Damage", "Potency", "Tenacity", "Armor", "Resistance"}
_NO_MODS = {"Armor Penetration", "Resistance Penetration"}



def _stat_text(name: str, stat: dict, speed_secondary: int) -> str:
    v, mods = stat["value"], stat["mods"]
    if name in _PERCENT:
        digits = 2 if name in _TWO_DECIMALS else 1
        text = f"{_dec(v, digits)}%"
        if abs(mods) >= 0.005 and name not in _NO_MODS:
            text += f" (+{_dec(mods, digits)})"
        return text
    text = _int(v)
    if abs(mods) >= 0.5 and name not in _NO_MODS:
        text += f" (+{_int(mods)}" + (f" [{speed_secondary}]" if name == "Speed" else "") + ")"
    return text


def _mod_value(stat: dict) -> str:
    if stat["is_percent"]:
        return f"+{_dec(stat['value'], 2)}%"
    return f"+{_int(stat['value'])}"


@lru_cache(maxsize=None)
def _image(rel_path: str):
    try:
        return Image.open(GAME_IMG / rel_path).convert("RGBA")
    except OSError:
        return None


@lru_cache(maxsize=256)
def _mod_icon(slot_key: str, set_id: int, gold: bool, tier: int, size: int):
    shape = _image(f"mods/shape-{slot_key}{'-gold' if gold else ''}.webp")
    if shape is None:
        return None
    icon = shape.resize((size, size), Image.LANCZOS)
    emblem = _image(f"sets/set-{set_id}.webp")
    if emblem is not None:
        top, left, width = SET_POS.get(slot_key, SET_POS["diamond"])
        side = max(1, int(size * SET_SIDE))
        cx, cy = size * (left + width / 2), size * (top + width / 2)
        alpha = emblem.getchannel("A").resize((side, side), Image.LANCZOS)
        colored = Image.new("RGBA", (side, side), TIER_COLORS.get(tier, TIER_COLORS[5]) + (255,))
        icon.paste(colored, (int(cx - side / 2), int(cy - side / 2)), alpha)
    return icon


@lru_cache(maxsize=8)
def _badge(name: str, size: int):
    img = _image(f"badges/{name}.png")
    return img.resize((size, size), Image.LANCZOS) if img is not None else None


def _fit(draw, text: str, font, width: int) -> str:
    if draw.textlength(text, font=font) <= width:
        return text
    while text and draw.textlength(text + "…", font=font) > width:
        text = text[:-1]
    return text + "…"


def _card_height(card: dict) -> int:
    if card["missing"]:
        return TITLE_H + 120
    left = PIC_TOP + PIC + 26 + 32 + len(toon.STATS) * STAT_H
    right = 6 * MOD_H + len(card["abilities"]) * ABIL_H
    return TITLE_H + max(left, right) + 6


def _circle(draw, cx: int, cy: int, r: int, fill, text: str, font):
    draw.ellipse((cx - r, cy - r, cx + r, cy + r), fill=fill, outline=(15, 16, 20), width=2)
    draw.text((cx, cy), text, font=font, fill=TEXT, anchor="mm")


def _draw_card(img, draw, x0: int, y0: int, h: int, unit_name: str, base_id: str, card: dict, best: dict, col: int):
    draw.rectangle((x0, y0, x0 + CARD_W - 1, y0 + h - 1), fill=PANEL)
    draw.rectangle((x0, y0, x0 + CARD_W - 1, y0 + TITLE_H - 1), fill=(201, 164, 106))
    title = _fit(draw, f"{card['player_name']} — {unit_name}", _font(19, True, condensed=True), CARD_W - 20)
    draw.text((x0 + CARD_W // 2, y0 + TITLE_H // 2), title, font=_font(19, True, condensed=True), fill=LABEL_FG, anchor="mm")
    y = y0 + TITLE_H
    if card["missing"]:
        draw.text((x0 + CARD_W // 2, y + 60), "Персонажа нет", font=_font(22, True), fill=MISSING_FG, anchor="mm")
        return

    # --- Левая колонка: квадратный блок портрета (звёзды дугой, значки по нижнему краю,
    # как у HotBot /toon), под ним ГМ и статы.
    lx = x0
    cx = lx + LEFT_W // 2
    py = y + PIC_TOP
    portrait = _portrait(base_id, PIC)
    px = cx - PIC // 2
    if portrait is not None:
        mask = Image.new("L", (PIC, PIC), 0)
        ImageDraw.Draw(mask).ellipse((0, 0, PIC - 1, PIC - 1), fill=255)
        img.paste(portrait, (px, py), Image.composite(portrait.getchannel("A"), mask, mask))
    draw.ellipse((px - 2, py - 2, px + PIC + 1, py + PIC + 1), outline=GOLD, width=3)
    arc_r, arc_cy = PIC // 2 + 22, py + PIC // 2
    for k in range(7):
        angle = math.radians(-90 + (k - 3) * 21)
        draw.text((cx + arc_r * math.cos(angle), arc_cy + arc_r * math.sin(angle)), "★", font=_font(28, True),
                  fill=GOLD if k < card["level"]["stars"] else (80, 84, 94), anchor="mm")

    level = card["level"]
    badge_y = py + PIC - 8
    font_b = _font(20, True)
    _circle(draw, cx - 36, badge_y, 24, LEVEL_BG, str(card["unit_level"]), font_b)
    if level["relic"] is not None:
        _circle(draw, cx + 36, badge_y, 24, RELIC_BG, f"R{level['relic']}", font_b)
    else:
        _circle(draw, cx + 36, badge_y, 24, GEAR_BG, f"G{level['gear']}", font_b)
    # Зеты слева, омикроны справа — иконкой с числом поверх, как в игре.
    for kind, count, bx in (("zeta", card["zetas"], cx - 104), ("omicron", card["omicrons"], cx + 104)):
        icon = _badge(kind, 46)
        if not count or icon is None:
            continue
        img.paste(icon, (bx - 23, badge_y - 23), icon)
        draw.text((bx, badge_y + 1), str(count), font=font_b, fill=TEXT, anchor="mm", stroke_width=2, stroke_fill=(10, 10, 14))

    y_stats = py + PIC + 26
    draw.rectangle((lx, y_stats, lx + LEFT_W - 1, y_stats + 30), fill=(120, 30, 30))
    draw.text((lx + LEFT_W // 2, y_stats + 15), f"ГМ {_int(card['gp'])}" if card["gp"] else "ГМ —",
              font=_font(19, True), fill=TEXT, anchor="mm")
    y_stats += 32
    label_w = 140
    for i, name in enumerate(toon.STATS):
        ry = y_stats + i * STAT_H
        fill = PANEL_ALT if i % 2 else PANEL
        draw.rectangle((lx, ry, lx + LEFT_W - 1, ry + STAT_H - 1), fill=fill)
        if best.get(name) == col:
            draw.rectangle((lx + label_w, ry, lx + LEFT_W - 1, ry + STAT_H - 1), fill=BEST_BG)
        draw.text((lx + label_w - 6, ry + STAT_H // 2), _fit(draw, STAT_LABELS[name], _font(15, condensed=True), label_w - 16),
                  font=_font(15, condensed=True), fill=MUTED, anchor="rm")
        text = _fit(draw, _stat_text(name, card["stats"][name], card["speed_secondary"]),
                    _font(15, True, condensed=True), LEFT_W - label_w - 10)
        draw.text((lx + label_w + 6, ry + STAT_H // 2), text, font=_font(15, True, condensed=True), fill=TEXT, anchor="lm")

    # --- Правая колонка: моды, способности.
    rx = x0 + LEFT_W + 4
    rw = CARD_W - LEFT_W - 4
    draw.rectangle((rx - 4, y, rx - 3, y0 + h - 1), fill=BG)
    for k, mod in enumerate(card["mods"]):
        my = y + k * MOD_H
        draw.rectangle((rx, my, rx + rw - 1, my + MOD_H - 2), fill=PANEL_ALT if k % 2 else PANEL)
        if mod is None:
            draw.text((rx + 40, my + MOD_H // 2), "—", font=_font(18, True), fill=MISSING_FG, anchor="mm")
            draw.text((rx + 84, my + MOD_H // 2), "Пустой слот", font=_font(15), fill=MISSING_FG, anchor="lm")
            continue
        icon = _mod_icon(mod["slot_key"], mod["set_id"], (mod["rarity"] or 0) >= 6, mod["tier"] or 5, 58)
        if icon is not None:
            img.paste(icon, (rx + 8, my + 18), icon)
        dots = "•" * (mod["rarity"] or 0)
        draw.text((rx + 37, my + 9), dots, font=_font(14, True), fill=GOLD if (mod["rarity"] or 0) >= 6 else TEXT, anchor="mm")
        if mod.get("level") and mod["level"] < 15:
            draw.text((rx + 37, my + MOD_H - 8), f"ур. {mod['level']}", font=_font(12), fill=MISSING_FG, anchor="mm")
        tx = rx + 68
        line_h = 16
        primary = mod["primary"]
        if primary:
            draw.text((tx + 22, my + 3), f"{_mod_value(primary)} {toon.MOD_STAT_RU.get(primary['stat_id'], primary['name'])}",
                      font=_font(14, True, condensed=True), fill=GOLD, anchor="la")
        for j, sec in enumerate(mod["secondaries"][:4]):
            sy = my + 3 + (j + 1) * line_h
            rolls = sec.get("rolls") or 1
            color = ROLL_COLORS.get(rolls, TEXT)
            draw.text((tx + 8, sy), str(rolls), font=_font(14, True, condensed=True), fill=color, anchor="ma")
            label = f"{_mod_value(sec)} {toon.MOD_STAT_RU.get(sec['stat_id'], sec['name'])}"
            draw.text((tx + 22, sy), _fit(draw, label, _font(14, True, condensed=True), rw - (tx + 22 - rx) - 2), font=_font(14, True, condensed=True),
                      fill=color if rolls >= 4 else TEXT, anchor="la")

    ay = y + 6 * MOD_H
    for k, a in enumerate(card["abilities"]):
        ry = ay + k * ABIL_H
        draw.rectangle((rx, ry, rx + rw - 1, ry + ABIL_H - 2), fill=(26, 28, 34))
        applied = a["zeta"] or a["omicron"]
        ix = rx + 6
        for kind, has_tier in (("zeta", a["zeta_tier"] is not None), ("omicron", a["omicron_tier"] is not None)):
            if not has_tier:
                continue
            badge = _badge(kind, 22)
            if badge is not None:
                if not a[kind]:
                    badge = badge.copy()
                    badge.putalpha(badge.getchannel("A").point(lambda v: v // 3))
                img.paste(badge, (ix, ry + 1), badge)
            if not a[kind]:  # не выдано — значок перечёркнут
                draw.line((ix + 2, ry + 21, ix + 20, ry + 3), fill=CROSS, width=3)
            ix += 24
        # Режим омикрона — в начале: длинное название обрезается с конца.
        name = (f"({a['mode']}) " if a["omicron_tier"] is not None and a["mode"] else "") + a["name"]
        font = _font(14, True, condensed=True)
        draw.text((ix + 4, ry + ABIL_H // 2 - 1), _fit(draw, name, font, rw - (ix - rx) - 6), font=font,
                  fill=TEXT if applied else MISSING_FG, anchor="lm")


def freshness_note(age_seconds) -> str:
    """Подпись к картинке в сообщении: насколько свежи данные (возраст кэша, None — из игры)."""
    if age_seconds is None:
        return "-# Данные из игры — свежие"
    return f"-# Данные из ежечасного обновления, {max(1, int(age_seconds) // 60)} мин назад («обновить» — свежие)"


def render(rows: list[dict]) -> bytes:
    n_cols = max(len(r["cards"]) for r in rows)
    width = PAD * 2 + n_cols * CARD_W + (n_cols - 1) * GAP
    heights = [max(_card_height(c) for c in r["cards"]) for r in rows]
    height = PAD * 2 + sum(heights) + GAP * (len(rows) - 1)
    img = Image.new("RGB", (width, height), BG)
    draw = ImageDraw.Draw(img)
    y = PAD
    for row, h in zip(rows, heights):
        for col, card in enumerate(row["cards"]):
            x = PAD + col * (CARD_W + GAP)
            _draw_card(img, draw, x, y, h, row["name"], row["base_id"], card, row["best"], col)
        y += h + GAP
    out = io.BytesIO()
    img.save(out, format="PNG", optimize=True)
    return out.getvalue()
