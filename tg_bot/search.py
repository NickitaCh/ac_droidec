"""Поиск юнитов, целей «Руководства» и фракций по тому, как их пишут в чате: без учёта
регистра, «ё», дефисов и порядка слов («дарт джа джа» → «Дарт Джа-Джа», «джа дарт» тоже),
по русскому и английскому имени и base_id. Общий для команд и инлайн-подсказок
(tg_bot/inline.py) — в Discord ту же роль играет автодополнение параметров."""

import re
import sqlite3
import time

import database
from services import faction as faction_svc
from services import journey_guide

_PUNCT_RE = re.compile(r"[\s\-‐–—'’`\"().,:;!?«»/]+")
_CACHE_TTL = 600
_units_cache: tuple[float, list] = (0.0, [])


def norm(text: str) -> str:
    return _PUNCT_RE.sub(" ", (text or "").casefold().replace("ё", "е")).strip()


def _rank(query: str, *names: str) -> int | None:
    """0 — точное совпадение, 1 — имя начинается с запроса, 2 — каждое слово запроса —
    начало какого-то слова имени, 3 — запрос где-то внутри; None — не подходит."""
    q = norm(query)
    if not q:
        return None
    q_words = q.split()
    best = None
    for name in names:
        n = norm(name)
        if not n:
            continue
        if n == q:
            rank = 0
        elif n.startswith(q):
            rank = 1
        elif all(any(w.startswith(qw) for w in n.split()) for qw in q_words):
            rank = 2
        elif q in n or q.replace(" ", "") in n.replace(" ", ""):
            rank = 3
        else:
            continue
        best = rank if best is None else min(best, rank)
    return best


def _all_units() -> list[tuple[str, str, str, str]]:
    """[(base_id, имя, английское имя, тип)] — кэш на 10 минут (~1200 строк)."""
    global _units_cache
    if time.time() - _units_cache[0] < _CACHE_TTL and _units_cache[1]:
        return _units_cache[1]
    conn = sqlite3.connect(database.DB_NAME)
    rows = conn.execute("SELECT base_id, cached_name, cached_name_en FROM game_units").fetchall()
    conn.close()
    types = database.get_unit_types([r[0] for r in rows])
    units = [(b, n or b, en or "", types.get(b) or "") for b, n, en in rows]
    _units_cache = (time.time(), units)
    return units


def units(query: str, limit: int = 50, exact_only: bool = True) -> list[tuple[str, str, bool]]:
    """[(base_id, имя, корабль?)] лучшие сверху. exact_only — при точном совпадении только
    оно (команда «/unit Мол» не спрашивает, какой из Молов); подсказкам нужны все."""
    hits = []
    for base_id, name, name_en, kind in _all_units():
        rank = _rank(query, name, name_en, base_id)
        if rank is not None:
            hits.append((rank, len(name), name, base_id, kind == "ship"))
    hits.sort()
    if exact_only and hits and hits[0][0] == 0:
        hits = [h for h in hits if h[0] == 0]
    return [(b, n, ship) for _r, _l, n, b, ship in hits[:limit]]


def guides(query: str, guild_id: int | None = None, limit: int = 50, exact_only: bool = True) -> list[dict]:
    """Цели «Руководства» (journey_guide.guide_options), у которых есть требования."""
    hits = []
    for g in journey_guide.guide_options():
        rank = _rank(query, g["name"], g["base_id"]) if query else 3
        if rank is None:
            continue
        if guild_id is not None and not database.get_journey_guide_requirements(g["base_id"], guild_id):
            continue
        hits.append((rank, g["name"], g))
    hits.sort(key=lambda h: (h[0], h[1]))
    if exact_only and hits and hits[0][0] == 0:
        hits = [h for h in hits if h[0] == 0]
    return [g for _r, _n, g in hits[:limit]]


def factions(query: str, limit: int = 50, exact_only: bool = True) -> list[tuple[str, str, int]]:
    """[(category_id, название, персонажей)]."""
    hits = []
    for cid, f in faction_svc.factions().items():
        rank = _rank(query, f["name"], cid) if query else 3
        if rank is not None:
            hits.append((rank, f["name"], cid, len(f["units"])))
    hits.sort()
    if exact_only and hits and hits[0][0] == 0:
        hits = [h for h in hits if h[0] == 0]
    return [(cid, name, count) for _r, name, cid, count in hits[:limit]]
