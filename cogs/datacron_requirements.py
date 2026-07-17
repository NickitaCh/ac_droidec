import asyncio
import re
import time

import disnake
from disnake.ext import commands, tasks

import database
from cogs.violations import autocomplete_players

# 'DatacronDefinitions' — бит из comlink.get_enums()["GameDataItemsEnum"], проверено вживую.
DATACRON_DEFINITIONS_FLAG = 70368744177664
DATACRON_LEVELS = (3, 6, 9)

DATACRON_ANY = "ANY"
DATACRON_NONE = "NONE"
DATACRON_ANY_LABEL = "Любой (тир разблокирован, бонус неважен)"
DATACRON_NONE_LABEL = "- (не проверять этот уровень)"

PRIORITY_REQUIRED = "required"
PRIORITY_OPTIONAL = "optional"
PRIORITY_USEFUL = "useful"
PRIORITY_ORDER = (PRIORITY_REQUIRED, PRIORITY_OPTIONAL, PRIORITY_USEFUL)
PRIORITY_LABELS = {
    PRIORITY_REQUIRED: "Обязательно",
    PRIORITY_OPTIONAL: "По желанию",
    PRIORITY_USEFUL: "Полезные",
}
PRIORITY_CHOICES = [disnake.OptionChoice(name=label, value=key) for key, label in PRIORITY_LABELS.items()]
PRIORITY_EMOJI = {
    PRIORITY_REQUIRED: "🔴",
    PRIORITY_OPTIONAL: "🟡",
    PRIORITY_USEFUL: "🟢",
}

DATACRON_LIST_COLOR = 0x3498DB
DATACRON_CHECK_COLOR_FULL = 0x2ECC71
DATACRON_CHECK_COLOR_PARTIAL = 0xF1C40F
DATACRON_CHECK_COLOR_NONE = 0xE74C3C

EMBED_FIELD_LIMIT = 24  # держим запас от жёсткого лимита Discord в 25 полей на embed
EMBED_CHAR_BUDGET = 5700  # запас от жёсткого лимита Discord в 6000 символов на embed

# Ключи локализации для подстановки {0} в тексты способностей уровней 3/6/9 —
# darkside/lightside используют отдельный ключ ForceAlignment_*, персонажи обычно
# резолвятся через UNIT_<TARGET_KEY>_NAME (см. _resolve_target_label), но у части
# персонажей внутренний ключ датакрона не совпадает с реальным baseId юнита —
# такие случаи прописаны здесь явно (найдено эмпирически на реальных данных).
TARGET_LABEL_OVERRIDES = {
    "darkside": "ForceAlignment_Dark",
    "lightside": "ForceAlignment_Light",
    "darthmaul": "UNIT_MAUL_NAME",
    "generalgrievous": "UNIT_GRIEVOUS_NAME",
    "t3-m4": "UNIT_T3_M4_NAME",
    "ahsokatano_snips": "UNIT_AHSOKATANO_NAME_V2",
}

