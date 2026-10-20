"""Картинка /рейд итоги — как C3PO `raid status`: игроки рейда по убыванию очков, рядом
очки в прошлом рейде. Жёлтым — меньше, чем в прошлый раз; красной строкой — ноль;
с порогом — ниже порога тоже красным. Шрифты и палитра — общие с /профиль."""

import io

from PIL import Image, ImageDraw

from services import raids as rs
from services.profile_image import BG, GOLD, MUTED, PANEL, PANEL_ALT, TEXT, _font, _int

W, PAD = 760, 12
NAME_W = 300
ROW_H = 26
HEAD_FILL = (40, 52, 80)
DROP_BG = (120, 100, 20)
ZERO_BG = (130, 30, 30)


def render(raid: dict, ranked: list[dict], prev_raid: dict | None, prev_scores: dict, threshold: int | None = None) -> bytes:
    """ranked — [{player_id, name, score}] по убыванию; prev_scores — {player_id: очки} прошлого рейда."""
    col_w = (W - 2 * PAD - NAME_W) // 3
    height = PAD + 40 + (len(ranked) + 2) * ROW_H + 80
    img = Image.new("RGB", (W, height), BG)
    draw = ImageDraw.Draw(img)
    y = PAD
    title = f"{rs.raid_name(raid['raid_id'])} — {rs.end_date(raid['end_time'])}"
    draw.text((W // 2, y + 16), title, font=_font(22, True), fill=TEXT, anchor="mm")
    y += 40

    def row(cells: list[str], fill, bold=False, color=TEXT, cell_fills=None):
        nonlocal y
        draw.rectangle((PAD, y, W - PAD, y + ROW_H - 2), fill=fill)
        font = _font(16, bold, condensed=True)
        x = PAD + 6
        name = cells[0]
        while name and draw.textlength(name, font=font) > NAME_W - 12:
            name = name[:-1]
        draw.text((x, y + ROW_H // 2 - 1), name, font=font, fill=color, anchor="lm")
        for i, text in enumerate(cells[1:]):
            cx = PAD + NAME_W + i * col_w
            if cell_fills and cell_fills[i]:
                draw.rectangle((cx, y, cx + col_w - 2, y + ROW_H - 2), fill=cell_fills[i])
            draw.text((cx + col_w - 10, y + ROW_H // 2 - 1), text, font=font, fill=color, anchor="rm")
        y += ROW_H

    prev_label = f"Прошлый ({rs.end_date(prev_raid['end_time'])[:5]})" if prev_raid else "Прошлый"
    row(["Игрок", "Этот рейд", prev_label, "Разница"], HEAD_FILL, bold=True, color=GOLD)
    total = sum(p["score"] for p in ranked)
    prev_total = sum(prev_scores.values()) if prev_raid else None
    diff = f"{total - prev_total:+,}".replace(",", " ") if prev_total is not None else ""
    row(["Всего гильдия", _int(total), _int(prev_total) if prev_total is not None else "—", diff], HEAD_FILL, bold=True)

    for k, p in enumerate(ranked):
        prev = prev_scores.get(p["player_id"]) if prev_raid else None
        low = p["score"] == 0 or (threshold is not None and p["score"] < threshold)
        fill = ZERO_BG if low else (PANEL if k % 2 == 0 else PANEL_ALT)
        dropped = prev is not None and p["score"] < prev and not low
        d = f"{p['score'] - prev:+,}".replace(",", " ") if prev is not None and p["score"] != prev else ""
        row([p["name"], _int(p["score"]), _int(prev) if prev is not None else "—", d], fill,
            cell_fills=[DROP_BG if dropped else None, None, None])

    notes = ["Жёлтым — меньше, чем в прошлом рейде", "красным — ноль" + (f" или ниже {rs.millions(threshold)}" if threshold else "")]
    draw.text((W // 2, y + 16), " · ".join(notes), font=_font(14), fill=MUTED, anchor="mm")
    y += 36
    out = io.BytesIO()
    img.crop((0, 0, W, y)).save(out, format="PNG", optimize=True)
    return out.getvalue()
