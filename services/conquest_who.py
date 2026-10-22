"""«Кем пройти?» для подвигов Завоевания (веб /conquest): какие персонажи подходят под
подвиг. В данных игры у подвига нет ссылки на персонажей (challenge.task пуст), есть только
id и текст, поэтому связь восстанавливаем по тексту, по порядку:
1. в id подвига стоит base_id персонажа (CONQUEST_VOL25_SECTOR_BISHOP_…) — он и есть;
2. «полный отряд X» — фракция X (видимые категории, bot_state unit_factions) или сторона Силы;
3. «наложить/получите X» — персонажи, у которых X есть в описании способностей;
4. «сохранив X», «с X в отряде» — персонаж по русскому имени (с учётом падежей);
иначе подвиг общий («без танков», «не потеряв ни одной боевой единицы») — подходит любой отряд.

Считается раз в сутки вместе с подвигами (conquest.sync) и кладётся в состояние
Завоевания: feat["who"] = {kind, label, units, need}."""

import asyncio
import re

import database

FULL_SQUAD = 5
# Эффект, который есть почти у всех («положительные эффекты»), подсказкой не считаем.
TOO_COMMON = 150
_TAG_RE = re.compile(r"\[[0-9A-Fa-f]{6}\]|\[-\]|\[/?[a-z]\]")
_WORD_RE = re.compile(r"[a-zа-я0-9]+")
_EFFECT_VERBS = ("наложить", "наложите", "получите", "получить")
_FILLER = {"попробуйте", "попытайтесь", "наложить", "наложите", "получите", "получить", "на", "врагов",
           "врага", "не", "менее", "раз", "раза", "эффект", "эффекты", "эффектов", "эффекта",
           "способностью", "способностями", "бою", "боях"}
_SIDES = {"светл": (2, "Светлая сторона"), "темн": (3, "Темная сторона")}


def words(text: str) -> list[str]:
    text = _TAG_RE.sub("", (text or "").replace("\\n", " ")).lower().replace("ё", "е")
    return _WORD_RE.findall(text)


def word_match(a: str, b: str) -> bool:
    """Одно слово в разных падежах для эффектов: «длительного лечения»/«длительное лечение»;
    короткие слова — только точно, иначе «нему» совпадает с «немощность»."""
    if a == b:
        return True
    if len(a) < 4 or len(b) < 4 or a.isdigit() or b.isdigit() or abs(len(a) - len(b)) > 4:
        return False
    common = 0
    for x, y in zip(a, b):
        if x != y:
            break
        common += 1
    return common >= max(3, min(len(a), len(b)) - 3)


def name_word_match(name_word: str, text_word: str) -> bool:
    """Слово имени (именительный падеж) в тексте подвига: окончание может отличаться на
    2 буквы и удлиниться не больше чем на 4 («страж» → «стражника»), у коротких слов —
    меняется только последняя буква («лея» → «лею», «зеб» → «зебом»), иначе «Босск» находится в «бой»."""
    if name_word == text_word:
        return True
    n = len(name_word)
    if n < 3 or name_word.isdigit() or len(text_word) > n + (4 if n > 3 else 2):
        return False
    common = 0
    for x, y in zip(name_word, text_word):
        if x != y:
            break
        common += 1
    return common >= (n - 1 if n <= 3 else max(3, n - 2))


def _contains_name(seq: list[str], phrase: list[str]) -> bool:
    n = len(phrase)
    return any(all(name_word_match(phrase[j], seq[i + j]) for j in range(n)) for i in range(len(seq) - n + 1))


def _contains(seq: list[str], phrase: list[str]) -> bool:
    n = len(phrase)
    return any(all(word_match(phrase[j], seq[i + j]) for j in range(n)) for i in range(len(seq) - n + 1))


def _name_score(name_words: list[str], desc_words: list[str]) -> float:
    """Доля слов имени, найденных в тексте; первое слово имени обязано найтись."""
    if not name_words or not any(name_word_match(name_words[0], d) for d in desc_words):
        return 0.0
    hit = sum(1 for w in name_words if any(name_word_match(w, d) for d in desc_words))
    return hit / len(name_words)


def _best_names(candidates: dict, desc_words: list[str], threshold: float) -> list[str]:
    """Самые подходящие по имени: лучшая доля, среди равных — самые длинные, и без тех, чьё
    имя целиком входит в имя другого найденного («Грогу» при «Грогу и анзелланы»)."""
    scored = [(bid, _name_score(ws, desc_words), ws) for bid, ws in candidates.items()]
    scored = [s for s in scored if s[1] >= threshold]
    if not scored:
        return []
    best = max(s[1] for s in scored)
    top = [s for s in scored if s[1] == best]
    out = []
    for bid, _score, ws in top:
        if any(len(other) > len(ws) and all(any(name_word_match(w, o) or name_word_match(o, w) for o in other) for w in ws)
               for _b, _s, other in top):
            continue
        out.append(bid)
    return out


