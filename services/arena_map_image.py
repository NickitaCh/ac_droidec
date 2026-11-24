"""Grand Arena board; defeated defenders stay on the map."""
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
import math
import re

from PIL import Image, ImageDraw, ImageEnhance, ImageOps
from services.game_personal_modes import arena_team_status, arena_zone_summary
from services.profile_image import _font, _portrait

_ID = re.compile(r"[A-Z0-9_]{1,80}", re.ASCII)
_ICON, _CARD = 64, 300
_COLORS = {1: "#78d6c3", 2: "#ffc765", 3: "#fa7581"}
_ZONE = re.compile(r"(?:4zone|tt4v4|2zone|tt2v2)_phase0([12])_conflict0([12])(?:_duel\d+)?", re.ASCII)
_POSITIONS = {(1, 1): 0, (2, 1): 1, (1, 2): 2, (2, 2): 3}


def zone_position(zone, fallback):
    """Front is phase 1; phase 2 is unlocked behind its matching conflict.

    Verified against Comlink Guild / territoryTournamentDefinition, including
    both the legacy 4zone and current tt4v4 maps. Never use response order.
    """
    match = _ZONE.fullmatch(zone.get("id", ""))
    return _POSITIONS[tuple(map(int, match.groups()))] if match else fallback


def zone_label(zone, fallback, map_id=""):
    if not _ZONE.fullmatch(zone.get("id", "")):
        return f"ЗОНА {fallback + 1}"
    position = zone_position(zone, fallback)
    if zone["id"].startswith(("2zone_", "tt2v2_")) and position == 2:
        return "ФЛОТ" if "1ship" in map_id else "НИЖНЯЯ ЗОНА"
    if position == 1:
        return "ТЫЛ · ВЕРХ" if "0ship" in map_id or "c4s0" in map_id else "ФЛОТ · ТЫЛ"
    return {0: "ПЕРЕДНЯЯ · ВЕРХ", 2: "ПЕРЕДНЯЯ · НИЗ", 3: "ЗАДНЯЯ · НИЗ"}[position]


def positioned_zones(zones):
    result = [(zone_position(zone, index), index, zone) for index, zone in enumerate(zones)]
    # Even unrevealed rear zones retain their real positions on a four-zone board.
    if any(_ZONE.fullmatch(z.get("id", "")) and z["id"].startswith(("4zone_", "tt4v4_")) for z in zones):
        prefix = next(z["id"].split("_phase", 1)[0] for z in zones if _ZONE.fullmatch(z.get("id", "")))
        present = {position for position, _, _ in result}
        for (phase, conflict), position in _POSITIONS.items():
            if position not in present:
                result.append((position, position, {"id": f"{prefix}_phase0{phase}_conflict0{conflict}",
                    "teams": [], "capacity": None, "placed": None, "remaining": None, "defeated": None}))
    return sorted(result, key=lambda item: (item[0], item[1]))


def _portrait_grid(index):
    # HotBot's board: three across in the upper/rear territories;
    # five across in the lower front territory beside the center emblem.
    return (5, 52) if index % 4 == 2 else (3, _ICON)


def _load_icon(unit):
    if not _ID.fullmatch(unit):
        return None
    try:
        return _portrait(unit, _ICON)
    except OSError:
        return None


def zone_bounds(position, side, top, height):
    """Mirror both boards: rear zones outside, front zones beside the emblem."""
    column = position % 2
    x = 48 + (1 - column) * 330 if side == 0 else 762 + column * 330
    return x, top, x + 300, top + height


