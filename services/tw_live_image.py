"""Draw the live Territory War board from the game's revealed zone data."""
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
from pathlib import Path
import re

from PIL import Image, ImageDraw

import database
from services.game_event_live import TW_ACTIVE_SQUAD_STATUSES, tw_remaining
from services.tb_live import OPEN_ZONE
from services.player_profile import galactic_legends
from services.profile_image import _font, _portrait
from tw_def_engine import ZONES

_GAME_ZONE = re.compile(r"tw_jakku01_phase0([1-4])_conflict0([1-3])", re.ASCII)
_UNIT_ID = re.compile(r"[A-Z0-9_]{1,80}", re.ASCII)
_BOARD_ART = Path(__file__).resolve().parents[1] / "web/static/img/game/tw-jakku-board.webp"
_POSITION = {
    (1, 1): "1t", (1, 2): "1b", (2, 1): "2t", (2, 2): "2b",
    (3, 1): "3f", (3, 2): "3c", (3, 3): "3b",
    (4, 1): "4f", (4, 2): "4c", (4, 3): "4b",
}
# Center of each territory on the blue half of the clean Jakku board.
_ANCHORS = {
    "4f": (209, 147), "3f": (400, 98), "2t": (592, 156), "1t": (781, 220),
    "4c": (142, 324), "3c": (376, 303), "4b": (136, 539), "3b": (366, 613),
    "2b": (595, 557), "1b": (829, 497),
}
_ART_REFERENCE = (1952, 806)


def zone_key(zone_id):
    match = _GAME_ZONE.fullmatch(zone_id)
    return _POSITION.get(tuple(map(int, match.groups()))) if match else None


def open_zone_groups(state):
    order = {entry["key"]: index for index, entry in enumerate(ZONES)}
    zones = (zone for zone in state.get("opponent", ()) if zone.get("status") == OPEN_ZONE)
    for zone in sorted(zones, key=lambda entry: order.get(zone_key(entry["id"]), 99)):
        teams_in_zone = zone.get("teams", ()) if tw_remaining(zone) != 0 else ()
        groups = {}
        for team in teams_in_zone:
            units = team.get("units", ())
            if units and team.get("status") in TW_ACTIVE_SQUAD_STATUSES:
                composition = (units[0], *sorted(units[1:]))
                groups.setdefault(composition, []).append(team)
        yield zone, sorted(groups.items(), key=lambda item: (-len(item[1]), item[0]))


def zone_pages(state):
    pages = []
    labels = {entry["key"]: entry["label"] for entry in ZONES}
    for zone, groups in open_zone_groups(state):
        remaining = tw_remaining(zone)
        label = labels.get(zone_key(zone["id"]), zone["id"])
        count = "—" if remaining is None else str(remaining)
        capacity = zone.get("capacity")
        title = f"{label.capitalize()} · осталось {count} / {capacity if capacity is not None else '—'}"
        shown = sum(len(teams) for _, teams in groups)
        if remaining == 0:
            note = "Нет оставшихся отрядов."
        elif any(team.get("status") is None for team in zone.get("teams", ())):
            note = "В этом снимке нет статусов части отрядов. Обновите сводку."
        elif not groups:
            note = "Составы пока не раскрыты."
        elif remaining is not None and shown < remaining:
            note = f"Раскрыто составов: {shown} из {remaining}."
        else:
            note = ""
        pages.append((title, note, groups))
    return sorted(pages, key=lambda page: not page[2])


