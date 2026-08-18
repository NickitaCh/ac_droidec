import asyncio
import hashlib
import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import aiohttp
import disnake
from bs4 import BeautifulSoup
from disnake.ext import commands, tasks

import database

MSK = ZoneInfo("Europe/Moscow")

GOHGG_USER_AGENT = "Mozilla/5.0 (compatible; ACDroidecBot/1.0; +https://github.com/)"
GOHGG_MAX_PAGES = 15  # потолок страниц за один прогон, чтобы не уйти в бесконечный скролл
BOT_STATE_KEY = "gohgg_activity_last_seen"

_APPLIED_RE = re.compile(r"^applied (Zeta|Omicron)\s*:\s*(.+?)(?:\s+\d+/\d+\s+unit.*)?$")
_STAR_RE = re.compile(r"^raised to (\d+)★ from (\d+)★$")
_LEVEL_RE = re.compile(r"^raised to Level (\d+) from (\d+)$")
_ERA_RE = re.compile(r"^raised to Era (\d+) from (\d+)$")
_RELIC_RE = re.compile(r"^raised to Relic (\d+) from (\d+)$")


def _gear_tier(span_tag) -> str | None:
    for cls in span_tag.get("class", []):
        if cls.startswith("gear-name--t-"):
            return cls.rsplit("-", 1)[-1]
    return None


def _parse_change(desc_div) -> tuple[str, str | None, str] | None:
    """Разбирает div с текстом изменения ('raised to ... from ...' / 'applied Zeta/Omicron: ...')
    на (action_type, old_value, new_value). Возвращает None для нераспознанного формата —
    вёрстка swgoh.gg сторонняя и может поменяться, лучше молча пропустить строку, чем упасть."""
    text = desc_div.get_text(" ", strip=True)

    if text.startswith("raised to"):
        gear_spans = [sp for sp in desc_div.find_all("span") if _gear_tier(sp)]
        if len(gear_spans) >= 2:
            return "gear", _gear_tier(gear_spans[1]), _gear_tier(gear_spans[0])
        for regex, action in ((_STAR_RE, "star"), (_LEVEL_RE, "level"), (_ERA_RE, "era"), (_RELIC_RE, "relic")):
            m = regex.match(text)
            if m:
                return action, m.group(2), m.group(1)
        return None

    if text.startswith("applied"):
        m = _APPLIED_RE.match(text)
        if m:
            # old_value="" (не None) — в UNIQUE-индексе guild_activity_events каждый NULL
            # уникален сам по себе (стандартное поведение SQLite), из-за чего INSERT OR
            # IGNORE не смог бы задедуплицировать повторные зета/омикрон-события с NULL.
            return m.group(1).lower(), "", m.group(2).strip()
        return None

    return None


def parse_activity_page(html: str) -> list[dict]:
    """Возвращает [{"ally_code", "base_id", "action_type", "old_value", "new_value", "date_label"}, ...]
    в порядке появления на странице (свежие сначала)."""
    soup = BeautifulSoup(html, "html.parser")
    main = soup.find("main")
    if main is None:
        return []

    container = None
    for div in main.find_all("div"):
        classes = div.get("class") or []
        if "grid" in classes and "gap-1.5" in classes:
            container = div
            break
    if container is None:
        return []

    events = []
    current_date_label = None
    for child in container.find_all("div", recursive=False):
        classes = child.get("class") or []

        if "col-span-full" in classes:
            for sp in child.find_all("span"):
                txt = sp.get_text(strip=True)
                if txt:
                    current_date_label = txt
                    break
            continue

        if "rounded-md" not in classes:
            continue

        header_link = child.find("a", href=lambda h: h and h.startswith("/p/") and h.endswith("/activity/"))
        if header_link is None:
            continue
        ally_code = header_link["href"].strip("/").split("/")[1]

        for row in child.find_all("div", recursive=False):
            row_classes = row.get("class") or []
            if "relative" not in row_classes:
                continue
            unit_link = row.find("a", href=lambda h: h and "/unit/" in h)
            desc_div = row.find("div", class_="flex-1")
            if unit_link is None or desc_div is None:
                continue
            base_id = unit_link["href"].rstrip("/").split("/")[-1]

            parsed = _parse_change(desc_div)
            if parsed is None:
                continue
            action_type, old_value, new_value = parsed
            events.append({
                "ally_code": ally_code,
                "base_id": base_id,
                "action_type": action_type,
                "old_value": old_value,
                "new_value": new_value,
                "date_label": current_date_label,
            })

    return events


