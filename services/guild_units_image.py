"""Картинка готовности гильдии к цели «Руководства» (как WookieeBot `guild ready journey`) —
по результату services/guild_units.readiness. Строка — игрок (процент, ник, уровень самой
цели, если уже открыта), колонки — требуемые юниты с порогом над портретом. Фон строки —
от зелёного (готов) к красному. Шрифты и портреты — общие с /профиль."""

import io
import math

from PIL import Image, ImageDraw

from services.profile_image import BG, GOLD, MUTED, TEXT, _font, _portrait

PAD = 12
PCT_W, NAME_W, GUIDE_W, CELL_W = 64, 230, 90, 86
HEAD_H, ROW_H = 118, 28
PIC = 64

MET_FG = (236, 236, 240)
MISS_FG = (255, 170, 120)
NONE_FG = (140, 144, 154)


def _row_color(pct: float):
    """100% — зелёный, ниже — к оливковому и красно-бурому, как у Wookiee."""
    t = max(0.0, min(1.0, pct / 100))
    if t >= 0.999:
        return (22, 74, 30)
    if t >= 0.5:
        k = (t - 0.5) / 0.5
        return (int(92 - 50 * k), int(84 - 6 * k), int(20 + 6 * k))
    k = t / 0.5
    return (int(96 - 4 * k), int(32 + 52 * k), int(26 - 6 * k))