def render_zone(title, groups):
    """One strip of unit portraits and ownership details per composition."""
    size, width, header = 58, 720, 76
    unit_ids = sorted({unit for units, _ in groups for unit in units})
    names = database.get_game_unit_names(unit_ids)
    with ThreadPoolExecutor(max_workers=8) as pool:
        icons = dict(zip(unit_ids, pool.map(lambda unit: _unit_icon(unit, size), unit_ids)))
    heading, count_font, normal, small = _font(27, True), _font(24, True), _font(18), _font(12, True)
    label_draw = ImageDraw.Draw(Image.new("RGB", (1, 1)))
    details = []
    for _, teams in groups:
        chips = []
        for team in sorted(teams, key=lambda row: (-row.get("holds", 0), row.get("player_name") or "")):
            owner = (team.get("player_name") or "Имя скрыто")[:45]
            holds = team.get("holds", 0)
            locked = team.get("status") == 2
            # The counter already includes the current attempt once a squad is locked.
            holds = max(0, holds - locked)
            label = f"{owner} · удерж. {holds}" if holds else owner
            if locked:
                label += " · в бою"
            chips.append((label, "#ffcf78" if holds else "#a1dcff" if locked else "#ecf3f9"))
        positions, x, y = [], 26, 78
        for label, color in chips:
            chip_width = round(label_draw.textlength(label, font=normal)) + 22
            if x + chip_width > width - 24:
                x, y = 26, y + 27
            positions.append((x, y, chip_width, label, color))
            x += chip_width + 6
        details.append((positions, y + 38))
    image = Image.new("RGB", (width, header + sum(height for _, height in details) + 12), "#111923")
    draw = ImageDraw.Draw(image)
    draw.text((24, 18), title, fill="#e5f5ff", font=heading)
    draw.text((24, 52), "Жёлтым отмечены пачки, которые удержали атаку", fill="#ffcf78", font=small)
    y = header
    for (units, teams), (positions, row_height) in zip(groups, details):
        draw.rounded_rectangle((12, y, width - 12, y + row_height - 8), radius=10, fill="#202d3b")
        draw.text((28, y + 20), f"×{len(teams)}", fill="#a1dcff", font=count_font)
        for position, unit in enumerate(units):
            x = 83 + position * (size + 5)
            draw.rectangle((x, y + 12, x + size, y + 12 + size), fill="#293a4a")
            if icon := icons[unit]:
                image.paste(icon, (x, y + 12), icon)
            else:
                words = (names.get(unit) or unit).split()
                draw.multiline_text((x + size // 2, y + 44), "\n".join(word[:8] for word in words[:3]),
                                    fill="#ebf2f7", font=small, anchor="mm", align="center", spacing=1)
            if position == 0:
                draw.rectangle((x, y + 12, x + size, y + 12 + size), outline="#f5c867", width=2)
        for x, offset, chip_width, label, color in positions:
            top = y + offset
            draw.rounded_rectangle((x, top, x + chip_width, top + 23), radius=6, fill="#2a3b4d")
            draw.text((x + 10, top + 1), label, fill=color, font=normal)
        y += row_height
    result = BytesIO()
    image.save(result, "PNG", optimize=True)
    return result.getvalue()


def _unit_icon(unit, size=53):
    if not _UNIT_ID.fullmatch(unit):
        return None
    try:
        return _portrait(unit, size)
    except OSError:
        return None


def breakdown(zone, legends, *, battle=False):
    teams = [team for team in zone.get("teams", ())
             if team.get("units") and (not battle or team.get("status") in TW_ACTIVE_SQUAD_STATUSES)]
    counts = Counter(team["units"][0] for team in teams)
    leader, count = min(counts.items(), key=lambda pair: (-pair[1], pair[0])) if counts else (None, 0)
    return {
        "leader": leader, "count": count, "known": len(teams),
        "legends": sum(not legends.isdisjoint(team["units"]) for team in teams),
        "holds": sum(team["holds"] for team in teams),
    }


def render(state):
    if not state or not any(state[side] for side in ("own", "opponent")):
        return None
    boards = [{key: zone for zone in state[side] if (key := zone_key(zone["id"]))}
              for side in ("own", "opponent")]
    if not any(boards):
        return None
    legends = set(galactic_legends())
    stats = [{key: breakdown(zone, legends, battle=state["round"] != 0) for key, zone in board.items()}
             for board in boards]
    leaders = sorted({d["leader"] for data in stats for d in data.values() if d["leader"]})
    names = database.get_game_unit_names(leaders) if leaders else {}
    with ThreadPoolExecutor(max_workers=6) as pool:
        icons = dict(zip(leaders, pool.map(_unit_icon, leaders)))
    width, board_height, header, footer = 1536, 635, 52, 37
    with Image.open(_BOARD_ART) as asset:
        board_art = asset.convert("RGB").resize((width, board_height), Image.Resampling.LANCZOS)
    height = header + board_height + footer
    image = Image.new("RGB", (width, height), "#101922")
    image.paste(board_art, (0, header))
    draw = ImageDraw.Draw(image)
    title, label, normal, small = _font(28, True), _font(19, True), _font(16), _font(14, True)

    def fit(value, font, limit):
        original = str(value)
        if draw.textlength(original, font=font) <= limit:
            return original
        while original and draw.textlength(original + "…", font=font) > limit:
            original = original[:-1]
        return original + "…"

    for i, (board, details) in enumerate(zip(boards, stats)):
        left, accent = (21, "#8bdaff") if i == 0 else (width // 2 + 21, "#ffb1a8")
        team = state.get("home_guild" if i == 0 else "opponent_guild") or {}
        heading = team.get("name") or ("Наша защита" if i == 0 else "Защита соперника")
        draw.text((left, 9), fit(heading, title, 720), fill=accent, font=title)
        for entry in ZONES:
            zone, info = board.get(entry["key"]), details.get(entry["key"])
            ax, ay = _ANCHORS[entry["key"]]
            ax = _ART_REFERENCE[0] - ax if i else ax
            x = round(ax * width / _ART_REFERENCE[0])
            y = header + round(ay * board_height / _ART_REFERENCE[1])
            def marker(offset, value, font, color):
                draw.text((x, y + offset), value, font=font, fill=color, anchor="mm",
                          stroke_width=2, stroke_fill="#111821")

            marker(26, entry["label"].upper(), small, accent)
            if not zone:
                marker(47, "Нет данных", small, "#e3eaf1")
                continue
            placed = zone["deployed"]
            if state["round"] == 0:
                amount, target = placed, zone["capacity"]
            else:
                amount, target = tw_remaining(zone), zone["capacity"]
            display = "—" if amount is None else str(amount)
            if target is not None:
                display += f" / {target}"
            marker(47, display, label, "#ffffff")
            if amount == 0:
                continue
            if info["leader"]:
                icon = icons.get(info["leader"])
                if icon is not None:
                    image.paste(icon, (x - 26, y - 39), icon)
                    draw.rectangle((x - 26, y - 39, x + 26, y + 13), outline=accent, width=2)
                name = names.get(info["leader"]) or info["leader"]
                marker(68, fit(f"{name} ×{info['count']}", small, 126), small, "#fff")
                more = info["known"] != amount
                numbers = f"ГЛ: {info['legends']} · удерж.: {info['holds']}"
                if more:
                    numbers += " *"
                marker(85, numbers, small, "#ffdc57")
            else:
                text = "Зона закрыта" if i == 1 and state["round"] != 0 and zone["status"] == 1 else "Составы скрыты"
                marker(68, text, small, "#e3eaf1")
    subtitle = "Расстановка · занятые слоты / вместимость" if state["round"] == 0 else "Бой · осталось отрядов / вместимость зоны"
    draw.text((22, height - 27), subtitle + "   ·   * по раскрытым составам", font=normal, fill="#b8c5d1")
    result = BytesIO()
    image.save(result, "PNG", optimize=True)
    return result.getvalue()
