"""Render the current Grand Arena zones as a private Discord map."""
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
import re

from PIL import Image, ImageDraw

from services.profile_image import _font, _portrait

_ID = re.compile(r"[A-Z0-9_]{1,80}", re.ASCII)
_ICON = 40
_GAP = 5
_ZONE_WIDTH = 239
_ZONE_HEIGHT = 150


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
    leaders = sorted({team["units"][0] for zones in boards for zone in zones for team in zone["teams"] if team["units"]})
    with ThreadPoolExecutor(max_workers=8) as pool:
        portraits = dict(zip(leaders, pool.map(_load_icon, leaders)))
    width = 1024
    rows = (max(map(len, boards)) + 1) // 2
    height = 70 + rows * (_ZONE_HEIGHT + 12)
    canvas = Image.new("RGB", (width, height), "#101823")
    draw = ImageDraw.Draw(canvas)
    heading, body, small = _font(23, True), _font(17, True), _font(13)
    for side, zones in enumerate(boards):
        left, accent, fill = (14, "#64b5fa", "#172c40") if side == 0 else (522, "#fb827a", "#39262d")
        draw.text((left, 17), "Моя защита" if side == 0 else "Зоны атаки", fill=accent, font=heading)
        if not zones:
            draw.text((left, 79), "Игра пока не раскрыла эти зоны", fill="#d5dce5", font=body)
        for index, zone in enumerate(zones):
            row, col = divmod(index, 2)
            x = left + (1 - col if side == 0 else col) * (_ZONE_WIDTH + 10)
            y = 56 + row * (_ZONE_HEIGHT + 12)
            draw.rounded_rectangle((x, y, x + _ZONE_WIDTH, y + _ZONE_HEIGHT), radius=9,
                                   fill=fill, outline=accent, width=1)
            draw.text((x + 12, y + 10), f"Зона {index + 1}", fill="#f8fafc", font=body)
            count = str(zone["placed"])
            if zone["capacity"] is not None:
                count += f" / {zone['capacity']}"
            draw.text((x + _ZONE_WIDTH - 12, y + 12), count, fill=accent, font=small, anchor="rt")
            if not zone["teams"]:
                label = "Составы не раскрыты" if side or zone["placed"] else "Пусто"
                draw.text((x + 12, y + 52), label, fill="#d5dce5", font=small)
            for item, team in enumerate(zone["teams"][:10]):
                top, column = divmod(item, 5)
                px, py = x + 9 + column * (_ICON + _GAP), y + 38 + top * (_ICON + _GAP)
                leader = next(iter(team["units"]), None)
                portrait = portraits.get(leader)
                draw.rectangle((px, py, px + _ICON - 1, py + _ICON - 1), fill="#101823")
                if portrait:
                    canvas.paste(portrait, (px, py), portrait)
                else:
                    name = (team.get("names") or ["?"])[0]
                    initials = "".join(word[0] for word in name.split()[:2])
                    draw.text((px + _ICON / 2, py + _ICON / 2), initials, fill="#f8fafc", font=small, anchor="mm")
                draw.rectangle((px, py, px + _ICON - 1, py + _ICON - 1), outline=accent, width=1)
            if zone["defeated"]:
                draw.text((x + 12, y + 132), f"Побеждено {zone['defeated']}", fill="#d5dce5", font=small)
    result = BytesIO()
    canvas.save(result, format="PNG", optimize=True)
    result.seek(0)
    return result
