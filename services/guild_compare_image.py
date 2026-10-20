"""Картинка для /вг_соперник — как HotBot `/compare guild`: две колонки, в каждой полная
сводка гильдии (итоги, модули, реликвии, датакроны, средний игрок, ГЛ, мета, флот со
средними статами у владельцев). Где лучше наша гильдия — зелёным, где соперник — красным.
Данные — player_profile.summarize_guild по обеим гильдиям; шрифты, портреты и палитра —
общие с /профиль (services/profile_image.py)."""

import io

from PIL import Image, ImageDraw

from services import player_profile as pp
from services.profile_image import (
    BG, GOLD, HEADER_BOTTOM, HEADER_TOP, LABEL_BG, LABEL_FG, MUTED, PANEL, PANEL_ALT, TEXT,
    _font, _int, _pct, _portrait, compact, short,
)

PAD, GAP = 14, 12
COL_W = 600
W = PAD * 2 + COL_W * 2 + GAP
ROW_H = 34
UNIT_H = 120
PIC = 96
OURS_BG = (52, 120, 60)
THEIRS_BG = (140, 48, 48)

# (подпись, путь в сводке, формат, больше = лучше; None — без подсветки)
SECTIONS = [
    ("Итого", [
        ("Участников", ("members",), "int", True), ("ГМ", ("gp",), "compact", True),
        ("ГМ персонажей", ("char_gp",), "compact", True), ("ГМ кораблей", ("ship_gp",), "compact", True),
        ("Реликвий", ("relics", "total"), "int", True), ("Омикроны", ("omicrons",), "int", True),
        ("Омикроны ВГ", ("omicrons_tw",), "int", True), ("Омикроны ТБ", ("omicrons_tb",), "int", True),
        ("G13", ("g13",), "int", True), ("G12", ("g12",), "int", None),
    ]),
    ("Модули", [
        ("Скорость 25+", ("mods", "s25"), "int", True), ("6 точек", ("mods", "six_dot"), "int", True),
        ("Скорость 20–24", ("mods", "s20_24"), "int", True), ("Скорость 20+", ("mods", "s20"), "int", True),
        ("Скорость 15–19", ("mods", "s15_19"), "int", True), ("Скорость 15+", ("mods", "s15"), "int", True),
        ("Скорость 10–14", ("mods", "s10_14"), "int", True), ("Скорость 10+", ("mods", "s10"), "int", True),
        ("Атака 6%+", ("mods", "off6"), "int", True), ("Атака 4%+", ("mods", "off4"), "int", True),
        ("Оборона 14%+", ("mods", "def14"), "int", True), ("Оборона 9%+", ("mods", "def9"), "int", True),
    ]),
    ("Реликвии", [
        ("Всего", ("relics", "total"), "int", True), ("R10", ("relics", "r10"), "int", True),
        ("R9", ("relics", "r9"), "int", True), ("R8", ("relics", "r8"), "int", True),
        ("R7", ("relics", "r7"), "int", True), ("R6", ("relics", "r6"), "int", True),
        ("R5", ("relics", "r5"), "int", True), ("R1–R4", ("relics", "r1_4"), "int", None),
    ]),
    ("Датакроны", [
        ("Перебросов", ("datacrons", "rerolls"), "int", True), ("Уровень 9", ("datacrons", "l9"), "int", True),
        ("Уровень 6–8", ("datacrons", "l6_8"), "int", None), ("Уровень 3–5", ("datacrons", "l3_5"), "int", None),
    ]),
    ("Средний игрок", [
        ("ГМ", ("avg", "gp"), "compact", True), ("Лучшие 80", ("avg", "top80"), "compact", True),
        ("Реликвий", ("avg", "relics"), "int", True), ("6 точек", ("avg", "six_dot"), "int", True),
        ("Скор. модулей G11+", ("avg", "speed_g11"), "plus", True), ("Зеты", ("avg", "zetas"), "int", True),
        ("G13", ("avg", "g13"), "int", True), ("G12", ("avg", "g12"), "int", None),
        ("Место на арене", ("avg", "arena_rank"), "int", False), ("Место на флоте", ("avg", "fleet_rank"), "int", False),
    ]),
]