def render(match):
    boards = (match["home"], match["away"])
    if not any(boards):
        return None
    leaders = sorted({team["units"][0] for zones in boards for zone in zones
                      for team in zone["teams"] if team["units"]})
    with ThreadPoolExecutor(max_workers=8) as pool:
        portraits = dict(zip(leaders, pool.map(_load_icon, leaders)))
    positioned = [positioned_zones(zones) for zones in boards]
    rows = max(position for zones in positioned for position, _, _ in zones) // 2 + 1

    def slots(zone):
        return max(len(zone["teams"]), zone.get("placed") or 0, zone.get("capacity") or 0)

    row_heights = [max([204] + [90 + math.ceil(slots(zone) / _portrait_grid(index)[0]) * (_portrait_grid(index)[1] + 6)
                               for zones in positioned for index, _, zone in zones if index // 2 == row])
                   for row in range(rows)]
    width, header, footer = 1440, 96, 60
    height = header + sum(row_heights) + 12 * (rows - 1) + footer
    canvas = Image.new("RGB", (width, height), "#080e17")
    draw = ImageDraw.Draw(canvas)
    heading, body, small, tiny = _font(25, True), _font(19, True), _font(16), _font(13, True)

    def fit(text, font, limit):
        text = str(text)
        if draw.textlength(text, font=font) <= limit:
            return text
        while text and draw.textlength(text + "…", font=font) > limit:
            text = text[:-1]
        return text + "…"

    draw.text((32, 18), "ВЕЛИКАЯ АРЕНА", font=heading, fill="#e5edf8")
    draw.text((width - 32, 23), fit(match.get("opponent") or "Соперник", body, 700),
              anchor="rt", font=body, fill="#edf3ff")
    draw.text((48, 64), "МОЯ ЗАЩИТА", font=body, fill="#7bbdff")
    draw.text((width - 48, 64), "ЗАЩИТА СОПЕРНИКА", anchor="rt", font=body, fill="#fa8591")
    board_bottom = height - footer
    for side, color in ((0, "#249ff0"), (1, "#ee4356")):
        left, right = (18, 710) if side == 0 else (730, 1422)
        outline = [(left + 30, header - 7), (right - 10, header - 7),
                   (right - 10, board_bottom + 7), (left, board_bottom + 7)]
        draw.polygon(outline, fill="#0c1c2b" if side == 0 else "#26151e", outline=color, width=3)
        draw.line((left + 38, header - 12, right - 15, header - 12), fill=color, width=2)

    for side, zones in enumerate(positioned):
        accent = "#7bbdff" if side == 0 else "#fa8591"
        if not zones:
            draw.text((70 if side == 0 else 800, header + 50), "Игра пока не раскрыла зоны", font=small, fill="#a3b1c9")
        for index, original_index, zone in zones:
            row = index // 2
            y = header + sum(row_heights[:row]) + row * 12
            h = row_heights[row]
            x, _, right, bottom = zone_bounds(index, side, y, h)
            info = arena_zone_summary(zone)
            cleared = info["remaining"] == 0 and bool(zone.get("placed"))
            polygon = [(x + 10, y), (right, y), (right - 5, bottom), (x, bottom)]
            draw.polygon(polygon, fill="#102535" if side == 0 else "#30202a", outline=accent, width=2)
            # Subtle horizontal field texture, like an arena holotable.
            for line_y in range(y + 5, bottom - 2, 6):
                draw.line((x + 12, line_y, right - 8, line_y), fill="#172f40" if side == 0 else "#3a2630")
            label = zone_label(zone, original_index, match.get("map", ""))
            draw.text((x + 150, y + 15), label, anchor="mt", fill=accent, font=small)
            columns, icon_size = _portrait_grid(index)
            step = icon_size + 6
            grid_rows = math.ceil(slots(zone) / columns)
            grid_top = h - 40 - grid_rows * step if index % 4 == 2 else 45
            for item in range(slots(zone)):
                top, column = divmod(item, columns)
                count = min(columns, slots(zone) - top * columns)
                inset = (300 - (count * step - 6)) // 2
                px, py = x + inset + column * step, y + grid_top + top * step
                team = zone["teams"][item] if item < len(zone["teams"]) else None
                state = arena_team_status(team) if team else {"state": None, "holds": 0}
                color = _COLORS.get(state["state"], "#6b7b96")
                draw.rectangle((px, py, px + icon_size, py + icon_size), fill="#0b1322", outline=color, width=2)
                leader = next(iter(team["units"]), None) if team else None
                portrait = portraits.get(leader)
                if portrait is not None:
                    portrait = portrait.convert("RGBA").copy()
                    if state["state"] == 3:
                        portrait = ImageEnhance.Brightness(ImageOps.grayscale(portrait).convert("RGBA")).enhance(0.55)
                    canvas.paste(portrait.resize((icon_size - 4, icon_size - 4)), (px + 2, py + 2))
                else:
                    empty = team is None and zone.get("placed") is not None and item >= zone["placed"]
                    name = (team.get("names") or ["?"])[0] if team else "—" if empty else "?"
                    initials = "".join(word[0] for word in name.split()[:2])
                    draw.text((px + icon_size / 2, py + icon_size / 2), initials, fill="#bdcbe0", font=body, anchor="mm")
                if state["state"] == 3:
                    draw.rectangle((px, py, px + icon_size, py + icon_size), outline=color, width=3)
                    draw.text((px + icon_size - 8, py + 9), "×", anchor="mm", font=body, fill="#ffb4bc", stroke_width=1)
                if state["state"] == 2 or state["holds"]:
                    badge = "БОЙ" if state["state"] == 2 else f"УД {state['holds']}"
                    draw.rectangle((px + 2, py + icon_size - 18, px + icon_size - 2, py + icon_size - 2), fill="#392a13")
                    draw.text((px + icon_size / 2, py + icon_size - 11), badge, anchor="mm", font=tiny, fill="#ffcc75")
            amount = "?" if info["remaining"] is None else str(info["remaining"])
            capacity = "?" if info["capacity"] is None else str(info["capacity"])
            draw.text((x + 150, bottom - 25), f"{amount} / {capacity}", anchor="mm", font=heading,
                      fill="#fa8591" if cleared else "#f0f5ff")

    # Central emblem touches the front zones; fleet/rear stay at the outside corners.
    cx = width // 2
    cy = header + row_heights[0] + 6 if rows > 1 else header + row_heights[0] // 2
    draw.ellipse((cx - 94, cy - 78, cx + 94, cy + 78), fill="#091321", outline="#6a7f99", width=3)
    draw.regular_polygon((cx, cy - 7, 49), 6, rotation=30, fill="#d8dda2")
    draw.regular_polygon((cx, cy - 7, 39), 3, rotation=0, fill="#15293b")
    draw.text((cx, cy + 55), "МАТЧ " + str(match.get("number") or "—"),
              anchor="mm", fill="#bacbdf", font=tiny)
    bottom = height - footer + 24
    for x, color, text in ((32, _COLORS[1], "В защите"), (245, _COLORS[3], "Побеждён · ×"),
                           (505, _COLORS[2], "В бою / УД — удержания"), (945, "#6b7b96", "? — скрыто или нет данных")):
        draw.rectangle((x, bottom - 3, x + 11, bottom + 8), fill=color)
        draw.text((x + 22, bottom - 6), text, font=small, fill="#b5c4da")
    result = BytesIO()
    canvas.save(result, format="PNG", optimize=True)
    result.seek(0)
    return result