# Вручную сокращённые описания способностей (цифры + суть механики, без "воды")
# для сезонов, активных на момент написания (30/31/32) — проверены на реальных
# данных comlink. "{0}" сохраняется как плейсхолдер там, где он был в оригинале
# (фракция/сторона/персонаж-генерик), подстановка происходит после этого шага.
# Для способностей будущих сезонов, которых здесь ещё нет, используется
# автоматическое сокращение (см. _auto_shorten) — не такое точное, но не даёт
# описанию обрываться посреди слова/предложения.
ABILITY_SHORT_OVERRIDES = {
    # --- Сезон 32 "Необходимые меры" ---
    # уровень 3 (текст читается как продолжение "Ветка: ..." — субъект не повторяется)
    "datacron_faction_generic_018": "лечит другого союзника → оба +5% к обороне/атаке/здоровью/защите (суммируется).",
    "datacron_faction_generic_024": "1-е падение ХП ниже 50% → неуязвим 1 ход + усиление кражи здоровья 2 хода.",
    "datacron_faction_generic_029": "в начале боя неуязвимы 2 хода (нельзя скопировать).",
    "datacron_faction_generic_005": "с 2+ снимаемыми дебаффами в начале хода — 65% шанс снять все.",
    "datacron_faction_generic_043": "лечит защиту → +2% макс. защиты (суммируется до 150%).",
    "datacron_faction_generic_023": "в начале боя получают возмездие на 3 хода (нельзя снять).",
    "datacron_faction_generic_035": "наносит урон → блокировка способностей врагу на 1 ход (если ни у кого нет).",
    "datacron_faction_generic_040": "при получении крита → 100% антикрит + 100% крит.шанс на 2 хода.",
    "datacron_faction_generic_008": "в конце хода восст. 50% от того, что вылечили враги за этот ход.",
    "datacron_faction_generic_014": "снятие дебаффа с врага → +2% эффективности/стойкости (суммируется).",
    # уровень 6
    "datacron_faction_generic_025": "с полным ХП/защитой в начале хода → +100% урона в этот ход.",
    "datacron_faction_generic_007": "накладывает дебафф в свой ход → доп. урон 10% макс.ХП врага (не увернуться).",
    "datacron_faction_generic_021": "без урона с прошлого хода → +крит.шанс и усиление атаки на 1 ход.",
    "datacron_faction_generic_016": "крит в свой ход → +10% атаки на 2 хода (суммируется).",
    "datacron_faction_generic_012": "на 1-м ходу +400% атаки от текущей обороны, затем -50% обороны.",
    "datacron_faction_generic_034": "убивает врага → доп. ход.",
    "datacron_faction_generic_015": "в начале боя теряет защиту → столько же здоровья + «живучесть» (меньше урона от % ХП, нельзя снять).",
    "datacron_faction_generic_022": "оглушает врага → ускорение+атака на 2 хода, врагу уязвимость 2 хода (не увернуться).",
    "datacron_faction_generic_011": "в конце хода восст. 50% защиты, если получил 5+ дебаффов за ход.",
    "datacron_faction_generic_013": "лечит ХП → столько же защиты (1 раз/ход, не при полном ХП).",
    # уровень 9 (для именных персонажей текст читается как продолжение "Имя: ...")
    "datacron_character_cobbvanth_001": "весь состав констебли/джавы/таскены: метка на констеблей поддержки до общего хода; 1-й «Час расплаты» по врагу — метка разбойника; по разбойнику — перезарядка -3.",
    "datacron_character_princesskneesaa_002": "атака вне очереди даёт +10% крит.урона/атаки (до 200%, до гибели); без Легенд ещё +15%, игнор защиты, чистый урон. Особая → 1 заряд DoT врагу.",
    "datacron_character_batchers3_001": "неуязвима к ошеломлению/оглушению. Урон врагу → забирает у него 15% ШХ себе (без Легенд враги не получают ШХ). «Плен» союзника Бракованной партии → сброс перезарядок, снятие дебаффов, доп. ход.",
    "datacron_character_jarjar_001": "дебафф на/от него → +10% ШХ. Помощь гунгана → потрясение или «подавленность» врагу. Весь состав гунганы: уворот → врагу +1 перезарядка, союзнику -1.",
    "datacron_character_chiefnebit_002": "призыв на помощь: +10% ХП/защиты себе за помощника, +5% другим джавам. Термальный детонатор на враге → 50% доп. детонатор на 2 хода.",
    "datacron_character_tarfful_002": "без Легенд: атака вне очереди/крит/уворот/потеря провокации → вукам 1 заряд «оплота» на 1 ход.",
    "datacron_character_ugnaught_002": "только с Хондо+Эзрой: заряды «выкупа» (до 50) → +25% обороны/атаки всем; Эзра на помощь — баффы; осн.способность — снятие дебаффов+6 помощей; в атаке — увертывание 50%(+50% без Легенд).",
    "datacron_character_tuskenshaman_001": "1-й дебафф за ход → 5 зарядов лечения себе + 2 заряда DoT врагу. Потеря заряда «импульса» → новый заряд + 2 лечения + активация всех зарядов лечения/урона.",
    "datacron_character_paploo_001": "провокация 2 хода в начале боя (нельзя снять; без Легенд игнор провокации). Особая → +50% обороны/атаки (до 300%) + 25% ХП/защиты. «Стимул» → перезарядки эвокам -2.",
    "datacron_character_talia_001": "особая по врагу с чумой → др. Сёстры ночи на помощь + 1 заряд чумы + перезарядка «Живой воды» -1. С Матерью Талзин лидером — метка заражённым врагам каждый ход.",

    # --- Сезон 31 "Старые добрые времена" ---
    # уровень 3
    "datacron_alignment_generic_025": "усиление защиты 20% на 2 хода в начале боя.",
    "datacron_alignment_generic_020": "накладывает DoT → заряд усиления защиты 5% (суммируется).",
    "datacron_alignment_generic_031": "в начале боя +15% макс.ХП/защиты за каждого другого союзника той же ветки.",
    "datacron_alignment_generic_022": "сопротивляется дебаффу → восст. 2% ХП/защиты.",
    "datacron_alignment_generic_018": "крит по врагу → восст. 2% ХП.",
    "datacron_alignment_generic_032": "даёт баф союзнику → +5% ШХ тому союзнику (1 раз/ход).",
    "datacron_alignment_generic_003": "оглушает/ошеломляет врага → восст. 10% ХП/защиты.",
    "datacron_alignment_generic_016": "1-я гибель союзника → выжившие +15% атаки, +20 скорости (суммируется).",
    "datacron_alignment_generic_002": "получает баф → восст. 2% ХП/защиты.",
    "datacron_alignment_generic_024": "в начале хода +25% шанс снять все дебаффы.",
    # уровень 6
    "datacron_faction_generic_030": "оглушение → 50% ШХ + восст. 25% ХП/защиты.",
    "datacron_faction_generic_038": "ХП < 100% → доп. ход (1 раз за бой на персонажа).",
    "datacron_faction_generic_031": "не-Легенды с ШХ ≥50% → урон по ним -75% до конца хода.",
    "datacron_faction_generic_040": "при получении крита → 100% антикрит + 100% крит.шанс на 2 хода.",
    "datacron_faction_generic_020": "оживает → длит. защита 25% на 2 хода + 25% ШХ + перезарядки -1.",
    "datacron_faction_separatist_001": "неуязвимы к ошеломлению. «Грабёж» на враге -100% крит/эффективность. Не-дроид исп. особую → рипост 1 ход.",
    "datacron_faction_separatist_002": "захват цели случайному врагу каждый ход (не увернуться). Прибавка ШХ при <100% → +3% крит.урона. Особая по цели с захватом → +3% макс.ХП (суммируется).",
    # уровень 9
    "datacron_character_generic_013": "ход врага → доп. ход с +100% урона (не срабатывает на врагах).",
    "datacron_character_trench_001": "не-дроид сепаратисты: тактическое превосходство 2 хода в начале боя. DoT-заряд → +5% крит.урона (до +200%). Защита-заряд → +5% обороны (до +200%). Осн.способность → все с рипостом на помощь. Потрясение врагу → усиление защиты 25% на 1 ход себе.",
    "datacron_character_generic_045": "+25% урона за каждый уровень усилителя реликвий у союзников.",
    "datacron_character_generic_033": "крит по врагу → неуязвим 1 ход (нельзя скопировать), +25% ШХ, слепота врагу 1 ход (не увернуться). При захвате цели — ещё усиление защиты 100%, возмездие, ускорение 2 хода.",
    "datacron_character_generic_022": "блокирует способности → +10% ШХ.",
    "datacron_character_jangofett_001": "горение → слабость всем врагам 2 хода; снятие горения → невосприимчивость к лечению 1 ход. Лидер: плата охотникам +300% обороны; после платы особая → +25% атаки (до 125%); крит охотника → доп. атака.",
    "datacron_character_carthonasi_001": "+100% макс.ХП/защиты/эффективности; ход другого перса → +15% ШХ. Осн.способность → доп. атака x2. Снятие ошеломления → +15% ШХ. Гибель врага → +25% атаки/крит.урона (суммируется), перезарядки сброшены, +100% ШХ.",
    "datacron_character_bastilashan_001": "в начале боя: +15% ШХ + усиление защиты 200% (нельзя снять) старореспубл. союзникам, танкам провокация 1 ход. Пока есть усиление защиты — +150% стойкости, +35% урона.",
    "datacron_character_zaalbar_002": "провокация танкам 1 ход в начале своего хода, пока жив — неуязвимость к оглушению/снятию бафов на них. Рваная броня врагу → на всех врагов (1 раз/ход).",
    "datacron_character_sateleshan_001": "только со Старой Республикой: 1-я потеря защиты → +100% защиты. В начале боя провокация 2 хода, невосприимчивость к снятию ШХ джедаям. Атака по защите врага → -2% макс.защиты врагу (до 20%), +5% макс.защиты союзникам (до 50%). Снятие метки → следующая осн.способность накладывает метку (нельзя снять/увернуться).",
    "datacron_character_sunfac_001": "+100% контрудар, неуязвим к ошеломлению/оглушению. Атака вне очереди → снимает свои дебаффы + чистый урон 15% макс.ХП. За каждый снятый дебафф — джеонозийцам +5% обороны/ХП/защиты.",

    # --- Сезон 30 "Мир и Сила" ---
    # уровень 3
    "datacron_alignment_generic_033": "+100% крит.шанс при атаке вне очереди.",
    "datacron_alignment_generic_014": "накладывает дебафф → +2% крит.шанса (суммируется) на 3 хода.",
    "datacron_alignment_generic_003": "оглушает/ошеломляет врага → восст. 10% ХП/защиты.",
    "datacron_alignment_generic_009": "накладывает дебафф → доп. урон 2% макс.ХП врага.",
    "datacron_alignment_generic_030": "исп. осн.способность → ускорение 2 хода.",
    "datacron_alignment_generic_019": "исп. особую способность → 15% ШХ.",
    "datacron_alignment_generic_018": "крит по врагу → восст. 2% ХП.",
    "datacron_alignment_generic_031": "в начале боя +15% макс.ХП/защиты за каждого другого союзника той же ветки.",
    "datacron_alignment_generic_025": "усиление защиты 20% на 2 хода в начале боя.",
    # уровень 6
    "datacron_faction_generic_002": "накладывает немощность/оглушение → доп.урон 35% макс.ХП врага (не увернуться).",
    "datacron_faction_generic_009": "урон врагу в начале его хода → +2% ШХ.",
    "datacron_faction_generic_038": "ХП < 100% → доп. ход (1 раз за бой на персонажа).",
    "datacron_faction_generic_029": "неуязвимы 2 хода в начале боя (нельзя скопировать).",
    "datacron_faction_sith_001": "страх/боль/потрясение на враге → +5% атаки (суммируется).",
    "datacron_faction_generic_027": "атакует вне очереди → 25% шанс снять 10% ШХ у цели.",
    "datacron_faction_generic_013": "лечит ХП → столько же защиты (1 раз/ход, не при полном ХП).",
    "datacron_faction_galacticrepublic_003": "без Легенд: помощь союзника с усилением защиты → отключение защиты врагу (нельзя снять/увернуться/сопротивляться). Пока есть усиление защиты — +50% точность/контрудар/крит.шанс/крит.урон, игнор провокации.",
    "datacron_faction_galacticrepublic_001": "крит → восст. 12% ХП.",
    # уровень 9
    "datacron_character_generic_036": "получает скрытность → перезарядки особых способностей -1.",
    "datacron_character_macewindu_001": "получение ШХ → +5% макс.ХП (до 200%). «Твёрдая оборона»+снятие «точки раскола» → точка раскола случайному врагу. Точка раскола на враге → восст. 10% ХП. Способность по оглушённому → перезарядка «Удара наповал» -1.",
    "datacron_character_palpatine_emperor_001": "1-е поражение: враг с финальным ударом получает доп.ход и гибнет. Спадание потрясения/оглушения с врага → всем врагам невосприимчивость к бафам + 1 заряд DoT + невосприимчивость к лечению на 1 ход.",
    "datacron_character_darthmaul_001": "лидер Мол → +200% атаки. Ситхи в скрытности → +100% крит.шанс/урон + восст. 30% ХП/защиты. Способность союзного Ситха → приходит на помощь.",
    "datacron_character_ahsokatano_snips_001": "с Падме лидером: способность вне очереди → усиление защиты ГР 30% на 3 хода (суммируется); снятие дебаффа с себя → усиление защиты ГР 10% на 2 хода.",
    "datacron_character_countdooku_002": "снятие рипоста → отключение защиты слабейшему врагу на 2 хода. С сепаратистом-скрытником/ситхом в составе: +30 скорости, +50% урона в свой ход, +100% урона вне очереди.",
    "datacron_character_anakinknight_001": "доп.ход → все восст. 25% ХП/защиты + перезарядки сброшены. Осн.способность при блокировке способностей → снимает с себя и накладывает врагу на 1 ход.",
    "datacron_character_depabilaba_001": "способность → заряд «расследования» врагу на 2 хода; заряд → +5% атаки (до 150%). Враг под «раскрытием» исп. особую → блокировка себе на 2 хода (не сопротивляться).",
    "datacron_character_fosithtrooper_001": "крит по врагу → скрытность 1 ход. Потеря скрытности → +30% преодоления обороны, +50% атаки на 1 ход.",
    "datacron_character_generic_011": "атакует вне очереди → восст. 20% ХП/защиты.",

    # генерик-способность без ручной записи выше вызывала фоллбэк — добавлена явно
    "datacron_character_generic_004": "накладывает дебафф → восст. 25% макс.ХП/защиты (не более раза за ход).",
}


