"""Compact Discord farm cards with game art and per-material deficits."""

from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
from PIL import Image, ImageDraw

from services import equipment_icons
from services.profile_image import _font, _portrait


WIDTH = 760
HEADER = 76
ROW = 57
PAGE_ROWS = 32
BACKGROUND = "#1c2d43"
STRIPE = "#1d395a"
TEAL = "#206d82"
WHITE = "#f6f8fc"
MUTED = "#cad9ea"
def _icon(item):
    path = equipment_icons.path_for(item)
    if path is None:
        return None
    try:
        with Image.open(path) as img:
            return img.convert("RGBA").resize((44, 44), Image.Resampling.LANCZOS)
    except OSError:
        return None


def _fit(draw, text, font, width):
    if draw.textlength(text, font=font) <= width:
        return text
    while text and draw.textlength(text + "…", font=font) > width:
        text = text[:-1]
    return text + "…"


def _rows(report):
    rows = []
    gear_title, relic_title = "Снаряжение", "Материалы реликвий"
    if report["unit_target"]:
        mode, level = report["unit_target"]
        gear_title = f"Снаряжение до G{13 if mode == 'relic' else level}"
        if mode == "relic":
            relic_title = f"Материалы до R{level}"
    for key, title in (("gear", gear_title), ("relic", relic_title)):
        if key == "relic" and not report["show_relic"]:
            continue
        items = [row for row in report["sections"][key] if row[3]]
        if items:
            rows.append(("heading", title, None, None))
            for name, _, _, missing, _, _, item in sorted(items, key=lambda row: row[3], reverse=True):
                rows.append(("item", name, f"× {missing}", item))
        else:
            rows.append(("note", f"{title} · всё есть", None, None))
    if report["blockers"]:
        rows.append(("heading", "Другие требования", None, None))
        rows.extend(("note", line, None, None) for line in report["blockers"])
    return rows


def render(report, player, portrait_id):
    rows = _rows(report)
    items = list(dict.fromkeys(item for _, _, _, item in rows if item))
    with ThreadPoolExecutor(max_workers=12) as pool:
        icons = dict(zip(items, pool.map(_icon, items)))
    portrait = _portrait(portrait_id, 54)
    title = f"{player} · {report['guide'] or report['goals'][0]}"
    pages = [rows[i:i + PAGE_ROWS] for i in range(0, len(rows), PAGE_ROWS)]
    return [_page(title, portrait, page, icons, number, len(pages))
            for number, page in enumerate(pages, 1)]


def _page(title, portrait, rows, icons, number, total):
    canvas = Image.new("RGB", (WIDTH, HEADER + ROW * len(rows)), BACKGROUND)
    draw = ImageDraw.Draw(canvas)
    draw.rectangle((0, 0, WIDTH, HEADER - 1), fill=TEAL)
    left = 16
    if portrait:
        mask = Image.new("L", portrait.size)
        ImageDraw.Draw(mask).ellipse((0, 0, 53, 53), fill=255)
        canvas.paste(portrait, (left, 11), mask)
        left += 67
    heading, body, small = _font(21, bold=True), _font(20), _font(17, bold=True)
    draw.text((left, HEADER // 2), _fit(draw, title, heading, WIDTH - left - 78),
              font=heading, fill=WHITE, anchor="lm")
    if total > 1:
        draw.text((WIDTH - 16, HEADER // 2), f"{number}/{total}", font=small, fill=WHITE, anchor="rm")
    for i, (kind, label, amount, item) in enumerate(rows):
        top = HEADER + ROW * i
        middle = top + ROW // 2
        if kind == "item":
            if i % 2:
                draw.rectangle((0, top, WIDTH, top + ROW - 1), fill=STRIPE)
            icon = icons.get(item)
            if icon:
                canvas.paste(icon, (16, top + 6), icon)
            draw.text((72, middle), _fit(draw, label, body, WIDTH - 176), fill=WHITE, font=body, anchor="lm")
            draw.text((WIDTH - 22, middle), amount, fill=WHITE, font=small, anchor="rm")
        else:
            draw.text((16, middle), _fit(draw, label, small, WIDTH - 32),
                      fill=WHITE if kind == "heading" else MUTED, font=small, anchor="lm")
    output = BytesIO()
    canvas.save(output, format="PNG")
    output.seek(0)
    return output
