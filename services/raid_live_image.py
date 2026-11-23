"""Cards for the active raid panel in Discord."""

from bisect import bisect_right
from io import BytesIO
from pathlib import Path

from PIL import Image, ImageDraw

from services.profile_image import BG, GOLD, MUTED, PANEL, PANEL_ALT, TEXT, _font

# Capital Games, Droid Destruction Update, EA Forums 13853363.
GUILD_THRESHOLDS = (
    10_000_000, 14_500_000, 20_500_000, 28_500_000, 41_500_000, 63_500_000,
    98_500_000, 147_500_000, 245_000_000, 375_000_000, 520_000_000,
)
SUPPORTED_RAIDS = {"droiddestruction", "order66"}
# Game UI textures shared by adjacent reward crates, in guild score order.
CHEST_TEXTURES = (1, 1, 2, 2, 3, 3, 4, 4, 5, 6, 7)
ASSETS = Path(__file__).with_name("assets")


def progress(raid):
    points = raid.get("points")
    if points is None or (raid.get("id") or "").lower() not in SUPPORTED_RAIDS:
        return None
    reached = bisect_right(GUILD_THRESHOLDS, points)
    goal = GUILD_THRESHOLDS[reached] if reached < len(GUILD_THRESHOLDS) else None
    return reached, goal


def score(points):
    return f"{points:,}".replace(",", " ") if points is not None else "—"


def _png(image):
    stream = BytesIO()
    image.save(stream, format="PNG", optimize=True)
    return stream.getvalue()


def _chest(image, index):
    texture = CHEST_TEXTURES[index]
    with Image.open(ASSETS / f"tex.chest_gc_{texture:02}.png") as source:
        icon = source.convert("RGBA")
    icon = icon.crop(icon.getbbox())
    icon.thumbnail((132, 96), Image.Resampling.LANCZOS)
    image.paste(icon, (96 - icon.width // 2, 64 - icon.height // 2), icon)


def guild_card(raid):
    points = raid.get("points")
    reward = progress(raid)
    image = Image.new("RGB", (760, 230 if reward else 126), BG)
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((10, 10, 750, image.height - 10), radius=20, fill=PANEL)
    score_x = 185 if reward else 36
    draw.text((score_x, 25), "Очки гильдии", font=_font(21), fill=MUTED)
    draw.text((score_x, 50), score(points), font=_font(44, True), fill=TEXT)
    if reward:
        reached, goal = reward
        _chest(image, reached if goal else reached - 1)
        current = f"Сундук №{reached} достигнут" if reached else "Первый сундук ещё впереди"
        draw.text((36, 125), current, font=_font(20, True), fill=GOLD)
        if goal:
            fraction = max(0, points / goal)
            target = f"Цель · сундук №{reached + 1}: {score(goal)}"
            remaining = f"Осталось {score(goal - points)} очков"
        else:
            fraction = 1
            target = "Максимальная награда"
            remaining = ""
        draw.text((724, 126), target, font=_font(19, True), fill=TEXT, anchor="ra")
        draw.rounded_rectangle((36, 164, 724, 174), radius=5, fill=BG)
        if fraction:
            draw.rounded_rectangle((36, 164, 36 + round(688 * fraction), 174), radius=5, fill=GOLD)
        draw.text((724, 186), remaining, font=_font(20), fill=TEXT, anchor="ra")
    return _png(image)


def player_card(rows):
    """Rank the snapshot in two columns, read from top to bottom."""
    split = (len(rows) + 1) // 2 if len(rows) > 12 else len(rows)
    columns = (rows[:split], rows[split:]) if split < len(rows) else (rows,)
    width = 760 // len(columns)
    image = Image.new("RGB", (760, 44 + split * 30), BG)
    draw = ImageDraw.Draw(image)
    name_font = _font(19, condensed=True)
    for column, entries in enumerate(columns):
        left = column * width + 8
        right = (column + 1) * width - 8
        draw.text((left + 53, 18), "Игрок", font=_font(17, True), fill=MUTED, anchor="lm")
        draw.text((right - 12, 18), "Очки", font=_font(17, True), fill=MUTED, anchor="rm")
        for index, (name, points) in enumerate(entries):
            rank = column * split + index + 1
            y = 35 + index * 30
            draw.rounded_rectangle((left, y, right, y + 27), radius=5,
                                   fill=PANEL if index % 2 == 0 else PANEL_ALT)
            color = GOLD if rank <= 3 and points else MUTED
            draw.text((left + 12, y + 13), str(rank), font=_font(17, True), fill=color, anchor="lm")
            name = " ".join(name.split())
            if draw.textlength(name, font=name_font) > width - 187:
                while name and draw.textlength(name + "…", font=name_font) > width - 187:
                    name = name[:-1]
                name += "…"
            draw.text((left + 53, y + 13), name, font=name_font, fill=TEXT, anchor="lm")
            draw.text((right - 12, y + 13), score(points), font=_font(18, True, condensed=True),
                      fill=TEXT if points else MUTED, anchor="rm")
    return _png(image)