# =====================================================================
# Построение справочника датакронов (game data + локализация) — тяжёлое,
# кешируется в bot.datacron_cache и обновляется раз в 12 часов.
# =====================================================================
def _parse_loc_kv(loc_text: str) -> dict:
    kv = {}
    for line in loc_text.split("\n"):
        if "|" not in line:
            continue
        k, _, v = line.partition("|")
        kv[k.strip()] = v.strip()
    return kv


def _resolve_ability_desc(ability_id: str, loc_kv: dict):
    prefix = f"{ability_id.upper()}_DESC"
    best = loc_kv.get(prefix)
    for v in range(2, 8):
        candidate = loc_kv.get(f"{prefix}_V{v}")
        if candidate is not None:
            best = candidate
    if best is not None:
        return best
    # Иногда номер варианта способности в abilityId не совпадает с номером
    # в ключе локализации (расхождение в данных EA — встречалось у Небита:
    # id оканчивается на _002, а описание есть только под _001, и наоборот
    # у Паплу). Для именных (не generic) персонажных шаблонов ищем ЛЮБОЙ
    # номер той же способности по имени персонажа.
    m = re.match(r"datacron_character_(?!generic)([a-z0-9]+)_\d+$", ability_id)
    if m:
        char_name = m.group(1).upper()
        base_pattern = re.compile(rf"^DATACRON_CHARACTER_{char_name}_\d+_DESC$")
        fallback_bases = sorted(k for k in loc_kv if base_pattern.match(k))
        if fallback_bases:
            fallback_prefix = fallback_bases[0]
            best = loc_kv.get(fallback_prefix)
            for v in range(2, 8):
                candidate = loc_kv.get(f"{fallback_prefix}_V{v}")
                if candidate is not None:
                    best = candidate
            return best
    return None


def _auto_shorten(text: str, limit: int = 200) -> str:
    """Фоллбэк-сокращение для способностей без ручной записи в ABILITY_SHORT_OVERRIDES
    (например, будущих сезонов) — берёт первое предложение вместо обрыва посреди текста."""
    text = text.strip()
    if len(text) <= limit:
        return text
    first_sentence = re.split(r"(?<=[.!?])\s+", text, maxsplit=1)[0]
    if len(first_sentence) <= limit:
        return first_sentence
    return first_sentence[:limit].rstrip(" ,;:") + "…"


def _resolve_target_label(target_key: str, loc_kv: dict) -> str:
    if not target_key:
        return ""
    override_key = TARGET_LABEL_OVERRIDES.get(target_key)
    if override_key and override_key in loc_kv:
        return loc_kv[override_key]
    # Порядок важен: сперва категории стороны/фракции/роли, затем — персонажи
    # (UNIT_<BASEID>_NAME; ключ найден эмпирически на реальных данных comlink).
    for key in (
        f"CATEGORY_{target_key.upper()}_DESC",
        f"CATEGORY_ROLE{target_key.upper()}_DESC",
        f"UNIT_{target_key.upper()}_NAME",
    ):
        if key in loc_kv:
            return loc_kv[key]
    return target_key


def _clean_ability_text(text: str) -> str:
    # В сырых текстах способностей встречаются буквальные "\n" (маркер переноса строки
    # для игрового клиента) — схлопываем в пробелы, чтобы текст был однострочным
    # (обязательно для опций автокомплита) и читаемым в финальном отчёте.
    return text.replace("\\n\\n", " ").replace("\\n", " ")


def _build_ability_desc(ability_id: str, target_rule: str, loc_kv: dict, target_label: str) -> str:
    template = ABILITY_SHORT_OVERRIDES.get(ability_id)
    if template is None:
        desc = _resolve_ability_desc(ability_id, loc_kv)
        if desc is None:
            return ability_id
        template = _auto_shorten(_clean_ability_text(desc))
    if "{0}" in template:
        template = template.replace("{0}", target_label)
    return template


