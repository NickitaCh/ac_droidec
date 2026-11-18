"""Shared gear icons for Discord and the web."""

from io import BytesIO
from pathlib import Path
import re
from hashlib import blake2s
from tempfile import NamedTemporaryFile

import httpx
from PIL import Image, ImageDraw, ImageFont, ImageOps

import database


ROOT = Path(__file__).resolve().parent.parent
CACHE = ROOT / "cache"
FONT = ROOT / "assets" / "fonts" / "DejaVuSans-Bold.ttf"
ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
TEXTURE = re.compile(r"^tex\.[A-Za-z0-9_-]{1,80}$")
CDN = "https://game-assets.swgoh.gg/textures/{name}.png"
COLORS = {
    1: "#aab6c0", 2: "#61db47", 3: "#61db47", 4: "#37bafa",
    5: "#37bafa", 6: "#37bafa", 7: "#b520fc", 8: "#b520fc",
    9: "#b520fc", 10: "#b520fc", 11: "#b520fc", 12: "#ffd33e", 13: "#ff553d",
}
OUTLINE = ((17, 2), (126, 2), (126, 109), (109, 126),
           (2, 126), (2, 17))


def _frame(source, tier, mark):
    result = Image.new("RGBA", (128, 128))
    clip = Image.new("L", result.size)
    ImageDraw.Draw(clip).polygon(OUTLINE, fill=255)
    art = Image.new("RGBA", result.size, "#100e13")
    icon = ImageOps.contain(source.convert("RGBA"), result.size)
    art.alpha_composite(icon, ((128 - icon.width) // 2, (128 - icon.height) // 2))
    result.paste(art, mask=clip)
    draw = ImageDraw.Draw(result)
    color = COLORS.get(tier, COLORS[1])
    draw.line((*OUTLINE, OUTLINE[0]), fill=color, width=6)
    if mark:
        font = ImageFont.truetype(str(FONT), 22)
        draw.text((118, 9), mark, font=font, fill="white", stroke_width=2, stroke_fill="#16121c", anchor="rt")
    return result


def _save(image, path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(dir=path.parent, suffix=".png", delete=False) as handle:
        temp = Path(handle.name)
    try:
        image.save(temp, format="PNG")
        temp.replace(path)
    finally:
        temp.unlink(missing_ok=True)


def path_for(item):
    if not ID.fullmatch(item):
        return None
    tier, mark = database.get_equipment_icon_style(item) or (None, "")
    raw = CACHE / "equip_img" / f"{item}.png"
    mark_key = blake2s(mark.encode(), digest_size=4).hexdigest()
    framed = CACHE / "equip_framed" / f"{item}-{tier}-{mark_key}.png"
    if tier and framed.is_file():
        return framed
    try:
        if not raw.is_file():
            texture = database.get_equipment_icon(item)
            if not texture or not TEXTURE.fullmatch(texture):
                return None
            response = httpx.get(CDN.format(name=texture), timeout=4)
            if not response.is_success or not response.headers.get("content-type", "").startswith("image/"):
                return None
            with Image.open(BytesIO(response.content)) as source:
                _save(source, raw)
        if tier:
            with Image.open(raw) as source:
                _save(_frame(source, tier, mark), framed)
            return framed
        return raw
    except (OSError, httpx.HTTPError):
        return None
