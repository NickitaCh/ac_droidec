"""Сокращения юнитов, как их пишут в чате и понимает HotBot: «JML» и «млюк» →
Мастер-джедай Люк Скайуокер. Имена юнитов не меняются — это только дополнительные
ключи поиска: автодополнение юнитов в Discord (database.search_game_units) и поиск
Telegram-бота (tg_bot/search.py, команды и инлайн-подсказки).

Сравнение — без учёта регистра, «ё», дефисов и пробелов (см. normalize). ALIASES —
встроенный список; супер-админ правит и дополняет его на /admin/unit-aliases (таблица
unit_aliases в БД, правка заменяет список юнита целиком). Одно сокращение не может
указывать на два юнита: встроенный список проверяется при импорте, правки — при сохранении."""

import re
import time

ALIASES: dict[str, tuple[str, ...]] = {
    # Галактические легенды
    "GRANDMASTERLUKE": ("JML", "JMLS", "млюк", "мл люк", "мастер люк", "джмл"),
    "SUPREMELEADERKYLOREN": ("SLKR", "слкр", "вл", "влкр", "вл кайло", "верховный кайло"),
    "GLREY": ("GL Rey", "GLRey", "гл рей", "глрей"),
    "SITHPALPATINE": ("SEE", "сее", "ивс", "вечный палпатин", "гл палпатин"),
    "JEDIMASTERKENOBI": ("JMK", "джмк", "мк", "мастер кеноби"),
    "LORDVADER": ("LV", "лв"),
    "JABBATHEHUTT": ("GL Jabba", "гл джабба"),
    "GLLEIA": ("GL Leia", "GLLeia", "гл лея", "глея"),
    "GLAHSOKATANO": ("GL Ahsoka", "GLAT", "гл асока", "глат"),
    "GLHONDO": ("GL Hondo", "PKH", "гл хондо", "король хондо"),
    # Лидеры и популярные отряды
    "JEDIKNIGHTLUKE": ("JKL", "джкл", "ржл", "рыцарь люк"),
    "JEDIKNIGHTREVAN": ("JKR", "джкр", "ржр", "рыцарь реван"),
    "DARTHREVAN": ("DR", "др"),
    "GENERALSKYWALKER": ("GAS", "гас", "генерал энакин"),
    "COMMANDERLUKESKYWALKER": ("CLS", "клс", "командир люк"),
    "COMMANDERAHSOKA": ("CAT", "кат", "командир асока"),
    "GRANDMASTERYODA": ("GMY", "вмй", "гмй", "магистр йода"),
    "JEDIMASTERMACEWINDU": ("JMMW", "мастер мейс"),
    "EMPERORPALPATINE": ("EP", "эп", "палп"),
    "GRANDADMIRALTHRAWN": ("GAT", "гат"),
    "GRANDMOFFTARKIN": ("GMT", "гмт"),
    "ADMIRALACKBAR": ("AA", "аа"),
    "ASAJVENTRESS": ("AV", "ав"),
    "MANDALORBOKATAN": ("BKM", "бкм"),
    "THEMANDALORIANBESKARARMOR": ("BAM", "бам", "бескар", "бескар мандо"),
    "THEMANDALORIAN": ("Mando", "мандо"),
    "MOFFGIDEONS3": ("DTMG", "дтмг", "темный гидеон"),
    "BOBAFETTSCION": ("BFSJ", "SoJ", "боба потомок"),
    "GENERALKENOBI": ("GK", "гк"),
    "GRIEVOUS": ("GG", "гг"),
    "GEONOSIANBROODALPHA": ("GBA", "гба"),
    "JEDIKNIGHTCAL": ("JKCK", "джккк", "рыцарь кэл"),
    "KYLORENUNMASKED": ("KRU", "кру", "кайло без маски"),
    "FIRSTORDEREXECUTIONER": ("FOE", "FOX", "фое"),
    "FIRSTORDEROFFICERMALE": ("FOO", "фоо"),
    "FIRSTORDERSPECIALFORCESPILOT": ("FOSFTP", "сф пилот"),
    "FIRSTORDERTIEPILOT": ("FOTP",),
    "FIRSTORDERTROOPER": ("FOST",),
    "EPIXFINN": ("RH Finn", "RHF", "герой финн"),
    "EPIXPOE": ("RH Poe", "RHP", "герой по"),
    "HOTHHAN": ("CHS", "кхс"),
    "HOTHLEIA": ("ROLO", "роло"),
    "SMUGGLERHAN": ("Vet Han", "вет хан"),
    "SMUGGLERCHEWBACCA": ("Vet Chewie", "вет чуи"),
    "CHEWBACCALEGENDARY": ("Chewie", "чуи"),
    "C3POLEGENDARY": ("3PO", "трипио"),
    "R2D2_LEGENDARY": ("R2", "р2", "арту"),
    "OLDBENKENOBI": ("Old Ben", "старый бен"),
    "HERMITYODA": ("HY",),
    "BASTILASHANDARK": ("Fallen Bastila", "падшая бастила"),
    "REYJEDITRAINING": ("RJT", "рджт"),
    "JEDIKNIGHTGUARDIAN": ("JKG",),
    "JEDIKNIGHTCONSULAR": ("JC",),
    # Флагманы и корабли
    "CAPITALEXECUTOR": ("Exec", "экзекутор", "экз"),
    "CAPITALPROFUNDITY": ("Prof", "профундити", "проф"),
    "CAPITALLEVIATHAN": ("Levi", "лева"),
    "CAPITALMALEVOLENCE": ("малеволенс",),
    "CAPITALNEGOTIATOR": ("Nego", "нега", "негошиэйтор"),
    "CAPITALJEDICRUISER": ("эндуранс",),
    "CAPITALFINALIZER": ("финалайзер",),
    "CAPITALMONCALAMARICRUISER": ("хоум ван",),
    "CAPITALSTARDESTROYER": ("экзекутрикс",),
    "MILLENNIUMFALCON": ("HMF", "сокол хана"),
    "MILLENNIUMFALCONEP7": ("RMF", "сокол рей"),
    "MILLENNIUMFALCONPRISTINE": ("LMF", "сокол ландо", "сокол лэндо"),
    "HOUNDSTOOTH": ("хаунд", "хаундс тус"),
}