async def _fetch_datacron_cache(comlink) -> dict:
    game_data = await asyncio.to_thread(comlink.get_game_data, items=str(DATACRON_DEFINITIONS_FLAG))
    loc = await asyncio.to_thread(comlink.get_localization, locale="RUS_RU", unzip=True)
    loc_kv = _parse_loc_kv(loc.get("Loc_RUS_RU.txt", ""))

    affix_sets = {a["id"]: a for a in game_data.get("datacronAffixTemplateSet", [])}

    now_ms = int(time.time() * 1000)
    seasons = {}
    for dset in game_data.get("datacronSet", []):
        set_id = dset.get("id")
        try:
            expiration_ms = int(dset.get("expirationTimeMs", 0))
        except (TypeError, ValueError):
            expiration_ms = 0
        if expiration_ms < now_ms:
            continue  # сезон уже завершился — не показываем в списке
        seasons[set_id] = {
            "display_name": loc_kv.get(dset.get("displayName", ""), f"Сезон {set_id}"),
            # branch_label -> {ability_id: label}; группировка по фракции/роли/персонажу,
            # чтобы все варианты одной ветки (напр. все "Танк") шли подряд в списке.
            "level3": {},
            "level6": {},
            "level9": {},
            "focused": {},  # character_key -> (label, max_tier)
        }

    for template in game_data.get("datacronTemplate", []):
        set_id = template.get("setId")
        if set_id not in seasons:
            continue  # сезон неактивен/просрочен — пропускаем

        if template.get("focused"):
            tid = str(template.get("id", ""))
            if tid.endswith("_upgraded"):
                # Дублирующее определение того же фокусного ДК — не встречается
                # в реальных данных игроков (templateId там всегда без суффикса).
                continue
            prefix = f"datacron_set_{set_id}_focused_"
            char_key = tid[len(prefix):] if tid.startswith(prefix) else tid
            char_label = _resolve_target_label(char_key, loc_kv) if char_key else tid
            max_tier = len(template.get("tier", []))
            seasons[set_id]["focused"][char_key] = (char_label, max_tier)
            continue

        for tier in template.get("tier", []):
            level = tier.get("id")
            if level not in DATACRON_LEVELS:
                continue
            level_key = f"level{level}"
            for affix_set_id in tier.get("affixTemplateSetId", []):
                affix_set = affix_sets.get(affix_set_id)
                if not affix_set:
                    continue
                affixes = affix_set.get("affix", [])
                if not affixes:
                    continue
                # Все affix одного affixTemplateSetId (одной ветки) делят один targetRule.
                branch_target_rule = affixes[0].get("targetRule", "")
                branch_key = (
                    branch_target_rule[len("target_datacron_"):]
                    if branch_target_rule.startswith("target_datacron_")
                    else ""
                )
                branch_label = _resolve_target_label(branch_key, loc_kv) if branch_key else affix_set_id
                branch_bucket = seasons[set_id][level_key].setdefault(branch_label, {})
                for affix in affixes:
                    ability_id = affix.get("abilityId")
                    if not ability_id:
                        continue
                    desc = _build_ability_desc(ability_id, affix.get("targetRule", ""), loc_kv, branch_label)
                    branch_bucket[ability_id] = f"{branch_label}: {desc}"

    result_seasons = {}
    for set_id, data in seasons.items():
        result_seasons[set_id] = {"display_name": data["display_name"]}
        for level_key in ("level3", "level6", "level9"):
            flat = []
            for branch_label in sorted(data[level_key].keys()):
                flat.extend(sorted(data[level_key][branch_label].items(), key=lambda kv: kv[1]))
            result_seasons[set_id][level_key] = flat
        result_seasons[set_id]["focused"] = sorted(
            [(char_key, label, max_tier) for char_key, (label, max_tier) in data["focused"].items()],
            key=lambda t: t[1],
        )
    return {"seasons": result_seasons}


# =====================================================================
# Мелкие хелперы форматирования/парсинга, общие для команд и автокомплитов
# =====================================================================
def _parse_trailing_bracket_int(text: str):
    if not text:
        return None
    m = re.search(r"\[(\d+)\]\s*$", text)
    return int(m.group(1)) if m else None


def _parse_leading_hash_int(text: str):
    if not text:
        return None
    m = re.match(r"#(\d+)", text.strip())
    return int(m.group(1)) if m else None


def _parse_focused_id(text: str):
    if not text:
        return None
    m = re.match(r"F(\d+)", text.strip())
    return int(m.group(1)) if m else None


def _season_label(cache, set_id) -> str:
    if cache and set_id in cache.get("seasons", {}):
        return cache["seasons"][set_id]["display_name"]
    return f"Сезон {set_id}"


def _level_label(level_options, value) -> str:
    if value == DATACRON_ANY:
        return "Любой"
    if value == DATACRON_NONE:
        return "-"
    for ability_id, label in level_options or []:
        if ability_id == value:
            return label
    return value


def _format_requirement_summary(set_id, l3, l6, l9, cache, pack=None) -> str:
    season_data = cache["seasons"].get(set_id) if cache else None
    l3_label = _level_label(season_data["level3"] if season_data else [], l3)
    l6_label = _level_label(season_data["level6"] if season_data else [], l6)
    l9_label = _level_label(season_data["level9"] if season_data else [], l9)
    pack_prefix = f"{pack}: " if pack else ""
    return f"{_season_label(cache, set_id)}: {pack_prefix}{l3_label} → {l6_label} → {l9_label}"


def _focused_char_label(cache, set_id, character_key) -> str:
    season_data = cache["seasons"].get(set_id) if cache else None
    if season_data:
        for key, label, _max_tier in season_data.get("focused", []):
            if key == character_key:
                return label
    return character_key


def _format_focused_requirement_summary(set_id, character_key, required_level, cache, pack=None) -> str:
    char_label = _focused_char_label(cache, set_id, character_key)
    pack_prefix = f"{pack}: " if pack else ""
    return f"{_season_label(cache, set_id)}: [Спец] {pack_prefix}{char_label} — уровень {required_level}+"


def _is_valid_season(cache, set_id) -> bool:
    return bool(cache) and set_id in cache.get("seasons", {})


def _is_valid_level_value(cache, set_id, level_num, value) -> bool:
    # "уровень3/6/9" — текстовый параметр с автодополнением, а не жёсткий choices=,
    # поэтому Discord не мешает ввести/вставить руками произвольный текст (например,
    # видимую подпись подсказки вместо реального значения) — проверяем сами, иначе
    # такое требование молча никогда ни с чем не совпадёт при проверке игрока.
    if value in (DATACRON_ANY, DATACRON_NONE):
        return True
    season_data = cache["seasons"].get(set_id) if cache else None
    if not season_data:
        return False
    return any(ability_id == value for ability_id, _label in season_data[f"level{level_num}"])


def _is_valid_focused_character(cache, set_id, character_key) -> bool:
    season_data = cache["seasons"].get(set_id) if cache else None
    if not season_data:
        return False
    return any(key == character_key for key, _label, _max_tier in season_data.get("focused", []))


# =====================================================================
# Построение красиво оформленных embed'ов для /дк_требования список и проверить —
# группировка по приоритету (заголовок-разделитель + по одному полю на требование).
# =====================================================================
def _branch_fallback_name(l3_label, l6_label, l9_label) -> str:
    # Без указанного "пака" используем название ветки (до ": ") как заголовок поля —
    # так у "Полезные" (без пака) отображается что-то осмысленное, а не пустота.
    for label in (l6_label, l3_label, l9_label):
        if label and label != "-" and ": " in label:
            return label.split(": ", 1)[0]
    return "Без пака"


def _requirement_value_lines(l3_label, l6_label, l9_label, comment) -> list:
    lines = []
    for tier, label in ((3, l3_label), (6, l6_label), (9, l9_label)):
        if label == "-":
            continue
        lines.append(f"**{tier} ур.:** {label}")
    if comment:
        lines.append(f"💠 *{comment}*")
    return lines


def _pack_heading(label: str) -> str:
    # "### " — markdown-заголовок внутри имени поля embed'а, крупнее обычного
    # жирного текста названия поля — выделяет пак/персонажа, под которым расписан ДК.
    return f"### {label}"


def _base_requirement_field(pack, l3_label, l6_label, l9_label, comment):
    label = pack if pack else _branch_fallback_name(l3_label, l6_label, l9_label)
    value = "\n".join(_requirement_value_lines(l3_label, l6_label, l9_label, comment)) or "​"
    return _pack_heading(label)[:256], value[:1024]


def _focused_requirement_field(pack, char_label, required_level, comment):
    label = pack if pack else char_label
    lines = [f"**Спец. датакрон:** {char_label} — уровень {required_level}+"]
    if comment:
        lines.append(f"💠 *{comment}*")
    return _pack_heading(label)[:256], "\n".join(lines)[:1024]


