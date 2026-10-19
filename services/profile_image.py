"""Картинка для /профиль (PNG через Pillow) — по сводке из services/player_profile.py.

Рисуем на заведомо высоком холсте, ведём курсор y и в конце обрезаем по нему — без
отдельного прохода раскладки. Портреты — тот же дисковый кэш, что у сайта
(cache/unit_img/<base_id>.png, web/routes/unit_images.py); чего нет — докачиваем с CDN
по game_units.thumbnail_name. Шрифт DejaVu лежит в assets/fonts (в контейнере системных
шрифтов нет, а встроенный шрифт Pillow без кириллицы)."""

import io
import re
from pathlib import Path

import httpx
from PIL import Image, ImageDraw, ImageFont

import database
from services import player_profile as pp

ROOT = Path(__file__).resolve().parent.parent
FONT_DIR = ROOT / "assets" / "fonts"
PORTRAIT_DIR = ROOT / "cache" / "unit_img"
CDN_URL = "https://game-assets.swgoh.gg/textures/{name}.png"
_TEXTURE_RE = re.compile(r"^tex\.[A-Za-z0-9_-]{1,80}$")

W, PAD, GAP = 1000, 20, 8
BG = (21, 23, 28)
PANEL = (34, 37, 44)
PANEL_ALT = (40, 44, 52)
LABEL_BG = (201, 164, 106)
LABEL_FG = (33, 26, 15)
TEXT = (236, 236, 240)
MUTED = (160, 166, 178)
GOLD = (230, 184, 92)
HEADER_TOP, HEADER_BOTTOM = (226, 172, 80), (176, 120, 40)
GREEN, RED = (120, 200, 130), (230, 110, 100)
RELIC_BG = (150, 40, 40)
GEAR_BG = (70, 90, 140)

_fonts: dict = {}


def _font(size: int, bold: bool = False, condensed: bool = False):
    key = (size, bold, condensed)
    if key not in _fonts:
        name = "DejaVuSans" + ("Condensed" if condensed else "") + ("-Bold" if bold else "") + ".ttf"
        _fonts[key] = ImageFont.truetype(str(FONT_DIR / name), size)
    return _fonts[key]


# ---------------------------------------------------------------- числа

def _int(n) -> str:
    return f"{int(round(n)):,}".replace(",", " ") if n is not None else "—"


def _dec(n, digits: int = 1) -> str:
    return f"{n:.{digits}f}".replace(".", ",") if n is not None else "—"


def compact(n) -> str:
    """238,7к / 13,76 млн / 703 млн / 1,2 млрд — коротко, с десятичной запятой."""
    if n is None:
        return "—"
    if n >= 1e9:
        return f"{_dec(n / 1e9, 2)} млрд"
    if n >= 1e8:
        return f"{_dec(n / 1e6, 0)} млн"
    if n >= 1e6:
        return f"{_dec(n / 1e6, 2)} млн"
    if n >= 1e4:
        return f"{_dec(n / 1e3, 1)}к"
    return _int(n)


def short(n) -> str:
    """Для карточек юнитов: 238,7к / 6,1к / 1,2м — всегда коротко."""
    if n is None:
        return "—"
    if n >= 1e6:
        return f"{_dec(n / 1e6, 1)}м"
    if n >= 1e3:
        return f"{_dec(n / 1e3, 1)}к"
    return _int(n)


def _pct(fraction) -> str:
    return f"{_int(fraction * 100)}%" if fraction is not None else "—"


# ---------------------------------------------------------------- портреты

def _portrait(base_id: str, size: int):
    path = PORTRAIT_DIR / f"{base_id}.png"
    if not path.exists():
        name = database.get_unit_thumbnail(base_id)
        if not name or not _TEXTURE_RE.match(name):
            return None
        try:
            resp = httpx.get(CDN_URL.format(name=name), timeout=10)
        except httpx.HTTPError:
            return None
        if resp.status_code != 200 or not resp.headers.get("content-type", "").startswith("image/"):
            return None
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_bytes(resp.content)
        tmp.replace(path)
    try:
        img = Image.open(path).convert("RGBA")
    except OSError:
        return None
    return img.resize((size, size), Image.LANCZOS)


# ---------------------------------------------------------------- холст