def _resolve_event_date(label: str | None, today) -> str:
    if label == "Today" or label is None:
        d = today
    elif label == "Yesterday":
        d = today - timedelta(days=1)
    else:
        try:
            d = datetime.strptime(label, "%b %d").date().replace(year=today.year)
            if d > today:
                d = d.replace(year=today.year - 1)
        except ValueError:
            d = today
    return d.isoformat()


def _fingerprint(*parts) -> str:
    return hashlib.sha1("|".join("" if p is None else str(p) for p in parts).encode()).hexdigest()


class GohggActivityCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.session: aiohttp.ClientSession | None = None
        self.scrape_loop.start()

    def cog_unload(self):
        self.scrape_loop.cancel()
        if self.session and not self.session.closed:
            asyncio.create_task(self.session.close())

    async def _scrape_guild(self, guild_id: int, gg_id: str, gname: str):
        last_seen = database.get_bot_state(BOT_STATE_KEY, guild_id=guild_id)
        today = datetime.now(MSK).date()
        newest_fingerprint = None
        total_new = 0
        stop = False

        for page in range(1, GOHGG_MAX_PAGES + 1):
            url = f"https://swgoh.gg/g/{gg_id}/activity/"
            if page > 1:
                url += f"?page={page}"
            try:
                async with self.session.get(url, timeout=aiohttp.ClientTimeout(total=20)) as resp:
                    if resp.status != 200:
                        print(f"[gohgg_activity] {gname}: HTTP {resp.status} на странице {page}, останавливаюсь")
                        break
                    html = await resp.text()
            except Exception as e:
                print(f"[gohgg_activity] {gname}: сбой запроса страницы {page}: {e}")
                break

            raw_events = parse_activity_page(html)
            if not raw_events:
                break

            for ev in raw_events:
                event_date = _resolve_event_date(ev["date_label"], today)
                fp = _fingerprint(guild_id, ev["ally_code"], ev["base_id"], ev["action_type"],
                                   ev["old_value"], ev["new_value"], event_date)
                if newest_fingerprint is None:
                    newest_fingerprint = fp
                if last_seen and fp == last_seen:
                    stop = True
                    break
                if database.add_guild_activity_event(
                    guild_id=guild_id, ally_code=ev["ally_code"], base_id=ev["base_id"],
                    action_type=ev["action_type"], old_value=ev["old_value"], new_value=ev["new_value"],
                    event_date=event_date,
                ):
                    total_new += 1

            if stop:
                break

        if newest_fingerprint:
            database.set_bot_state(BOT_STATE_KEY, newest_fingerprint, guild_id=guild_id)
        if total_new:
            print(f"[gohgg_activity] {gname}: добавлено {total_new} новых событий активности")

    @tasks.loop(hours=1)
    async def scrape_loop(self):
        if self.session is None:
            self.session = aiohttp.ClientSession(headers={"User-Agent": GOHGG_USER_AGENT})

        for guild_cfg in database.get_all_guild_configs():
            gg_id = guild_cfg.get("swgoh_gg_guild_id")
            if not gg_id:
                continue
            try:
                await self._scrape_guild(guild_cfg["id"], gg_id, guild_cfg["name"])
            except Exception as e:
                print(f"[gohgg_activity] Ошибка скрапинга гильдии {guild_cfg['name']}: {e}")

    @scrape_loop.before_loop
    async def before_scrape_loop(self):
        await self.bot.wait_until_ready()


def setup(bot):
    bot.add_cog(GohggActivityCog(bot))