def _base_check_field(pack, l3_label, l6_label, l9_label, comment, matched, closed_levels):
    status = "✅" if matched else "❌"
    label = pack if pack else _branch_fallback_name(l3_label, l6_label, l9_label)
    name = f"### {status} {label}"
    lines = _requirement_value_lines(l3_label, l6_label, l9_label, comment)
    if matched and closed_levels:
        closed_parts = [v for v in closed_levels if v != "—"]
        lines.append(f"✅ Закрыто: {' → '.join(closed_parts)}")
    return name[:256], ("\n".join(lines) or "​")[:1024]


def _focused_check_field(pack, char_label, required_level, current_level, comment, ok):
    status = "✅" if ok else "❌"
    label = pack if pack else char_label
    name = f"### {status} {label}"
    lines = [f"**Спец:** {char_label} — нужен уровень {required_level}+, у игрока {current_level}"]
    if comment:
        lines.append(f"💠 *{comment}*")
    return name[:256], "\n".join(lines)[:1024]


def _build_priority_embeds(title, color, priority_items, footer_totals=None):
    """priority_items: {priority: [(field_name, field_value), ...]}, в порядке PRIORITY_ORDER.
    Заголовок категории вклеивается первой строкой в имя первого поля группы (без
    отдельной пустой строки под ним), а между группами вставляется пустое поле-разделитель.
    Разбивает на несколько embed'ов, если превышен лимит Discord на поля/символы."""
    embeds = []
    part = [1]

    def make_embed():
        t = title if part[0] == 1 else f"{title} (продолжение {part[0]})"
        return disnake.Embed(title=t[:256], color=color)

    embed = make_embed()
    char_budget = len(embed.title)
    field_count = 0

    def add_entry(name, value):
        nonlocal embed, char_budget, field_count
        entry_len = len(name) + len(value)
        if field_count >= EMBED_FIELD_LIMIT or char_budget + entry_len > EMBED_CHAR_BUDGET:
            embeds.append(embed)
            part[0] += 1
            embed = make_embed()
            char_budget = len(embed.title)
            field_count = 0
        embed.add_field(name=name, value=value, inline=False)
        field_count += 1
        char_budget += entry_len

    any_group_emitted = False
    for priority in PRIORITY_ORDER:
        items = priority_items.get(priority)
        if not items:
            continue

        if any_group_emitted:
            add_entry("​", "​")  # пустая строка-разделитель перед новой категорией
        any_group_emitted = True

        header_label = f"{PRIORITY_EMOJI[priority]} {PRIORITY_LABELS[priority]}"
        totals = (footer_totals or {}).get(priority)
        if totals:
            header_label = f"{header_label} — {totals}"

        first_name, first_value = items[0]
        add_entry(f"{header_label}\n{first_name}"[:256], first_value)
        for name, value in items[1:]:
            add_entry(name, value)

    embeds.append(embed)
    return [e for e in embeds if len(e.fields) > 0]


# =====================================================================
# Подбор подходящих датакронов игрока под требования сезона
# =====================================================================
def _extract_player_base_datacrons(player_json: dict, set_id: int):
    result = []
    for d in player_json.get("datacron", []) or []:
        if d.get("setId") != set_id or d.get("focused"):
            continue
        if not str(d.get("templateId", "")).endswith("_base"):
            continue
        affix = d.get("affix", [])
        levels = {}
        for level_num, idx in ((3, 2), (6, 5), (9, 8)):
            levels[level_num] = affix[idx].get("abilityId") if len(affix) > idx else None
        result.append({"id": d.get("id"), "levels": levels})
    return result


def _extract_player_focused_datacrons(player_json: dict, set_id: int):
    """character_key -> сколько уровней сейчас разблокировано (0, если нет такого ДК)."""
    result = {}
    prefix = f"datacron_set_{set_id}_focused_"
    for d in player_json.get("datacron", []) or []:
        if d.get("setId") != set_id or not d.get("focused"):
            continue
        tid = str(d.get("templateId", ""))
        if not tid.startswith(prefix):
            continue
        char_key = tid[len(prefix):]
        if char_key.endswith("_upgraded"):
            char_key = char_key[: -len("_upgraded")]
        current_level = len(d.get("affix", []))
        result[char_key] = max(result.get(char_key, 0), current_level)
    return result


def _level_matches(requirement_value, owned_ability_id) -> bool:
    if requirement_value == DATACRON_NONE:
        return True
    if requirement_value == DATACRON_ANY:
        return owned_ability_id is not None
    return owned_ability_id == requirement_value


def _requirement_specificity(row) -> int:
    _, _, _, l3, l6, l9, _, _, _, _ = row
    return sum(v not in (DATACRON_ANY, DATACRON_NONE) for v in (l3, l6, l9))


def _match_requirements(requirements, owned_datacrons):
    """Жадный подбор: сначала самые конкретные требования, каждый ДК занимает не больше одного слота."""
    sorted_reqs = sorted(requirements, key=lambda r: (-_requirement_specificity(r), r[0]))
    used_ids = set()
    pairs = []
    for req in sorted_reqs:
        _, _, _, l3, l6, l9, _, _, _, _ = req
        match = None
        for dc in owned_datacrons:
            if dc["id"] in used_ids:
                continue
            levels = dc["levels"]
            if (_level_matches(l3, levels[3]) and _level_matches(l6, levels[6]) and _level_matches(l9, levels[9])):
                match = dc
                break
        if match:
            used_ids.add(match["id"])
        pairs.append((req, match))
    pairs.sort(key=lambda p: p[0][0])
    return pairs


# =====================================================================
# Автокомплиты (модульные функции — как autocomplete_players/autocomplete_violations)
# =====================================================================
async def autocomplete_datacron_season(inter: disnake.ApplicationCommandInteraction, string: str):
    cache = inter.bot.datacron_cache
    if not cache:
        return ["⏳ Справочник датакронов ещё загружается, подождите..."]
    search = string.lower().strip()
    options = []
    for set_id, data in sorted(cache["seasons"].items(), key=lambda kv: -kv[0]):
        label = f"{data['display_name']} [{set_id}]"
        if not search or search in label.lower():
            options.append(label)
    return options[:25]


async def _autocomplete_datacron_level(inter: disnake.ApplicationCommandInteraction, string: str, level_num: int):
    cache = inter.bot.datacron_cache
    if not cache:
        return ["⏳ Справочник датакронов ещё загружается, подождите..."]
    set_id = _parse_trailing_bracket_int(inter.filled_options.get("сезон"))
    if set_id is None:
        return ["⚠️ СНАЧАЛА выберите сезон!"]
    season_data = cache["seasons"].get(set_id)
    if not season_data:
        return ["❌ Нет данных по этому сезону в справочнике."]

    options = [
        disnake.OptionChoice(name=DATACRON_ANY_LABEL, value=DATACRON_ANY),
        disnake.OptionChoice(name=DATACRON_NONE_LABEL, value=DATACRON_NONE),
    ]
    search = string.lower().strip()
    for ability_id, label in season_data[f"level{level_num}"]:
        if not search or search in label.lower():
            options.append(disnake.OptionChoice(name=label[:100], value=ability_id))
    return options[:25]


async def autocomplete_datacron_level3(inter, string):
    return await _autocomplete_datacron_level(inter, string, 3)


async def autocomplete_datacron_level6(inter, string):
    return await _autocomplete_datacron_level(inter, string, 6)


async def autocomplete_datacron_level9(inter, string):
    return await _autocomplete_datacron_level(inter, string, 9)


