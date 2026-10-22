"""Сокращения юнитов, как их пишут в чате и понимает HotBot: «JML» и «млюк» →
Мастер-джедай Люк Скайуокер. Имена юнитов не меняются — это только дополнительные
ключи поиска: автодополнение юнитов в Discord (database.search_game_units) и поиск
Telegram-бота (tg_bot/search.py, команды и инлайн-подсказки).

Сравнение — без учёта регистра, «ё», дефисов и пробелов (см. normalize). Новое
сокращение — просто ещё одна строка в списке юнита; одно сокращение не должно
указывать на два юнита (проверяется при импорте)."""

import re

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


_BY_KEY: dict[str, str] = {}
_PREFIX_KEYS: list[tuple[str, str]] = []  # только однословные: «кайло» не должно выводить «кайло без маски» первым
for _base_id, _aliases in ALIASES.items():
    for _alias in _aliases:
        _key = normalize(_alias)
        if _BY_KEY.get(_key, _base_id) != _base_id:
            raise ValueError(f"Сокращение «{_alias}» указывает и на {_BY_KEY[_key]}, и на {_base_id}")
        _BY_KEY[_key] = _base_id
        if " " not in _alias.strip():
            _PREFIX_KEYS.append((_key, _base_id))


def aliases(base_id: str) -> tuple[str, ...]:
    return ALIASES.get(base_id, ())


def exact(query: str) -> str | None:
    """base_id юнита, если запрос целиком — его сокращение («jml», «Млюк»)."""
    return _BY_KEY.get(normalize(query))


def matching(query: str) -> list[str]:
    """base_id юнитов, у которых однословное сокращение начинается с запроса; точное
    совпадение (любое, и из нескольких слов) первым. Пустой запрос — ничего (иначе в подсказки попадёт весь список)."""
    q = normalize(query)
    if not q:
        return []
    hit = _BY_KEY.get(q)
    out = [hit] if hit else []
    for key, base_id in _PREFIX_KEYS:
        if key.startswith(q) and base_id not in out:
            out.append(base_id)
    return out
