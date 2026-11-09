"""Карточка требований плейта: никаких выведенных из статов рекомендаций."""
import io
from pathlib import Path

from PIL import Image, ImageDraw

from services.profile_image import _font, _portrait

GAME_IMG = Path(__file__).resolve().parent.parent / "web/static/img/game/mods"
SLOTS = [("square", "Квадрат"), ("arrow", "Стрелка"), ("diamond", "Ромб"),
         ("triangle", "Треугольник"), ("circle", "Круг"), ("cross", "Крест")]


def _wrap(text, width, font):
    lines, line = [], ""
    for char in str(text):
        if char == "\n" or (line and font.getlength(line + char) > width):
            lines.append(line)
            line = ""
        if char != "\n":
            line += char
    return lines + [line]


def render(model: dict) -> bytes:
    """model содержит снимок норм, уже отфильтрованный по варианту и отрядам."""
    font = _font(23)
    slot_lines = {slot: _wrap("; ".join(model["slots"].get(slot, [])) or "Не задано", 310, font)
                  for slot, _ in SLOTS}
    row_heights = [max(120, 56 + 29 * max(len(slot_lines[s]) for s, _ in SLOTS[r:r + 2]))
                   for r in (0, 2, 4)]
    details = []
    for label, value in model["requirements"]:
        details.extend(_wrap(f"{label}: {value}", 912, font))
    if not details:
        details = ["Другие требования не заданы"]
    title_lines = _wrap(model["name"], 780, _font(32, True))
    subtitle = _wrap(model["plate"] + (" · " + model["scheme"] if model.get("scheme") else ""), 780, _font(22))
    top = max(150, 40 + 39 * len(title_lines) + 28 * len(subtitle))
    height = top + sum(row_heights) + 36 + 29 * len(details) + 68
    image = Image.new("RGB", (1000, height), "#101c2a")
    draw = ImageDraw.Draw(image)
    try:
        portrait = _portrait(model["base_id"], 100)
        if portrait is not None:
            image.paste(portrait, (28, 25), portrait)
    except Exception:
        pass
    y = 24
    for line in title_lines:
        draw.text((150, y), line, font=_font(32, True), fill="#e8eff8")
        y += 39
    for line in subtitle:
        draw.text((150, y), line, font=_font(22), fill="#9bb3c9")
        y += 28
    y = top
    for row, row_height in enumerate(row_heights):
        for col in range(2):
            slot, label = SLOTS[row * 2 + col]
            x = 28 + col * 480
            draw.rounded_rectangle((x, y, x + 464, y + row_height - 12), radius=12, fill="#20364a")
            try:
                with Image.open(GAME_IMG / f"shape-{slot}.webp") as source:
                    icon = source.convert("RGBA").resize((78, 78), Image.Resampling.LANCZOS)
                image.paste(icon, (x + 10, y + 14), icon)
            except OSError:
                pass
            draw.text((x + 100, y + 10), label, font=_font(23, True), fill="#70d8ff")
            for n, line in enumerate(slot_lines[slot]):
                draw.text((x + 100, y + 44 + n * 29), line, font=font, fill="#e8eff8")
        y += row_height
    y += 12
    for line in details:
        draw.text((36, y), line, font=font, fill="#e8eff8")
        y += 29
    draw.text((36, height - 40), "Только требования плейта · * необязательно · Не задано = нет требования", font=_font(19), fill="#9bb3c9")
    output = io.BytesIO()
    image.save(output, "PNG")
    return output.getvalue()