async def autocomplete_datacron_focused_character(inter: disnake.ApplicationCommandInteraction, string: str):
    cache = inter.bot.datacron_cache
    if not cache:
        return ["⏳ Справочник датакронов ещё загружается, подождите..."]
    set_id = _parse_trailing_bracket_int(inter.filled_options.get("сезон"))
    if set_id is None:
        return ["⚠️ СНАЧАЛА выберите сезон!"]
    season_data = cache["seasons"].get(set_id)
    if not season_data:
        return ["❌ Нет данных по этому сезону в справочнике."]

    search = string.lower().strip()
    options = []
    for char_key, label, max_tier in season_data.get("focused", []):
        full_label = f"{label} (макс. уровень {max_tier})"
        if not search or search in full_label.lower():
            options.append(disnake.OptionChoice(name=full_label[:100], value=char_key))
    if not options:
        return ["❌ В этом сезоне нет фокусных (спец.) датакронов."]
    return options[:25]


async def autocomplete_datacron_req_id(inter: disnake.ApplicationCommandInteraction, string: str):
    base_rows = database.get_all_datacron_requirements()
    focused_rows = database.get_all_datacron_focused_requirements()
    if not base_rows and not focused_rows:
        return ["❌ Список требований пуст."]
    cache = inter.bot.datacron_cache
    search = string.lower().strip()
    options = []
    for row in base_rows:
        req_id, set_id, pack, l3, l6, l9, comment, created_by, created_at, priority = row
        label = f"#{req_id} [{PRIORITY_LABELS.get(priority, priority)}] — {_format_requirement_summary(set_id, l3, l6, l9, cache, pack=pack)}"
        if not search or search in label.lower():
            options.append(disnake.OptionChoice(name=label[:100], value=f"#{req_id}"))
    for row in focused_rows:
        req_id, set_id, pack, character_key, required_level, comment, created_by, created_at, priority = row
        label = f"F{req_id} [{PRIORITY_LABELS.get(priority, priority)}] — {_format_focused_requirement_summary(set_id, character_key, required_level, cache, pack=pack)}"
        if not search or search in label.lower():
            options.append(disnake.OptionChoice(name=label[:100], value=f"F{req_id}"))
    return options[:25]


# =====================================================================
# Кнопка «Показать всем» на скрытом (ephemeral) отчёте /дк_требования проверить —
# публикует те же embed'ы отчёта в канал открытым сообщением по нажатию.
# =====================================================================
class DatacronCheckRevealView(disnake.ui.View):
    def __init__(self, embeds):
        super().__init__(timeout=1800)
        self.embeds = embeds
        self.revealed = False

    @disnake.ui.button(label="Показать всем", emoji="🔓", style=disnake.ButtonStyle.secondary)
    async def reveal(self, button: disnake.ui.Button, interaction: disnake.MessageInteraction):
        if self.revealed:
            await interaction.response.defer()
            return
        self.revealed = True
        button.disabled = True
        button.label = "Показано всем"
        await interaction.response.edit_message(view=self)
        for e in self.embeds:
            await interaction.channel.send(embed=e)


