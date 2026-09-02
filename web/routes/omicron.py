from pathlib import Path
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

import database
from services.units_sync import sync_units
from web.deps import require_officer_access

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent.parent / "templates"))


def _get_comlink():
    # См. web/routes/registration.py::_get_comlink — веб-процесс не поднимает
    # диска-клиента, строит свой SwgohComlink на тот же comlink-сайдкар.
    from swgoh_comlink import SwgohComlink
    return SwgohComlink(url="http://localhost:3000")


# =====================================================================
# ФРАЗЫ-ПРИПИСКИ ДЛЯ АВТООБЪЯВЛЕНИЙ О ВЫДАЧЕ ОМИКРОНОВ
# Справочник глобальный (не per-guild, см. database.py::_ensure_omicron_phrases_table),
# но, в отличие от /admin/guilds и /admin/access, это гильдийская фича, а не
# настройка бота — доступна обычным офицерам (require_officer_access), не только
# супер-админам; до 2026-09-02 жила под /admin и была ограничена супер-админами,
# перенесена в собственный раздел меню по просьбе пользователя. Сама детекция
# выдачи и отправка сообщения в Discord — cogs/stat_requirements.py::_announce_omicrons,
# эта страница только редактирует соответствия персонаж(+омикрон) → фраза, ничего не постит.
#
# 2026-08-31: раньше страница была формой добавления + таблицей уже заданных фраз (нужно
# было заранее знать персонажа). Переделано в один сплошной список ВСЕХ персонажей, у
# которых В ИГРЕ есть омикрон (database.get_all_unit_omicron_skills, тот же источник, что
# game_units.has_omicron) — сразу видно, у кого фраза уже задана, а у кого нет, без
# отдельного поиска/формы. У персонажа с несколькими омикронами (isOmicronTier на
# нескольких способностях) первая строка — фраза "по умолчанию" (skill_id='', используется
# для любого его омикрона, пока не переопределена), плюс по строке на каждый конкретный
# омикрон с собственным override — см. database.get_omicron_phrase за порядком резолва.
# =====================================================================
def _omicron_phrase_status(own_phrase: str, default_phrase: str, is_override_row: bool) -> tuple:
    if own_phrase:
        return ("badge-ok", "✅ своя" if is_override_row else "✅ задана")
    if is_override_row and default_phrase:
        return ("badge-neutral", "↳ по умолчанию")
    return ("badge-neutral", "— не задана")


# Порядок режимов в выпадающем фильтре — сперва самые актуальные для гильдии (ВГ/ТБ/ВА/рейд),
# остальные (см. services/units_sync.py::_OMICRON_MODE_LABELS) — по алфавиту следом.
_OMICRON_MODE_FILTER_ORDER = ["ВГ", "ТБ", "ВА", "рейд", "арена", "PvE", "PvE-ивент", "покорение", "галактический вызов", "все режимы"]


def _sort_modes(modes) -> list:
    order = {m: i for i, m in enumerate(_OMICRON_MODE_FILTER_ORDER)}
    return sorted(modes, key=lambda m: (order.get(m, len(order)), m))


@router.get("/phrases", response_class=HTMLResponse)
async def omicron_phrases_page(request: Request, user: dict = Depends(require_officer_access)):
    skills_by_base = database.get_all_unit_omicron_skills()
    skill_info = database.get_skill_display_info([sid for sids in skills_by_base.values() for sid in sids])
    names = database.get_game_unit_names(list(skills_by_base.keys()))

    phrase_map = {(r[1], r[2]): r for r in database.get_all_omicron_phrases()}  # (character_key, skill_id) -> row
    username_cache = {}

    def _updated_by_name(discord_id):
        if not discord_id:
            return None
        if discord_id not in username_cache:
            username_cache[discord_id] = database.get_username_for_discord_id(discord_id)
        return username_cache[discord_id]

    characters = []
    all_modes = set()
    for base_id, skill_ids in skills_by_base.items():
        name = names.get(base_id) or base_id
        default_row = phrase_map.get((base_id, ""))
        default_phrase = default_row[3] if default_row else ""

        omicrons = []
        char_modes = set()
        for skill_id in skill_ids:
            ability_name, _ability_id, ability_type, omicron_mode = skill_info.get(skill_id, (None, None, None, None))
            extra = " / ".join(p for p in (ability_type, omicron_mode) if p)
            row = phrase_map.get((base_id, skill_id))
            own_phrase = row[3] if row else ""
            badge_class, badge_text = _omicron_phrase_status(own_phrase, default_phrase, is_override_row=True)
            if omicron_mode:
                char_modes.add(omicron_mode)
                all_modes.add(omicron_mode)
            omicrons.append({
                "skill_id": skill_id,
                "label": ability_name or skill_id,
                "extra": extra,
                "mode": omicron_mode or "",
                "phrase": own_phrase,
                "updated_by_name": _updated_by_name(row[4]) if row else None,
                "badge_class": badge_class,
                "badge_text": badge_text,
            })
        omicrons.sort(key=lambda o: o["label"].lower())

        default_badge_class, default_badge_text = _omicron_phrase_status(default_phrase, default_phrase, is_override_row=False)
        characters.append({
            "base_id": base_id,
            "name": name,
            "default_phrase": default_phrase,
            "default_updated_by_name": _updated_by_name(default_row[4]) if default_row else None,
            "default_badge_class": default_badge_class,
            "default_badge_text": default_badge_text,
            "omicrons": omicrons,
            "modes": " ".join(_sort_modes(char_modes)),
            "multi": len(omicrons) > 1,
        })
    characters.sort(key=lambda c: c["name"].lower())

    return templates.TemplateResponse(request, "omicron_phrases.html", {
        "user": user,
        "characters": characters,
        "modes": _sort_modes(all_modes),
        "error": request.query_params.get("error"),
        "synced": request.query_params.get("synced"),
    })


@router.post("/phrases/set", response_class=HTMLResponse)
async def omicron_phrases_set(
    character_key: str = Form(...),
    skill_id: str = Form(""),
    phrase: str = Form(""),
    user: dict = Depends(require_officer_access),
):
    character_key = character_key.strip()
    skill_id = skill_id.strip()
    phrase = phrase.strip()
    if not character_key:
        return RedirectResponse(f"/omicron/phrases?{urlencode({'error': 'Некорректный персонаж.'})}", status_code=303)
    # Пустое поле = убрать фразу (единая точка входа вместо отдельных add/edit/delete —
    # так и default-строка, и override на конкретный омикрон правятся одной формой).
    if phrase:
        database.set_omicron_phrase(character_key, phrase, user["discord_id"], skill_id=skill_id)
    else:
        database.delete_omicron_phrase(character_key, skill_id=skill_id)
    return RedirectResponse("/omicron/phrases", status_code=303)


@router.post("/phrases/sync", response_class=HTMLResponse)
async def omicron_phrases_sync(user: dict = Depends(require_officer_access)):
    # Тот же sync_units, что кнопка "Обновить справочник юнитов" на /tasks (см.
    # web/routes/tasks.py::sync_units_now) — он и так раз в час обновляет справочник
    # омикронов (services/units_sync.py::sync_units), кнопка здесь просто даёт запустить
    # его вручную сразу, не дожидаясь цикла, если появился новый персонаж/омикрон.
    comlink = _get_comlink()
    try:
        total = await sync_units(comlink)
    except Exception as e:
        return RedirectResponse(f"/omicron/phrases?{urlencode({'error': f'Ошибка синхронизации: {e}'})}", status_code=303)
    return RedirectResponse(f"/omicron/phrases?{urlencode({'synced': str(total)})}", status_code=303)