# Статы юнита в карточке (средние у владельцев): (подпись, ключ, процент ли).
UNIT_STATS = [("Здоровье", "health", False), ("Защита", "protection", False), ("Физ. урон", "phys", False),
              ("Стойкость", "tenacity", True), ("Эффект.", "potency", True), ("Особ. урон", "spec", False)]


def _v(g: dict, path: tuple):
    v = g
    for k in path:
        v = v.get(k) if isinstance(v, dict) else None
    return v


def _fmt(value, kind: str) -> str:
    if value is None:
        return "—"
    if kind == "compact":
        return compact(value)
    if kind == "plus":
        return f"+{_int(value)}"
    return _int(value)


def _mark(col: int, va, vb, higher):
    """Заливка ячейки колонки col (0 — мы, 1 — соперник): только у лучшей из двух."""
    if higher is None or va is None or vb is None or abs(va - vb) < 1e-9:
        return None
    better = 0 if (va > vb) == bool(higher) else 1
    if better != col:
        return None
    return OURS_BG if col == 0 else THEIRS_BG


def render(a: dict, b: dict, note: str | None = None) -> bytes:
    guilds = (a, b)
    unit_blocks = []
    for title, key, ids in (("Галактические легенды", "gls", None), ("Мета", "meta_units", pp.META_CHARACTERS),
                            ("Мета флота", "fleet", pp.FLEET_META)):
        by = [{r["base_id"]: r for r in g.get(key) or []} for g in guilds]
        order = list(ids) if ids else pp.ordered_gls(list(set(by[0]) | set(by[1])))
        unit_blocks.append((title, [(x, by[0].get(x), by[1].get(x)) for x in order if x in by[0] or x in by[1]]))

    metric_rows = sum((len(c) + 1) // 2 for _t, c in SECTIONS)
    n_units = sum(len(u) for _t, u in unit_blocks)
    height = PAD + 110 + (len(SECTIONS) + len(unit_blocks)) * 46 + metric_rows * (ROW_H + 4) + n_units * (UNIT_H + 6) + 80
    img = Image.new("RGB", (W, height), BG)
    draw = ImageDraw.Draw(img)

    def fit(text, font, width):
        if draw.textlength(text, font=font) <= width:
            return text
        while text and draw.textlength(text + "…", font=font) > width:
            text = text[:-1]
        return text + "…"

    def col_x(i):
        return PAD + i * (COL_W + GAP)

    y = PAD
    for i, g in enumerate(guilds):
        x, h = col_x(i), 96
        for k in range(h):
            t = k / h
            draw.line((x, y + k, x + COL_W, y + k), fill=(int(70 - 50 * t), int(18 - 8 * t), int(16 - 6 * t)))
        draw.rectangle((x, y, x + COL_W, y + h), outline=GOLD, width=2)
        draw.text((x + COL_W // 2, y + h // 2), fit(g["name"], _font(32, True), COL_W - 30), font=_font(32, True), fill=TEXT, anchor="mm")
    y += 96 + GAP

    def section(title):
        nonlocal y
        sh = 36
        for i in range(2):
            x = col_x(i)
            for k in range(sh):
                t = k / sh
                draw.line((x, y + k, x + COL_W, y + k), fill=tuple(int(p + (q - p) * t) for p, q in zip(HEADER_TOP, HEADER_BOTTOM)))
            draw.text((x + COL_W // 2, y + sh // 2), title, font=_font(20, True), fill=LABEL_FG, anchor="mm")
        y += sh + 4

    half = COL_W // 2
    label_w = int(half * 0.56)
    for title, cells in SECTIONS:
        section(title)
        for r in range(0, len(cells), 2):
            for c, (label, path, kind, higher) in enumerate(cells[r:r + 2]):
                va, vb = _v(a, path), _v(b, path)
                for i, v in enumerate((va, vb)):
                    x = col_x(i) + c * half
                    draw.rectangle((x, y, x + label_w - 2, y + ROW_H), fill=LABEL_BG)
                    fill = _mark(i, va, vb, higher) or (PANEL if (r // 2) % 2 == 0 else PANEL_ALT)
                    draw.rectangle((x + label_w, y, x + half - 3, y + ROW_H), fill=fill)
                    lf = _font(17, condensed=True)
                    draw.text((x + label_w - 8, y + ROW_H // 2), fit(label, lf, label_w - 12), font=lf, fill=LABEL_FG, anchor="rm")
                    vf = _font(19, True, condensed=True)
                    draw.text((x + label_w + 8, y + ROW_H // 2), fit(_fmt(v, kind), vf, half - label_w - 14), font=vf, fill=TEXT, anchor="lm")
            y += ROW_H + 4
        y += 6

    for title, rows in unit_blocks:
        if not rows:
            continue
        section(f"{title} — средние у владельцев")
        for k, (base_id, ra, rb) in enumerate(rows):
            for i, row in enumerate((ra, rb)):
                ours, theirs = ra or {}, rb or {}
                x = col_x(i)
                draw.rectangle((x, y, x + COL_W, y + UNIT_H), fill=PANEL if k % 2 == 0 else PANEL_ALT)
                portrait = _portrait(base_id, PIC)
                if portrait is not None:
                    if row is None:  # ни у кого нет — серый портрет
                        gray = portrait.convert("LA").convert("RGBA")
                        gray.putalpha(portrait.getchannel("A").point(lambda v: v * 6 // 10))
                        portrait = gray
                    img.paste(portrait, (x + 8, y + (UNIT_H - PIC) // 2), portrait)
                tx = x + PIC + 20
                if row is None:
                    draw.text((tx, y + UNIT_H // 2), "Нет ни у кого", font=_font(18), fill=MUTED, anchor="lm")
                    continue
                sp = row.get("speed")
                sp_text = f"Скорость {_int(sp)}" + (f" (+{_int(row['speed_mods'])})" if row.get("speed_mods") else "")
                sp_fill = _mark(i, ours.get("speed"), theirs.get("speed"), True)
                if sp_fill:
                    draw.rectangle((tx - 4, y + 4, x + COL_W - 6, y + 32), fill=sp_fill)
                draw.text((tx, y + 18), sp_text, font=_font(19, True), fill=TEXT if sp_fill else GOLD, anchor="lm")
                cw = (x + COL_W - 6 - tx) // 3
                for j, (label, key, is_pct) in enumerate(UNIT_STATS):
                    cx, cy = tx + (j % 3) * cw, y + 38 + (j // 3) * 26
                    val = row.get(key)
                    text = f"{label} {_pct(val) if is_pct else short(val)}"
                    fill = _mark(i, ours.get(key), theirs.get(key), True)
                    if fill:
                        draw.rectangle((cx - 4, cy, cx + cw - 6, cy + 24), fill=fill)
                    font = _font(15, condensed=True)
                    draw.text((cx, cy + 12), fit(text, font, cw - 8), font=font, fill=TEXT, anchor="lm")
                parts = [f"Есть у {row['owners']}"]
                if row["is_ship"]:
                    parts.append(f"7★ {row['stars7']}")
                else:
                    parts.append(f"R8+ {row['r8_plus']}")
                    if row.get("ultimates"):
                        parts.append(f"абс. {row['ultimates']}")
                own_fill = _mark(i, ours.get("owners", 0), theirs.get("owners", 0), True)
                own_text = " · ".join(parts)
                own_font = _font(16, True, condensed=True)
                if own_fill:
                    draw.rectangle((tx - 4, y + 92, tx + int(draw.textlength(own_text, font=own_font)) + 6, y + 116), fill=own_fill)
                draw.text((tx, y + 104), own_text, font=own_font, fill=TEXT, anchor="lm")
            y += UNIT_H + 6
        y += 6

    foot = "Зелёным — где лучше мы, красным — где лучше соперник · мы — ежечасное обновление, соперник — данные из игры на момент запроса"
    if note:
        foot += f" · {note}"
    draw.text((W // 2, y + 16), fit(foot, _font(15), W - 2 * PAD), font=_font(15), fill=MUTED, anchor="mm")
    y += 36
    out = io.BytesIO()
    img.crop((0, 0, W, y + 8)).save(out, format="PNG", optimize=True)
    return out.getvalue()