class _Canvas:
    def __init__(self, max_height: int = 12000):
        self.img = Image.new("RGB", (W, max_height), BG)
        self.draw = ImageDraw.Draw(self.img)
        self.y = PAD
        self.gls = set(pp.galactic_legends())

    def png(self) -> bytes:
        out = io.BytesIO()
        self.img.crop((0, 0, W, self.y + PAD)).save(out, format="PNG", optimize=True)
        return out.getvalue()

    def text(self, xy, text, font, fill=TEXT, anchor="la"):
        self.draw.text(xy, text, font=font, fill=fill, anchor=anchor)

    def fit(self, text: str, font, width: int) -> str:
        if self.draw.textlength(text, font=font) <= width:
            return text
        while text and self.draw.textlength(text + "…", font=font) > width:
            text = text[:-1]
        return text + "…"

    # -- блоки

    def banner(self, title: str, subtitle: str):
        h = 110
        box = (PAD, self.y, W - PAD, self.y + h)
        for i in range(h):  # вертикальный градиент: тёмно-красный → почти чёрный
            t = i / h
            color = (int(70 - 50 * t), int(18 - 8 * t), int(16 - 6 * t))
            self.draw.line((PAD, self.y + i, W - PAD, self.y + i), fill=color)
        self.draw.rectangle(box, outline=GOLD, width=2)
        self.text((W // 2, self.y + 44), self.fit(title, _font(40, True), W - 4 * PAD), _font(40, True), anchor="mm")
        self.text((W // 2, self.y + 86), self.fit(subtitle, _font(20), W - 4 * PAD), _font(20), fill=MUTED, anchor="mm")
        self.y += h + GAP * 2

    def section(self, title: str):
        h = 36
        for i in range(h):
            t = i / h
            color = tuple(int(a + (b - a) * t) for a, b in zip(HEADER_TOP, HEADER_BOTTOM))
            self.draw.line((PAD, self.y + i, W - PAD, self.y + i), fill=color)
        self.text((W // 2, self.y + h // 2), title, _font(21, True), fill=LABEL_FG, anchor="mm")
        self.y += h + 4

    def grid(self, cells: list[tuple[str, str]], cols: int = 3, label_ratio: float = 0.58):
        row_h, gap = 34, 4
        col_w = (W - 2 * PAD - gap * (cols - 1)) // cols
        label_w = int(col_w * label_ratio)
        for i, (label, value) in enumerate(cells):
            r, c = divmod(i, cols)
            x = PAD + c * (col_w + gap)
            y = self.y + r * (row_h + gap)
            self.draw.rectangle((x, y, x + label_w - 1, y + row_h), fill=LABEL_BG)
            self.draw.rectangle((x + label_w, y, x + col_w, y + row_h), fill=PANEL if r % 2 == 0 else PANEL_ALT)
            font = _font(18, condensed=True)
            self.text((x + label_w - 8, y + row_h // 2), self.fit(label, font, label_w - 12), font, fill=LABEL_FG, anchor="rm")
            vfont = _font(19, True, condensed=True)
            self.text((x + label_w + 8, y + row_h // 2), self.fit(value, vfont, col_w - label_w - 12), vfont, anchor="lm")
        rows = (len(cells) + cols - 1) // cols
        self.y += rows * (row_h + gap) + GAP * 2

    def units(self, rows: list[dict], guild: bool = False):
        cols, gap = 2, 8
        card_w = (W - 2 * PAD - gap) // cols
        card_h = 132 if guild else 110
        pic = 92
        for i, row in enumerate(rows):
            r, c = divmod(i, cols)
            x = PAD + c * (card_w + gap)
            y = self.y + r * (card_h + gap)
            self.draw.rounded_rectangle((x, y, x + card_w, y + card_h), radius=8, fill=PANEL)
            self._unit_card(row, x, y, card_w, card_h, pic, guild)
        n_rows = (len(rows) + cols - 1) // cols
        self.y += n_rows * (card_h + gap) + GAP

    def _unit_card(self, row: dict, x: int, y: int, w: int, h: int, pic: int, guild: bool):
        px, py = x + 8, y + (h - pic) // 2
        portrait = _portrait(row["base_id"], pic)
        border = GOLD if row["base_id"] in self.gls else (110, 116, 128)
        if portrait is not None:
            mask = Image.new("L", (pic, pic), 0)
            ImageDraw.Draw(mask).rounded_rectangle((0, 0, pic - 1, pic - 1), radius=12, fill=255)
            self.img.paste(portrait, (px, py), Image.composite(portrait.getchannel("A"), mask, mask))
        self.draw.rounded_rectangle((px - 1, py - 1, px + pic, py + pic), radius=12, outline=border, width=2)

        badge = self._badge(row, guild)
        if badge:
            text, color = badge
            bf = _font(15, True)
            tw = int(self.draw.textlength(text, font=bf)) + 12
            bx, by = px + pic - tw + 4, py + pic - 18
            self.draw.rounded_rectangle((bx, by, bx + tw, by + 20), radius=9, fill=color, outline=(20, 20, 20))
            self.text((bx + tw // 2, by + 10), text, bf, anchor="mm")

        tx = px + pic + 14
        tw = x + w - 10 - tx
        name_font = _font(19, True, condensed=True)
        self.text((tx, y + 8), self.fit(row["name"], name_font, tw), name_font)

        small = _font(17, condensed=True)
        bold = _font(17, True, condensed=True)
        speed = row.get("speed")
        speed_text = f"Скор. {_int(speed)}" if speed is not None else ""
        if speed is not None and row.get("speed_mods"):
            speed_text += f" (+{_int(row['speed_mods'])})"
        self.text((tx, y + 33), speed_text, bold, fill=GOLD)
        sx = tx + int(self.draw.textlength(speed_text + "   ", font=bold)) if speed_text else tx
        self.text((sx, y + 33), self.fit(f"ЗД {short(row.get('health'))}", small, x + w - 10 - sx), small)
        line3 = []
        if row.get("protection"):
            line3.append(f"Защ {short(row['protection'])}")
        line3.append(f"Урон {short(row.get('phys'))} / {short(row.get('spec'))}")
        self.text((tx, y + 56), self.fit("  ·  ".join(line3), small, tw), small)
        line4 = [f"Стойк. {_pct(row.get('tenacity'))}", f"Эфф. {_pct(row.get('potency'))}"]
        if not guild:
            if row.get("zetas"):
                line4.append(f"Зет {row['zetas']}")
            if row.get("omicrons"):
                line4.append(f"Омик. {row['omicrons']}")
            if row.get("ultimate"):
                line4.append("Ульта")
        self.text((tx, y + 79), self.fit("  ·  ".join(line4), small, tw), small, fill=MUTED)
        if guild:
            parts = [f"Есть {row['owners']}"]
            if row.get("ultimates"):
                parts.append(f"ульта {row['ultimates']}")
            if not row["is_ship"]:
                parts.append(f"R8+ {row['r8_plus']}")
                if row.get("avg_relic") is not None:
                    parts.append(f"R ср. {_dec(row['avg_relic'])}")
            else:
                parts.append(f"7★ {row['stars7']}")
            self.text((tx, y + 102), self.fit(" · ".join(parts), bold, tw), bold, fill=GREEN)

    @staticmethod
    def _badge(row: dict, guild: bool):
        if guild:
            return None
        if row["is_ship"]:
            return (f"{row['stars']}★", GEAR_BG)
        if row.get("relic") is not None:
            return (f"R{row['relic']}", RELIC_BG)
        return (f"G{row['gear']}", GEAR_BG)


# ---------------------------------------------------------------- страницы

def _relic_cells(rel: dict) -> list:
    return [("Всего", _int(rel["total"])), ("R10", _int(rel["r10"])), ("R9", _int(rel["r9"])),
            ("R8", _int(rel["r8"])), ("R7", _int(rel["r7"])), ("R6", _int(rel["r6"])),
            ("R5", _int(rel["r5"])), ("R1–R4", _int(rel["r1_4"]))]


def _mod_cells(mods: dict) -> list:
    return [("Скорость 25+", _int(mods["s25"])), ("Скорость 20–24", _int(mods["s20_24"])), ("6 точек", _int(mods["six_dot"])),
            ("Скорость 15–19", _int(mods["s15_19"])), ("Скорость 10–14", _int(mods["s10_14"])), ("Скорость 20+", _int(mods["s20"])),
            ("Скорость 15+", _int(mods["s15"])), ("Скорость 10+", _int(mods["s10"])), ("Нападение 6%+", _int(mods["off6"])),
            ("Нападение 4%+", _int(mods["off4"])), ("Защита 14%+", _int(mods["def14"])), ("Защита 9%+", _int(mods["def9"]))]


def _datacron_cells(dc: dict) -> list:
    return [("Перебросов", _int(dc["rerolls"])), ("Уровень 9", _int(dc["l9"])),
            ("Уровень 6–8", _int(dc["l6_8"])), ("Уровень 3–5", _int(dc["l3_5"]))]


def _ally(code: str) -> str:
    return f"{code[:3]}-{code[3:6]}-{code[6:]}" if len(code) == 9 else code


def render_player(s: dict, guild_gp: int | None = None) -> bytes:
    m = s["meta"]
    stats = m.get("stats") or {}
    cv = _Canvas()
    league = pp.LEAGUE_NAMES.get(m.get("league"), m.get("league") or "")
    sub = [f"Код {_ally(m['ally_code'])}"]
    if m.get("guild_name"):
        sub.append(m["guild_name"])
    if league:
        sub.append(f"ВА: {league} {m['division'] or ''}".strip())
    cv.banner(m["name"], "  ·  ".join(sub))

    cv.section("Великая арена")
    cv.grid([
        ("Лига", f"{league} {m.get('division') or ''}".strip() or "—"), ("Рейтинг", _int(m.get("skill_rating"))),
        ("Повышений", _int(stats.get("STAT_SEASON_PROMOTIONS_EARNED_NAME"))),
        ("Побед в атаке", _int(stats.get("STAT_SEASON_OFFENSIVE_BATTLES_WON_NAME"))),
        ("Меньшим составом", _int(stats.get("STAT_SEASON_UNDERSIZED_SQUAD_WINS_NAME"))),
        ("Успешных защит", _int(stats.get("STAT_SEASON_SUCCESSFUL_DEFENDS_NAME"))),
        ("Полных зачисток", _int(stats.get("STAT_SEASON_FULL_CLEAR_ROUND_WINS_NAME"))),
        ("Зон захвачено", _int(stats.get("STAT_SEASON_TERRITORIES_DEFEATED_NAME"))),
        ("ГМ гильдии", compact(guild_gp) if guild_gp else "—"),
    ])

    cv.section("Сводка")
    cv.grid([
        ("ГМ", compact(s["gp"])), ("ГМ персонажей", compact(s["char_gp"])), ("ГМ кораблей", compact(s["ship_gp"])),
        ("Топ-80", compact(s["top80"])), ("Арена", _int(m.get("arena_rank"))), ("Флот", _int(m.get("fleet_rank"))),
        ("Зеты", _int(s["zetas"])), ("Омикроны", _int(s["omicrons"])),
        ("Омик. ВГ / ТБ / ВА", f"{s['omicrons_tw']} / {s['omicrons_tb']} / {s['omicrons_gac']}"),
        ("G13", _int(s["g13"])), ("G12", _int(s["g12"])), ("G11", _int(s["g11"])),
        ("Скор. модов G11+", f"+{_int(s['speed_g11'])}" if s["speed_g11"] is not None else "—"),
        ("Скор. модов топ-80", f"+{_int(s['speed_top80'])}" if s["speed_top80"] is not None else "—"),
    ])

    cv.section("Моды")
    cv.grid(_mod_cells(s["mods"]))
    cv.section("Реликвии")
    cv.grid(_relic_cells(s["relics"]), cols=4)
    cv.section("Датакроны")
    cv.grid(_datacron_cells(s["datacrons"]), cols=4)

    for title, key in (("Галактические легенды", "gls"), ("Мета", "meta_units"), ("Мета флота", "fleet")):
        if s[key]:
            cv.section(title)
            cv.units(s[key])
    return cv.png()


def render_guild(g: dict) -> bytes:
    cv = _Canvas()
    cv.banner(g["name"], f"Участников: {g['members']}  ·  ГМ {compact(g['gp'])}")
    cv.section("Итого")
    cv.grid([
        ("Участников", _int(g["members"])), ("ГМ", compact(g["gp"])), ("Уровней реликвий", _int(g["relics"]["total"])),
        ("ГМ персонажей", compact(g["char_gp"])), ("ГМ кораблей", compact(g["ship_gp"])), ("Омикроны", _int(g["omicrons"])),
        ("Омик. ВГ", _int(g["omicrons_tw"])), ("Омик. ТБ", _int(g["omicrons_tb"])), ("Омик. ВА", _int(g["omicrons_gac"])),
        ("G13", _int(g["g13"])), ("G12", _int(g["g12"])),
    ])
    if g["mods"]:
        cv.section("Моды")
        cv.grid(_mod_cells(g["mods"]))
    cv.section("Реликвии")
    cv.grid(_relic_cells(g["relics"]), cols=4)
    cv.section("Датакроны")
    cv.grid(_datacron_cells(g["datacrons"]), cols=4)
    a = g["avg"]
    cv.section("Средний игрок")
    cv.grid([
        ("ГМ", compact(a["gp"])), ("Топ-80", compact(a["top80"])), ("Уровней релик", _int(a["relics"])),
        ("6 точек", _int(a["six_dot"])), ("Зеты", _int(a["zetas"])), ("G13", _int(a["g13"])),
        ("G12", _int(a["g12"])), ("Арена", _int(a["arena_rank"])), ("Флот", _int(a["fleet_rank"])),
        ("Скор. модов G11+", f"+{_int(a['speed_g11'])}" if a["speed_g11"] is not None else "—"),
    ])
    for title, key in (("Галактические легенды", "gls"), ("Мета", "meta_units"), ("Мета флота", "fleet")):
        if g[key]:
            cv.section(f"{title} — средние у владельцев")
            cv.units(g[key], guild=True)
    return cv.png()