def _feat_token(feat_id: str) -> str:
    m = re.search(r"_(?:EVENT|SECTOR|BOSS|MINIBOSS)_(.+?)_I{1,3}_DIFF", feat_id)
    return m.group(1) if m else ""


def match(feat: dict, chars: dict, names: dict, factions: dict, ability_words: dict) -> dict:
    """chars — {base_id: {"alignment"}} (только персонажи), names — {base_id: [слова имени]},
    factions — {category_id: {"name", "units"}}, ability_words — {base_id: [слова способностей]}."""
    desc = words(feat.get("desc"))
    token = _feat_token(feat["id"])
    if token in chars:
        return {"kind": "unit", "label": "", "units": [token], "need": 1}

    if any(word_match("полный", w) for w in desc) and any(word_match("отряд", w) for w in desc):
        fac_words = {cid: words(f["name"]) for cid, f in factions.items()}
        hits = [cid for cid, ws in fac_words.items() if _contains_name(desc, ws)]
        if hits:
            cid = max(hits, key=lambda c: len(" ".join(fac_words[c])))
            return {"kind": "faction", "label": factions[cid]["name"], "units": factions[cid]["units"], "need": FULL_SQUAD}
        sides = [v for stem, v in _SIDES.items() if any(w.startswith(stem) for w in desc)]
        if len(sides) == 1:
            align, label = sides[0]
            return {"kind": "faction", "label": label, "need": FULL_SQUAD,
                    "units": sorted(b for b, c in chars.items() if c.get("alignment") == align)}

    if any(w in _EFFECT_VERBS for w in desc):
        phrase = [w for w in desc if w not in _FILLER and not w.isdigit()]
        if phrase:
            units = sorted(b for b, ws in ability_words.items() if b in chars and _contains(ws, phrase))
            if units and len(units) <= TOO_COMMON:
                return {"kind": "effect", "label": " ".join(phrase), "units": units, "need": 1}
        return {"kind": "any", "label": "", "units": [], "need": 0}

    units = _best_names({b: ws for b, ws in names.items() if b in chars}, desc, 0.6)
    if units:
        return {"kind": "unit", "label": "", "units": units, "need": 1}
    return {"kind": "any", "label": "", "units": [], "need": 0}


def unit_ability_words(units: list, skills: list, abilities: list, loc: dict) -> dict:
    """{base_id: слова всех описаний способностей (последний уровень)}."""
    ability_of_skill = {s["id"]: s.get("abilityReference") for s in skills if s.get("id")}
    ability_by_id = {a["id"]: a for a in abilities if a.get("id")}
    out = {}
    for u in units:
        text = []
        for ref in u.get("skillReference") or []:
            a = ability_by_id.get(ability_of_skill.get(ref.get("skillId")))
            if not a:
                continue
            tiers = a.get("tier") or []
            key = (tiers[-1].get("descKey") if tiers else None) or a.get("descKey")
            text.append(loc.get(key or "", ""))
        out[u["baseId"]] = words(" ".join(text))
    return out


async def annotate(comlink, state: dict, units: list, loc: dict) -> None:
    """Дописывает feat["who"] во все подвиги состояния. units — канонические юниты игры."""
    from services import faction
    from services.units_sync import ABILITY_DEFINITIONS_FLAG, SKILL_DEFINITIONS_FLAG

    def _data(flag):
        return comlink.get_game_data(items=str(flag), include_pve_units=False)

    skills = (await asyncio.to_thread(_data, SKILL_DEFINITIONS_FLAG)).get("skill") or []
    abilities = (await asyncio.to_thread(_data, ABILITY_DEFINITIONS_FLAG)).get("ability") or []
    chars_list = [u for u in units if u.get("combatType") == 1]
    chars = {u["baseId"]: {"alignment": u.get("forceAlignment")} for u in chars_list}
    names = {b: words(n) for b, n in database.get_game_unit_names(list(chars)).items()}
    ability_words = unit_ability_words(chars_list, skills, abilities, loc)
    facs = faction.factions()
    for d in state["difficulties"]:
        for f in d["feats"]:
            f["who"] = match(f, chars, names, facs, ability_words)
