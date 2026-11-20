"""Private Grand Arena board; defeated defenders stay on the map."""
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
import math
import re

from PIL import Image, ImageDraw, ImageEnhance, ImageOps
from services.game_personal_modes import arena_team_status, arena_zone_summary
from services.profile_image import _font, _portrait

_ID = re.compile(r"[A-Z0-9_]{1,80}", re.ASCII)
_ICON, _CARD, _STEP = 64, 300, 72
_COLORS = {1: "#78d6c3", 2: "#ffc765", 3: "#fa7581"}


def _load_icon(unit):
    if not _ID.fullmatch(unit):
        return None
    try:
        return _portrait(unit, _ICON)
    except OSError:
        return None


def render(match):
    boards = (match["home"], match["away"])
    if not any(boards):
        return None
    leaders = sorted({team["units"][0] for zones in boards for zone in zones
                      for team in zone["teams"] if team["units"]})
    with ThreadPoolExecutor(max_workers=8) as pool:
        portraits = dict(zip(leaders, pool.map(_load_icon, leaders)))
    rows = (max(map(len, boards)) + 1) // 2

    def slots(zone):
        return max(len(zone["teams"]), zone.get("placed") or 0, zone.get("capacity") or 0)

    row_heights = [max([182] + [140 + math.ceil(slots(zone) / 4) * _STEP
                               for zones in boards for zone in zones[row * 2:row * 2 + 2]])
                   for row in range(rows)]
    width, header, footer = 1440, 112, 76
    height = header + sum(row_heights) + 14 * (rows - 1) + footer
    canvas = Image.new("RGB", (width, height), "#0b111e")
    draw = ImageDraw.Draw(canvas)
    heading, body, small, tiny = _font(25, True), _font(19, True), _font(16), _font(13, True)

    def fit(text, font, limit):
        text = str(text)
        if draw.textlength(text, font=font) <= limit:
            return text
        while text and draw.textlength(text + "…", font=font) > limit:
            text = text[:-1]
        return text + "…"

    draw.text((24, 19), "ВЕЛИКАЯ АРЕНА", font=small, fill="#8b9db8")
    draw.text((width - 24, 19), fit(match.get("opponent") or "Соперник", body, 580), anchor="rt", font=body, fill="#f0f4ff")
    draw.line((24, 53, width - 24, 53), fill="#25354e", width=1)
    draw.text((24, 72), "МОЯ ЗАЩИТА", font=heading, fill="#8fceff")
    draw.text((width - 24, 72), "ЗАЩИТА СОПЕРНИКА", anchor="rt", font=heading, fill="#ff9ba7")
    cy = header + (height - header - footer) // 2
    cx = width // 2
    draw.line((cx, header + 8, cx, height - footer - 8), fill="#2b3a54", width=2)
    draw.regular_polygon((cx, cy, 43), 6, rotation=30, fill="#152239", outline="#496184", width=2)
    draw.text((cx, cy - 9), "ГА", anchor="mm", fill="#dceaff", font=heading)
    draw.text((cx, cy + 17), "РАУНД " + str(match.get("round") if match.get("round") is not None else "—"),
              anchor="mm", fill="#8cabc9", font=tiny)

    for side, zones in enumerate(boards):
        accent = "#7bbdff" if side == 0 else "#ed8a98"
        if not zones:
            draw.text((24 if side == 0 else 802, header + 25), "Игра пока не раскрыла зоны", font=small, fill="#a3b1c9")
        y = header
        for index, zone in enumerate(zones):
            row, col = divmod(index, 2)
            if col == 0 and row:
                y += row_heights[row - 1] + 14
            x = 24 + (1 - col) * 314 if side == 0 else 802 + col * 314
            h = row_heights[row]
            info = arena_zone_summary(zone)
            cleared = info["remaining"] == 0 and bool(zone.get("placed"))
            fill = "#1d1c2b" if cleared else "#142338" if side == 0 else "#2a2031"
            draw.rounded_rectangle((x, y, x + _CARD, y + h), radius=16, fill=fill, outline="#36445b", width=1)
            draw.rounded_rectangle((x + 14, y + 17, x + 18, y + 43), radius=2, fill=accent)
            draw.text((x + 29, y + 18), f"ЗОНА {index + 1}", fill=accent, font=body)
            amount = "—" if info["remaining"] is None else str(info["remaining"])
            capacity = "—" if info["capacity"] is None else str(info["capacity"])
            draw.text((x + 16, y + 53), f"{amount} / {capacity}", fill="#f0f5ff", font=heading)
            draw.text((x + _CARD - 15, y + 62), "ЗАЧИЩЕНА" if cleared else "ОСТАЛОСЬ", anchor="rt",
                      fill="#fa7581" if cleared else "#a1b1ca", font=tiny)
            for item in range(slots(zone)):
                top, column = divmod(item, 4)
                px, py = x + 10 + column * _STEP, y + 94 + top * _STEP
                team = zone["teams"][item] if item < len(zone["teams"]) else None
                state = arena_team_status(team) if team else {"state": None, "holds": 0}
                color = _COLORS.get(state["state"], "#6b7b96")
                draw.rounded_rectangle((px, py, px + _ICON, py + _ICON), radius=8, fill="#0b1322", outline=color, width=2)
                leader = next(iter(team["units"]), None) if team else None
                portrait = portraits.get(leader)
                if portrait is not None:
                    portrait = portrait.convert("RGBA").copy()
                    if state["state"] == 3:
                        portrait = ImageEnhance.Brightness(ImageOps.grayscale(portrait).convert("RGBA")).enhance(0.65)
                    mask = Image.new("L", (_ICON - 4, _ICON - 4))
                    ImageDraw.Draw(mask).rounded_rectangle((0, 0, _ICON - 5, _ICON - 5), radius=5, fill=255)
                    canvas.paste(portrait.resize((_ICON - 4, _ICON - 4)), (px + 2, py + 2), mask)
                else:
                    empty = team is None and zone.get("placed") is not None and item >= zone["placed"]
                    name = (team.get("names") or ["?"])[0] if team else "—" if empty else "?"
                    initials = "".join(word[0] for word in name.split()[:2])
                    draw.text((px + _ICON / 2, py + _ICON / 2), initials, fill="#bdcbe0", font=body, anchor="mm")
                if state["state"] == 3:
                    bx, by = px + _ICON - 21, py + 3
                    draw.rounded_rectangle((bx, by, bx + 18, by + 18), radius=4, fill="#631f30")
                    draw.line((bx + 5, by + 5, bx + 13, by + 13), fill="#ffb4bc", width=2)
                    draw.line((bx + 13, by + 5, bx + 5, by + 13), fill="#ffb4bc", width=2)
                if state["state"] == 2 or state["holds"]:
                    badge = "БОЙ" if state["state"] == 2 else f"УД {state['holds']}"
                    draw.rounded_rectangle((px + 3, py + _ICON - 19, px + _ICON - 3, py + _ICON - 3), radius=4, fill="#392a13")
                    draw.text((px + _ICON / 2, py + _ICON - 12), badge, anchor="mm", font=tiny, fill="#ffcc75")
            defeated = "—" if info["defeated"] is None else str(info["defeated"])
            draw.text((x + 15, y + h - 20), f"Побеждено: {defeated}", anchor="lm", font=small, fill="#adbcd1")

    bottom = height - footer + 23
    for x, color, text in ((24, _COLORS[1], "В защите"), (245, _COLORS[3], "Побеждён · ×"),
                           (520, _COLORS[2], "В бою / УД — удержания"), (940, "#6b7b96", "Серый — нет статуса · ? скрыто · — пусто")):
        draw.rounded_rectangle((x, bottom - 4, x + 12, bottom + 8), radius=3, fill=color)
        draw.text((x + 23, bottom - 7), text, font=small, fill="#b5c4da")
    draw.text((24, height - 20), "Портрет — лидер отряда. Счётчики — оставшиеся отряды / вместимость зоны.", font=tiny, fill="#7186a3")
    result = BytesIO()
    canvas.save(result, format="PNG", optimize=True)
    result.seek(0)
    return result