# =====================================================================
#                    ОСНОВНОЙ МОДУЛЬ ТРЕБОВАНИЙ К ДАТАКРОНАМ
# =====================================================================
class DatacronRequirementsCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        if not hasattr(bot, "datacron_cache"):
            bot.datacron_cache = None
        self.datacron_cache_loop.start()

    def cog_unload(self):
        self.datacron_cache_loop.cancel()

    @tasks.loop(hours=12)
    async def datacron_cache_loop(self):
        try:
            self.bot.datacron_cache = await _fetch_datacron_cache(self.bot.comlink)
            print(f"✅ [ДК] Справочник датакронов обновлён: {len(self.bot.datacron_cache['seasons'])} сезонов")
        except Exception as e:
            print(f"❌ [ДК] Ошибка обновления справочника датакронов: {e}")

    @datacron_cache_loop.before_loop
    async def _before_datacron_cache_loop(self):
        await self.bot.wait_until_ready()

    # ------------------ Slash-команды ------------------
    @commands.slash_command(name="дк_требования", description="Управление требованиями к датакронам по сезонам ТБ")
    @commands.has_any_role(1153753506772164629)
    async def datacron_req(self, inter: disnake.ApplicationCommandInteraction):
        pass

    @datacron_req.sub_command(name="добавить", description="Добавить требование к датакрону в список сезона")
    async def datacron_req_add(
        self,
        inter: disnake.ApplicationCommandInteraction,
        сезон: str = commands.Param(description="Сезон датакрона", autocomplete=autocomplete_datacron_season),
        приоритет: str = commands.Param(default=PRIORITY_REQUIRED, description="Приоритет требования", choices=PRIORITY_CHOICES),
        пак: str = commands.Param(default=None, description="На какой пак/персонажа этот датакрон (справочно, не проверяется)"),
        уровень3: str = commands.Param(default=DATACRON_NONE, description="Бонус 3 уровня", autocomplete=autocomplete_datacron_level3),
        уровень6: str = commands.Param(default=DATACRON_NONE, description="Бонус 6 уровня", autocomplete=autocomplete_datacron_level6),
        уровень9: str = commands.Param(default=DATACRON_NONE, description="Бонус 9 уровня", autocomplete=autocomplete_datacron_level9),
        комментарий: str = commands.Param(default=None, description="Заметка по приоритетным % статам (не проверяется автоматически)"),
    ):
        set_id = _parse_trailing_bracket_int(сезон)
        if set_id is None or not _is_valid_season(self.bot.datacron_cache, set_id):
            await inter.response.send_message("❌ Некорректный сезон — выберите вариант из списка автодополнения, не вводите текст вручную.", ephemeral=True)
            return
        for level_num, value in ((3, уровень3), (6, уровень6), (9, уровень9)):
            if not _is_valid_level_value(self.bot.datacron_cache, set_id, level_num, value):
                await inter.response.send_message(
                    f"❌ Некорректное значение уровня {level_num} — выберите вариант из списка автодополнения, не вводите/вставляйте текст вручную.",
                    ephemeral=True,
                )
                return
        if уровень3 == DATACRON_NONE and уровень6 == DATACRON_NONE and уровень9 == DATACRON_NONE:
            await inter.response.send_message("❌ Хотя бы один уровень (3/6/9) должен быть указан, иначе требование бессмысленно.", ephemeral=True)
            return

        req_id = database.add_datacron_requirement(set_id, пак, уровень3, уровень6, уровень9, комментарий, str(inter.author.id), приоритет)
        summary = _format_requirement_summary(set_id, уровень3, уровень6, уровень9, self.bot.datacron_cache, pack=пак)
        await inter.response.send_message(f"✅ Требование #{req_id} [{PRIORITY_LABELS[приоритет]}] добавлено: {summary}", ephemeral=True)

    @datacron_req.sub_command(name="добавить_спец", description="Добавить требование к фокусному (спец.) датакрону в список сезона")
    async def datacron_req_add_focused(
        self,
        inter: disnake.ApplicationCommandInteraction,
        сезон: str = commands.Param(description="Сезон датакрона", autocomplete=autocomplete_datacron_season),
        персонаж: str = commands.Param(description="Персонаж фокусного датакрона", autocomplete=autocomplete_datacron_focused_character),
        уровень: int = commands.Param(description="Нужный уровень прокачки (обычно 1-9, у некоторых персонажей больше)", ge=1, le=20),
        приоритет: str = commands.Param(default=PRIORITY_REQUIRED, description="Приоритет требования", choices=PRIORITY_CHOICES),
        пак: str = commands.Param(default=None, description="На какой пак/персонажа этот датакрон (справочно, не проверяется)"),
        комментарий: str = commands.Param(default=None, description="Заметка"),
    ):
        set_id = _parse_trailing_bracket_int(сезон)
        if set_id is None or not _is_valid_season(self.bot.datacron_cache, set_id):
            await inter.response.send_message("❌ Некорректный сезон — выберите вариант из списка автодополнения, не вводите текст вручную.", ephemeral=True)
            return
        if not _is_valid_focused_character(self.bot.datacron_cache, set_id, персонаж):
            await inter.response.send_message("❌ Некорректный персонаж — выберите вариант из списка автодополнения, не вводите текст вручную.", ephemeral=True)
            return

        req_id = database.add_datacron_focused_requirement(set_id, пак, персонаж, уровень, комментарий, str(inter.author.id), приоритет)
        summary = _format_focused_requirement_summary(set_id, персонаж, уровень, self.bot.datacron_cache, pack=пак)
        await inter.response.send_message(f"✅ Спец. требование F{req_id} [{PRIORITY_LABELS[приоритет]}] добавлено: {summary}", ephemeral=True)

    @datacron_req.sub_command(name="редактировать", description="Изменить требование (обычное или спец.) или удалить его из списка")
    async def datacron_req_edit(
        self,
        inter: disnake.ApplicationCommandInteraction,
        id: str = commands.Param(description="Требование для изменения", autocomplete=autocomplete_datacron_req_id),
        сезон: str = commands.Param(default=None, description="Новый сезон", autocomplete=autocomplete_datacron_season),
        приоритет: str = commands.Param(default=None, description="Новый приоритет требования", choices=PRIORITY_CHOICES),
        пак: str = commands.Param(default=None, description="Новый пак/персонаж, для которого этот датакрон (справочно, не проверяется)"),
        уровень3: str = commands.Param(default=None, description="[Обычное] новый бонус 3 уровня", autocomplete=autocomplete_datacron_level3),
        уровень6: str = commands.Param(default=None, description="[Обычное] новый бонус 6 уровня", autocomplete=autocomplete_datacron_level6),
        уровень9: str = commands.Param(default=None, description="[Обычное] новый бонус 9 уровня", autocomplete=autocomplete_datacron_level9),
        персонаж: str = commands.Param(default=None, description="[Спец.] новый персонаж", autocomplete=autocomplete_datacron_focused_character),
        уровень: int = commands.Param(default=None, description="[Спец.] новый нужный уровень прокачки", ge=1, le=20),
        комментарий: str = commands.Param(default=None, description="Новая заметка"),
        удалить: bool = commands.Param(default=False, description="Удалить это требование вместо редактирования"),
    ):
        focused_id = _parse_focused_id(id)
        if focused_id is not None:
            await self._edit_focused_requirement(
                inter, focused_id, сезон, приоритет, пак, персонаж, уровень, комментарий, удалить
            )
            return

        req_id = _parse_leading_hash_int(id)
        if req_id is None:
            await inter.response.send_message("❌ Некорректный id — выберите вариант из списка автодополнения.", ephemeral=True)
            return
        row = database.get_datacron_requirement(req_id)
        if not row:
            await inter.response.send_message(f"❌ Требование #{req_id} не найдено.", ephemeral=True)
            return

        if удалить:
            database.delete_datacron_requirement(req_id)
            await inter.response.send_message(f"🗑️ Требование #{req_id} удалено.", ephemeral=True)
            return

        _, cur_set_id, cur_pack, cur_l3, cur_l6, cur_l9, cur_comment, _, _, cur_priority = row

        new_set_id = cur_set_id
        if сезон is not None:
            parsed = _parse_trailing_bracket_int(сезон)
            if parsed is None or not _is_valid_season(self.bot.datacron_cache, parsed):
                await inter.response.send_message("❌ Некорректный сезон — выберите вариант из списка автодополнения, не вводите текст вручную.", ephemeral=True)
                return
            new_set_id = parsed

        new_priority = приоритет if приоритет is not None else cur_priority
        new_pack = пак if пак is not None else cur_pack
        new_l3 = уровень3 if уровень3 is not None else cur_l3
        new_l6 = уровень6 if уровень6 is not None else cur_l6
        new_l9 = уровень9 if уровень9 is not None else cur_l9
        new_comment = комментарий if комментарий is not None else cur_comment

        for level_num, value in ((3, new_l3), (6, new_l6), (9, new_l9)):
            if not _is_valid_level_value(self.bot.datacron_cache, new_set_id, level_num, value):
                await inter.response.send_message(
                    f"❌ Некорректное значение уровня {level_num} — выберите вариант из списка автодополнения, не вводите/вставляйте текст вручную.",
                    ephemeral=True,
                )
                return

        database.update_datacron_requirement(req_id, new_set_id, new_pack, new_l3, new_l6, new_l9, new_comment, new_priority)
        summary = _format_requirement_summary(new_set_id, new_l3, new_l6, new_l9, self.bot.datacron_cache, pack=new_pack)
        await inter.response.send_message(f"✅ Требование #{req_id} [{PRIORITY_LABELS.get(new_priority, new_priority)}] обновлено: {summary}", ephemeral=True)

    async def _edit_focused_requirement(self, inter, req_id, сезон, приоритет, пак, персонаж, уровень, комментарий, удалить):
        row = database.get_datacron_focused_requirement(req_id)
        if not row:
            await inter.response.send_message(f"❌ Спец. требование F{req_id} не найдено.", ephemeral=True)
            return

        if удалить:
            database.delete_datacron_focused_requirement(req_id)
            await inter.response.send_message(f"🗑️ Спец. требование F{req_id} удалено.", ephemeral=True)
            return

        _, cur_set_id, cur_pack, cur_char, cur_level, cur_comment, _, _, cur_priority = row

        new_set_id = cur_set_id
        if сезон is not None:
            parsed = _parse_trailing_bracket_int(сезон)
            if parsed is None or not _is_valid_season(self.bot.datacron_cache, parsed):
                await inter.response.send_message("❌ Некорректный сезон — выберите вариант из списка автодополнения, не вводите текст вручную.", ephemeral=True)
                return
            new_set_id = parsed

        new_priority = приоритет if приоритет is not None else cur_priority
        new_pack = пак if пак is not None else cur_pack
        new_char = персонаж if персонаж is not None else cur_char
        new_level = уровень if уровень is not None else cur_level
        new_comment = комментарий if комментарий is not None else cur_comment

        if not _is_valid_focused_character(self.bot.datacron_cache, new_set_id, new_char):
            await inter.response.send_message("❌ Некорректный персонаж — выберите вариант из списка автодополнения, не вводите текст вручную.", ephemeral=True)
            return

        database.update_datacron_focused_requirement(req_id, new_set_id, new_pack, new_char, new_level, new_comment, new_priority)
        summary = _format_focused_requirement_summary(new_set_id, new_char, new_level, self.bot.datacron_cache, pack=new_pack)
        await inter.response.send_message(f"✅ Спец. требование F{req_id} [{PRIORITY_LABELS.get(new_priority, new_priority)}] обновлено: {summary}", ephemeral=True)

    @datacron_req.sub_command(name="очистить_сезон", description="Удалить весь список требований неактивного сезона")
    async def datacron_req_clear_season(
        self,
        inter: disnake.ApplicationCommandInteraction,
        сезон: str = commands.Param(description="Сезон для очистки", autocomplete=autocomplete_datacron_season),
        подтвердить: bool = commands.Param(default=False, description="Установите true только после проверки количества требований для удаления"),
    ):
        set_id = _parse_trailing_bracket_int(сезон)
        if set_id is None:
            await inter.response.send_message("❌ Некорректный сезон — выберите вариант из списка автодополнения.", ephemeral=True)
            return

        season_label = _season_label(self.bot.datacron_cache, set_id)
        count_base = database.count_datacron_requirements_by_set(set_id)
        count_focused = database.count_datacron_focused_requirements_by_set(set_id)
        if count_base == 0 and count_focused == 0:
            await inter.response.send_message(f"ℹ️ У сезона {season_label} нет сохранённых требований.", ephemeral=True)
            return

        if not подтвердить:
            await inter.response.send_message(
                f"⚠️ Будет удалено требований: {count_base} обычных + {count_focused} спец. ({season_label}). "
                f"Повторите команду с подтвердить=True, чтобы подтвердить удаление.",
                ephemeral=True,
            )
            return

        deleted_base = database.delete_datacron_requirements_by_set(set_id)
        deleted_focused = database.delete_datacron_focused_requirements_by_set(set_id)
        await inter.response.send_message(
            f"🗑️ Удалено требований: {deleted_base} обычных + {deleted_focused} спец. ({season_label}).",
            ephemeral=True,
        )

    @datacron_req.sub_command(name="проверить", description="Проверить, есть ли у игрока подходящие датакроны под все требования сезона")
    async def datacron_req_check(
        self,
        inter: disnake.ApplicationCommandInteraction,
        сезон: str = commands.Param(description="Сезон для проверки", autocomplete=autocomplete_datacron_season),
        игрок: str = commands.Param(description="Выберите игрока", autocomplete=autocomplete_players),
    ):
        await inter.response.defer(ephemeral=True)

        set_id = _parse_trailing_bracket_int(сезон)
        if set_id is None:
            await inter.edit_original_message("❌ Некорректный сезон — выберите вариант из списка автодополнения.")
            return

        cache = self.bot.guild_roster_cache
        if not cache or игрок not in cache:
            await inter.edit_original_message("❌ Игрок не найден в кэше состава.")
            return
        allycode = cache[игрок]

        season_label = _season_label(self.bot.datacron_cache, set_id)
        requirements = database.get_datacron_requirements_by_set(set_id)
        focused_requirements = database.get_datacron_focused_requirements_by_set(set_id)
        if not requirements and not focused_requirements:
            await inter.edit_original_message(f"ℹ️ У сезона {season_label} нет сохранённых требований.")
            return

        try:
            player = await asyncio.wait_for(
                asyncio.to_thread(self.bot.comlink.get_player, allycode=allycode),
                timeout=15.0,
            )
        except asyncio.TimeoutError:
            await inter.edit_original_message("⏰ Запрос к Comlink занял слишком много времени.")
            return
        except Exception as e:
            await inter.edit_original_message(f"Ошибка получения данных игрока: {e}")
            return

        season_data = self.bot.datacron_cache["seasons"].get(set_id) if self.bot.datacron_cache else None

        def level_label(level_num, value):
            options = season_data[f"level{level_num}"] if season_data else []
            return _level_label(options, value)

        groups = {p: {"items": [], "matched": 0, "total": 0} for p in PRIORITY_ORDER}

        if requirements:
            owned = _extract_player_base_datacrons(player, set_id)
            pairs = _match_requirements(requirements, owned)
            for req, match in pairs:
                _, _, pack, l3, l6, l9, comment, _, _, priority = req
                group = groups.get(priority, groups[PRIORITY_REQUIRED])
                l3_lbl, l6_lbl, l9_lbl = level_label(3, l3), level_label(6, l6), level_label(9, l9)
                closed_levels = None
                if match:
                    m = match["levels"]
                    closed_levels = (
                        level_label(3, m[3]) if m[3] else "—",
                        level_label(6, m[6]) if m[6] else "—",
                        level_label(9, m[9]) if m[9] else "—",
                    )
                group["items"].append(_base_check_field(pack, l3_lbl, l6_lbl, l9_lbl, comment, bool(match), closed_levels))
                group["total"] += 1
                if match:
                    group["matched"] += 1

        if focused_requirements:
            owned_focused = _extract_player_focused_datacrons(player, set_id)
            for req in focused_requirements:
                _, _, pack, character_key, required_level, comment, _, _, priority = req
                group = groups.get(priority, groups[PRIORITY_REQUIRED])
                char_label = _focused_char_label(self.bot.datacron_cache, set_id, character_key)
                current_level = owned_focused.get(character_key, 0)
                ok = current_level >= required_level
                group["items"].append(_focused_check_field(pack, char_label, required_level, current_level, comment, ok))
                group["total"] += 1
                if ok:
                    group["matched"] += 1

        priority_items = {p: groups[p]["items"] for p in PRIORITY_ORDER}
        footer_totals = {
            p: f"{groups[p]['matched']}/{groups[p]['total']} закрыто"
            for p in PRIORITY_ORDER if groups[p]["total"] > 0
        }
        total_matched = sum(g["matched"] for g in groups.values())
        total_all = sum(g["total"] for g in groups.values())
        if total_all and total_matched == total_all:
            color = DATACRON_CHECK_COLOR_FULL
        elif total_matched:
            color = DATACRON_CHECK_COLOR_PARTIAL
        else:
            color = DATACRON_CHECK_COLOR_NONE
        title = f"📋 Проверка датакронов: {игрок} — {season_label} ({total_matched}/{total_all})"
        embeds = _build_priority_embeds(title, color, priority_items, footer_totals=footer_totals)

        view = DatacronCheckRevealView(embeds)
        await inter.edit_original_message(content=None, embed=embeds[0], view=view)
        for e in embeds[1:]:
            await inter.followup.send(embed=e, ephemeral=True)

    @datacron_req.sub_command(name="список", description="Показать весь список требуемых датакронов по активным сезонам")
    async def datacron_req_list(self, inter: disnake.ApplicationCommandInteraction):
        await inter.response.defer(ephemeral=False)

        cache = self.bot.datacron_cache
        if not cache:
            await inter.edit_original_message("⏳ Справочник датакронов ещё загружается, подождите...")
            return

        all_embeds = []
        any_found = False
        for set_id in sorted(cache["seasons"].keys(), reverse=True):
            season_data = cache["seasons"][set_id]
            base_reqs = database.get_datacron_requirements_by_set(set_id)
            focused_reqs = database.get_datacron_focused_requirements_by_set(set_id)
            if not base_reqs and not focused_reqs:
                continue
            any_found = True

            priority_items = {p: [] for p in PRIORITY_ORDER}
            for row in base_reqs:
                _, _, pack, l3, l6, l9, comment, _, _, priority = row
                l3_lbl = _level_label(season_data["level3"], l3)
                l6_lbl = _level_label(season_data["level6"], l6)
                l9_lbl = _level_label(season_data["level9"], l9)
                field = _base_requirement_field(pack, l3_lbl, l6_lbl, l9_lbl, comment)
                priority_items.get(priority, priority_items[PRIORITY_REQUIRED]).append(field)
            for row in focused_reqs:
                _, _, pack, character_key, required_level, comment, _, _, priority = row
                char_label = _focused_char_label(cache, set_id, character_key)
                field = _focused_requirement_field(pack, char_label, required_level, comment)
                priority_items.get(priority, priority_items[PRIORITY_REQUIRED]).append(field)

            title = f"📋 {season_data['display_name']} · сезон {set_id}"
            all_embeds.extend(_build_priority_embeds(title, DATACRON_LIST_COLOR, priority_items))

        if not any_found:
            await inter.edit_original_message("ℹ️ Ни у одного активного сезона нет сохранённых требований.")
            return

        await inter.edit_original_message(content=None, embed=all_embeds[0])
        for e in all_embeds[1:]:
            await inter.followup.send(embed=e, ephemeral=False)


def setup(bot):
    bot.add_cog(DatacronRequirementsCog(bot))
