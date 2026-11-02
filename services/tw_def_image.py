"""Персональная карта ВГ: только назначения выбранного игрового аккаунта."""
import io
from concurrent.futures import ThreadPoolExecutor

from PIL import Image, ImageDraw

from services.profile_image import _font, _portrait
from tw_def_engine import ZONES


def _squad_lines(value):
    font = _font(18, True)
    lines, line = [], ""
    for word in str(value).split():
        candidate = f"{line} {word}".strip()
        if line and font.getlength(candidate) > 222:
            lines.append(line.rstrip())
            line = ""
        for char in (" " if line else "") + word:
            if line and font.getlength(line + char) > 222:
                lines.append(line.rstrip())
                line = ""
            line += char
    return lines + [line.strip()] if line else lines or ["Отряд"]


def render(guild_name: str, plan_name: str, player: dict) -> bytes:
    width, gap, card_w = 1440, 16, 340
    entries = player.get("entries", [])
    leaders = {e["unit_ids"][0] for e in entries if e.get("unit_ids")}

    def portrait(base_id):
        try:
            return base_id, _portrait(base_id, 72)
        except Exception:
            return base_id, None

    with ThreadPoolExecutor(max_workers=6) as pool:
        portraits = dict(pool.map(portrait, leaders))
    grouped = {z["key"]: [e for e in entries if e.get("zone_key", e["zone"]) in (z["key"], z["label"])] for z in ZONES}
    # Высота общего ряда растёт вместе с числом назначений: ни один отряд не скрывается.
    heights = {r: max(150, max((64 + sum(96 + 23 * len(_squad_lines(e["squad"])) for e in grouped[z["key"]])
                               for z in ZONES if z["row"] == r), default=150)) for r in (1, 2, 3)}
    height = 180 + sum(heights.values()) + gap * 3 + 45
    img = Image.new("RGB", (width, height), "#101c2a")
    draw = ImageDraw.Draw(img)

    def text(x, y, value, size=22, color="#e8eff8", bold=False, max_width=None):
        value = str(value)
        if max_width:
            while value and draw.textlength(value, font=_font(size, bold)) > max_width:
                value = value[:-2] + "…"
        draw.text((x, y), value, font=_font(size, bold), fill=color)

    text(24, 18, "ВАША ЗАЩИТА НА ВГ", 30, "#70d8ff", True)
    text(24, 63, f"{guild_name} · план «{plan_name}»", 24, max_width=1390)
    text(24, 103, player["name"], 25, bold=True, max_width=1390)
    for col in range(1, 5):
        x = 24 + (4 - col) * (card_w + gap)
        text(x, 147, f"Линия {col}" + (" · фронт" if col == 1 else " · тыл" if col == 4 else ""), 19, "#9bb3c9")
    y_by_row = {1: 185, 2: 185 + heights[1] + gap, 3: 185 + heights[1] + heights[2] + gap * 2}
    for zone in ZONES:
        x, y = 24 + (4 - zone["column"]) * (card_w + gap), y_by_row[zone["row"]]
        bottom = y + heights[zone["row"]]
        active = grouped[zone["key"]]
        draw.rounded_rectangle((x, y, x + card_w, bottom), radius=12, fill="#20364a" if active else "#172737", outline="#52c7f0" if active else "#30465b", width=2)
        text(x + 14, y + 10, zone["label"], 23, "#70d8ff", True)
        cy = y + 53
        if not active:
            text(x + 14, cy, "Нет назначений", 18, "#8298ad")
        for entry in active:
            ids = entry.get("unit_ids", [])
            pic = portraits.get(ids[0]) if ids else None
            if pic is not None:
                img.paste(pic, (x + 14, cy), pic)
            else:
                draw.ellipse((x + 14, cy, x + 86, cy + 72), fill="#36516b")
                text(x + 37, cy + 18, "?", 28)
            lines = _squad_lines(entry["squad"])
            for i, line in enumerate(lines):
                text(x + 98, cy + i * 23, line, 18, bold=True)
            text(x + 98, cy + len(lines) * 23 + 5, f"ДК: {len(entry.get('datacrons', []))}", 17, "#9bb3c9")
            cy += 96 + len(lines) * 23
    text(24, height - 32, "Составы и бонусы датакронов — в сообщении к карте.", 18, "#9bb3c9")
    output = io.BytesIO()
    img.save(output, "PNG")
    return output.getvalue()