_PUNCT_RE = re.compile(r"[\s\-‐–—'’`\"().,:;!?«»/&]+")


def normalize(text: str) -> str:
    """Ключ сравнения: регистр, «ё», дефисы и пробелы не важны («GL Rey» = «glrey»)."""
    return _PUNCT_RE.sub("", (text or "").casefold().replace("ё", "е"))


MAX_ALIAS_LEN = 40
_RELOAD_CHECK_SECONDS = 30
_CYRILLIC_RE = re.compile(r"[а-яё]", re.IGNORECASE)


def is_russian(alias: str) -> bool:
    return bool(_CYRILLIC_RE.search(alias))


def _build_index(table: dict) -> tuple[dict[str, str], list[tuple[str, str]]]:
    by_key: dict[str, str] = {}
    prefix_keys: list[tuple[str, str]] = []  # только однословные: «кайло» не должно выводить «кайло без маски» первым
    for base_id, items in table.items():
        for alias in items:
            key = normalize(alias)
            if not key:
                continue
            if by_key.get(key, base_id) != base_id:
                raise ValueError(f"Сокращение «{alias}» указывает и на {by_key[key]}, и на {base_id}")
            by_key[key] = base_id
            if " " not in alias.strip():
                prefix_keys.append((key, base_id))
    return by_key, prefix_keys


_build_index(ALIASES)  # встроенный список проверяется на дубли при импорте

# Действующий список = встроенный + правки из /admin/unit-aliases (database.unit_aliases).
# Бот, сайт и Telegram — разные процессы, поэтому версия правок перечитывается из БД
# не чаще раза в _RELOAD_CHECK_SECONDS; после сохранения на сайте — invalidate().
_state = {"checked": 0.0, "version": None, "table": dict(ALIASES), "custom": {}, "index": _build_index(ALIASES)}


def _current() -> dict:
    now = time.time()
    if now - _state["checked"] < _RELOAD_CHECK_SECONDS:
        return _state
    _state["checked"] = now
    try:
        import database  # не на уровне модуля: database импортирует этот модуль
        version = database.get_unit_aliases_version()
        if version == _state["version"]:
            return _state
        custom = database.get_custom_unit_aliases()
        table = {**ALIASES, **{b: tuple(a) for b, a in custom.items()}}
        index = _build_index(table)
    except Exception as e:
        print(f"⚠️ [unit_aliases] Правки сокращений не загружены: {e}")
        return _state
    _state.update(version=version, table=table, custom=custom, index=index)
    return _state


def invalidate():
    _state["checked"] = 0.0
    _state["version"] = None


def table() -> dict[str, tuple[str, ...]]:
    """Действующие сокращения {base_id: (…)} — для страницы админки."""
    return {b: a for b, a in _current()["table"].items() if a}


def custom_ids() -> set[str]:
    """Юниты, чей список правили на сайте (не встроенный)."""
    return set(_current()["custom"])


def aliases(base_id: str) -> tuple[str, ...]:
    return _current()["table"].get(base_id, ())


def exact(query: str) -> str | None:
    """base_id юнита, если запрос целиком — его сокращение («jml», «Млюк»)."""
    return _current()["index"][0].get(normalize(query))


def matching(query: str) -> list[str]:
    """base_id юнитов, у которых однословное сокращение начинается с запроса; точное
    совпадение (любое, и из нескольких слов) первым. Пустой запрос — ничего (иначе в
    подсказки попадёт весь список)."""
    q = normalize(query)
    if not q:
        return []
    by_key, prefix_keys = _current()["index"]
    hit = by_key.get(q)
    out = [hit] if hit else []
    for key, base_id in prefix_keys:
        if key.startswith(q) and base_id not in out:
            out.append(base_id)
    return out


def parse(text: str) -> list[str]:
    """«JML, JMLS» → ['JML', 'JMLS']: через запятую или с новой строки, без повторов."""
    out, seen = [], set()
    for part in re.split(r"[,;\n]+", text or ""):
        alias = " ".join(part.split())
        if alias and normalize(alias) not in seen:
            seen.add(normalize(alias))
            out.append(alias)
    return out


def validate(base_id: str, items: list[str], names: dict | None = None) -> str | None:
    """Текст ошибки, если сокращение длинное, пустое после очистки или уже занято другим юнитом."""
    by_key = _current()["index"][0]
    for alias in items:
        if len(alias) > MAX_ALIAS_LEN:
            return f"«{alias}» длиннее {MAX_ALIAS_LEN} символов."
        if not normalize(alias):
            return f"«{alias}» — одни знаки препинания."
        owner = by_key.get(normalize(alias))
        if owner and owner != base_id:
            return f"«{alias}» уже занято: {(names or {}).get(owner) or owner}."
    return None