def render(r: dict, guild_name: str, updated_note: str | None = None, title: str | None = None,
           notes: list[str] | None = None, summary: str | None = None, cell_w: int = CELL_W) -> bytes:
    """title — заголовок вместо «Готовность к «цель»» (рейд); без r["guide"] нет колонки «Цель».
    notes — подписи внизу вместо стандартных, summary — строка под заголовком вместо «Готовы: …»."""
    units, players = r["units"], r["players"]
    guide = r.get("guide")
    gw = GUIDE_W if guide else 0
    width = PAD * 2 + PCT_W + NAME_W + gw + cell_w * len(units)
    title_text = (title or f"Готовность к «{guide['name']}»") + f" — {guild_name}"
    # Одна-две колонки уже заголовка — расширяем картинку под него (иначе он обрезается).
    width = max(width, int(ImageDraw.Draw(Image.new("RGB", (1, 1))).textlength(title_text, font=_font(22, True))) + PAD * 4)
    title_h = 64
    groups = r.get("groups") if len(r.get("groups") or []) > 1 else None
    band_h = 30 if groups else 0
    height = PAD + title_h + band_h + HEAD_H + ROW_H * len(players) + 200  # подписи переносятся по словам
    img = Image.new("RGB", (width, height), BG)
    draw = ImageDraw.Draw(img)

    ready = sum(1 for p in players if p["ready"])
    y = PAD
    draw.text((width // 2, y + 16), title_text, font=_font(22, True), fill=TEXT, anchor="mm")
    draw.text((width // 2, y + 44), summary or f"Готовы: {ready} · Не готовы: {len(players) - ready}", font=_font(18), fill=GOLD, anchor="mm")
    y += title_h

    # Шапка: цель, затем требуемые юниты с порогом.
    gx = PAD + PCT_W + NAME_W
    if groups:  # подписи отрядов над их колонками (рейд)
        x = gx + gw
        for k, (name, count) in enumerate(groups):
            w = count * cell_w
            draw.rectangle((x + 2, y, x + w - 2, y + band_h - 6), fill=(70, 58, 30) if k % 2 == 0 else (40, 52, 80))
            label = name
            while label and draw.textlength(label, font=_font(15, True)) > w - 10:
                label = label[:-1]
            draw.text((x + w // 2, y + (band_h - 6) // 2), label, font=_font(15, True), fill=TEXT, anchor="mm")
            x += w
        y += band_h
    if guide:
        portrait = _portrait(guide["base_id"], PIC)
        if portrait is not None:
            img.paste(portrait, (gx + (gw - PIC) // 2, y + HEAD_H - PIC - 8), portrait)
        draw.text((gx + gw // 2, y + 22), "Цель", font=_font(16, True), fill=GOLD, anchor="mm")
    for i, u in enumerate(units):
        x = gx + gw + i * cell_w
        draw.text((x + cell_w // 2, y + 12), "Нужно", font=_font(14, True), fill=(230, 80, 80), anchor="mm")
        draw.text((x + cell_w // 2, y + 32), u["label"], font=_font(15, True), fill=TEXT, anchor="mm")
        p = _portrait(u["base_id"], PIC)
        if p is not None:
            img.paste(p, (x + (cell_w - PIC) // 2, y + HEAD_H - PIC - 8), p)
    draw.line((gx + gw - 3, y + 6, gx + gw - 3, y + HEAD_H + ROW_H * len(players)), fill=(200, 200, 200), width=2)
    y += HEAD_H

    for p in players:
        draw.rectangle((PAD, y, width - PAD, y + ROW_H - 1), fill=_row_color(p["pct"]))
        cy = y + ROW_H // 2
        draw.text((PAD + 6, cy), f"{math.floor(p['pct'] + 1e-9)}%", font=_font(16), fill=TEXT, anchor="lm")
        name_font = _font(16)
        name = p["name"]
        while name and draw.textlength(name, font=name_font) > NAME_W - 10:
            name = name[:-1]
        draw.text((PAD + PCT_W, cy), name, font=name_font, fill=TEXT, anchor="lm")
        if guide:
            draw.text((gx + gw // 2, cy), p["guide_level"] or "—", font=_font(16), fill=TEXT, anchor="mm")
        for i, c in enumerate(p["cells"]):
            x = gx + gw + i * cell_w
            color = MET_FG if c["met"] else (NONE_FG if c["text"] == "—" else MISS_FG)
            draw.text((x + cell_w // 2, cy), c["text"], font=_font(16, c["met"] is False and c["text"] != "—"), fill=color, anchor="mm")
        y += ROW_H

    notes = list(notes or ["Оранжевым — ещё не дотягивает до нужного", "% — средний прогресс по всем юнитам"])
    if updated_note:
        notes.append(updated_note)
    lines = [" · ".join(notes)]
    if draw.textlength(lines[0], font=_font(14)) > width - 2 * PAD:
        lines = []
        for note in notes:  # не влезает и одна подпись — переносим по словам
            cur = ""
            for word in note.split(" "):
                if cur and draw.textlength(f"{cur} {word}", font=_font(14)) > width - 2 * PAD:
                    lines.append(cur)
                    cur = word
                else:
                    cur = f"{cur} {word}".strip()
            lines.append(cur)
    for line in lines:
        draw.text((width // 2, y + 16), line, font=_font(14), fill=MUTED, anchor="mm")
        y += 20
    out = io.BytesIO()
    img.crop((0, 0, width, y + 20)).save(out, format="PNG", optimize=True)
    return out.getvalue()


# ---------------------------------------------------------------- один игрок (как C3PO `panic`)

TILE_W, TILE_H, TILE_GAP, TILES_PER_ROW = 150, 196, 8, 5
STATUS_COLORS = {"met": (60, 190, 80), "short": (235, 190, 40), "missing": (215, 60, 70)}
STATUS_LABELS = {"met": "выполнено", "short": "не хватает реликвии или гира", "missing": "нет персонажа или не хватает звёзд"}


def _level_badge(level: dict | None, is_ship: bool) -> str:
    if level is None:
        return ""
    if is_ship:
        return ""
    return str(level["relic"]) if level["relic"] is not None else f"G{level['gear']}"


def render_player(r: dict, player_name: str, updated_note: str | None = None) -> bytes:
    """Плитки требуемых юнитов: портрет, текущие звёзды и реликвия/гир, в углу — сколько нужно;
    рамка — зелёная (выполнено), жёлтая (не хватает реликвии/гира), красная (нет / мало звёзд)."""
    units = r["units"]
    cols = min(TILES_PER_ROW, max(1, len(units)))
    rows = (len(units) + cols - 1) // cols
    width = PAD * 2 + cols * TILE_W + (cols - 1) * TILE_GAP
    width = max(width, 520)
    head_h = 70
    height = PAD + head_h + rows * (TILE_H + TILE_GAP) + 120
    img = Image.new("RGB", (width, height), BG)
    draw = ImageDraw.Draw(img)

    title = f"{player_name} — готовность к «{r['guide']['name']}»"
    tf = _font(20, True)
    while draw.textlength(title, font=tf) > width - 2 * PAD and len(title) > 10:
        title = title[:-2]
    draw.text((width // 2, PAD + 14), title, font=tf, fill=TEXT, anchor="mm")
    met = sum(1 for u in units if u["status"] == "met")
    sub = f"Выполнено {met} из {len(units)} · прогресс {math.floor(r['pct'] + 1e-9)}%"
    if r.get("guide_level"):
        sub += " · цель уже открыта"
    draw.text((width // 2, PAD + 44), sub, font=_font(17), fill=GOLD, anchor="mm")
    y0 = PAD + head_h

    for i, u in enumerate(units):
        cx, cy = divmod(i, cols)[1], divmod(i, cols)[0]
        x = PAD + cx * (TILE_W + TILE_GAP)
        y = y0 + cy * (TILE_H + TILE_GAP)
        color = STATUS_COLORS[u["status"]]
        draw.rounded_rectangle((x, y, x + TILE_W, y + TILE_H), radius=8, fill=(8, 8, 10), outline=color, width=4)
        # Угол с требованием.
        draw.polygon([(x + TILE_W - 62, y + 2), (x + TILE_W - 2, y + 2), (x + TILE_W - 2, y + 62)], fill=color)
        for k, part in enumerate(u["label"].split(" ")[:2]):
            draw.text((x + TILE_W - 8, y + 6 + k * 17), part, font=_font(14, True), fill=(20, 20, 20), anchor="ra")
        pic = 96
        px, py = x + (TILE_W - pic) // 2, y + 22
        portrait = _portrait(u["base_id"], pic)
        if portrait is not None:
            if u["level"] is None:  # юнита нет — серый портрет
                gray = portrait.convert("LA").convert("RGBA")
                gray.putalpha(portrait.getchannel("A").point(lambda v: v * 5 // 10))
                portrait = gray
            mask = Image.new("L", (pic, pic), 0)
            ImageDraw.Draw(mask).ellipse((0, 0, pic - 1, pic - 1), fill=255)
            img.paste(portrait, (px, py), Image.composite(portrait.getchannel("A"), mask, mask))
        badge = _level_badge(u["level"], u["is_ship"])
        if badge:
            bx, by = x + TILE_W // 2, py + pic - 4
            relic = u["level"]["relic"] is not None
            draw.ellipse((bx - 15, by - 15, bx + 15, by + 15), fill=(120, 30, 30) if relic else (40, 70, 120), outline=(230, 230, 230), width=2)
            draw.text((bx, by), badge, font=_font(14 if len(badge) > 2 else 16, True), fill=TEXT, anchor="mm")
        stars = u["level"]["stars"] if u["level"] else 0
        sy = py + pic + 18
        for k in range(7):
            draw.text((x + TILE_W // 2 + (k - 3) * 16, sy), "★", font=_font(15, True),
                      fill=GOLD if k < stars else (70, 72, 80), anchor="mm")
        nf = _font(14, True, condensed=True)
        words, lines, cur = u["name"].split(), [], ""
        for w in words:
            if draw.textlength((cur + " " + w).strip(), font=nf) <= TILE_W - 12:
                cur = (cur + " " + w).strip()
            else:
                lines.append(cur)
                cur = w
        lines.append(cur)
        for k, line in enumerate(lines[:2]):
            draw.text((x + TILE_W // 2, sy + 18 + k * 17), line, font=nf, fill=TEXT, anchor="mm")

    y = y0 + rows * (TILE_H + TILE_GAP) + 6
    notes = [f"Зелёная рамка — {STATUS_LABELS['met']}", f"Жёлтая — {STATUS_LABELS['short']}",
             f"Красная — {STATUS_LABELS['missing']}"] + ([updated_note] if updated_note else [])
    for line in notes:
        lf = _font(13)
        while draw.textlength(line, font=lf) > width - 2 * PAD and len(line) > 10:
            line = line[:-2]
        draw.text((width // 2, y + 10), line, font=lf, fill=MUTED, anchor="mm")
        y += 20
    out = io.BytesIO()
    img.crop((0, 0, width, y + 10)).save(out, format="PNG", optimize=True)
    return out.getvalue()
