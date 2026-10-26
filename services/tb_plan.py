"""План ТБ «Восхода Империи» — калькулятор (веб /tb/plan), по образцу Strategy у HotUtils TB v2.

План — какие звёзды брать на каждой открытой планете по этапам (0 — копим на следующий
этап, «прелоад»). Маршрут выводится сам: планета со звездой закрывается и открывает
следующую в ветке, бонусные (Зеффо, Мандалор) — после звезды на Бракке / Татуине. Расчёт —
services/tb_forecast.py::simulate (залив ГМ — в любую планету, бои — только своей, взводы
платят при закрытии планеты), подбор — tb_forecast.forecast. Когда офицер меняет звёзды на
этапе N, этапы 1…N остаются как заданы, а N+1…6 подбираются заново под его выбор.

Хранение — таблица tb_calc_plans (здесь, не в database.py, как у mod_progress): data_json =
{"choices": {этап: {планета: звёзд}}, "preload": {этап: {планета: очков}},
"expect": {планета: % боёв}, "bonus": [бонусные планеты в плане], "deploy_pct": % ГМ | null,
"no_platoons": {этап: [планеты без взводов]}}.

Из плана собирается ордер в Discord (те же тексты этапов, что у «ордера из картинки»,
services/tb_order_image.py::build_order_blocks) и публикуется в ветку."""

import json
import sqlite3

import httpx

import database
from services import tb_forecast, tb_order_image
from services.tb_projection import BRANCHES

SIDE_OF = {p: side for side, branch in BRANCHES.items() for p in branch}
SIDE_LABEL = {"dark": "тёмная", "mixed": "нейтральная", "light": "светлая", "bonus": "бонус"}


def _ensure(cursor) -> None:
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS tb_calc_plans (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            data_json TEXT NOT NULL,
            stars INTEGER,
            order_plan_id INTEGER,
            created_by TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
    """)


def _conn():
    conn = sqlite3.connect(database.DB_NAME)
    _ensure(conn.cursor())
    conn.commit()
    return conn


def _row(r) -> dict:
    return {"id": r[0], "guild_id": r[1], "name": r[2], "data": json.loads(r[3] or "{}"), "stars": r[4],
            "order_plan_id": r[5], "created_by": r[6], "created_at": r[7], "updated_at": r[8]}


_COLS = "id, guild_id, name, data_json, stars, order_plan_id, created_by, created_at, updated_at"


def list_plans(guild_id: int) -> list[dict]:
    conn = _conn()
    rows = conn.execute(f"SELECT {_COLS} FROM tb_calc_plans WHERE guild_id = ? ORDER BY updated_at DESC, id DESC",
                        (guild_id,)).fetchall()
    conn.close()
    return [_row(r) for r in rows]


def get_plan(guild_id: int, plan_id: int) -> dict | None:
    conn = _conn()
    r = conn.execute(f"SELECT {_COLS} FROM tb_calc_plans WHERE id = ? AND guild_id = ?", (plan_id, guild_id)).fetchone()
    conn.close()
    return _row(r) if r else None


def create_plan(guild_id: int, name: str, data: dict, stars: int | None, created_by: str) -> int:
    conn = _conn()
    cur = conn.execute("INSERT INTO tb_calc_plans (guild_id, name, data_json, stars, created_by) VALUES (?, ?, ?, ?, ?)",
                       (guild_id, name, json.dumps(data, ensure_ascii=False), stars, created_by))
    conn.commit()
    plan_id = cur.lastrowid
    conn.close()
    return plan_id


def update_plan(guild_id: int, plan_id: int, data: dict = None, name: str = None, stars: int = None,
                order_plan_id: int = None) -> None:
    sets, args = ["updated_at = datetime('now')"], []
    if data is not None:
        sets.append("data_json = ?")
        args.append(json.dumps(data, ensure_ascii=False))
    if name is not None:
        sets.append("name = ?")
        args.append(name)
    if stars is not None:
        sets.append("stars = ?")
        args.append(stars)
    if order_plan_id is not None:
        sets.append("order_plan_id = ?")
        args.append(order_plan_id)
    conn = _conn()
    conn.execute(f"UPDATE tb_calc_plans SET {', '.join(sets)} WHERE id = ? AND guild_id = ?", (*args, plan_id, guild_id))
    conn.commit()
    conn.close()


def delete_plan(guild_id: int, plan_id: int) -> None:
    conn = _conn()
    conn.execute("DELETE FROM tb_calc_plans WHERE id = ? AND guild_id = ?", (plan_id, guild_id))
    conn.commit()
    conn.close()


# ------------------------------------------------------------------ расчёт

def _int_keys(d: dict) -> dict:
    return {int(k): v for k, v in (d or {}).items()}


def _params(data: dict, inp: dict) -> dict:
    """Параметры для tb_forecast.forecast/simulate из сохранённого плана."""
    expect = {p: float(v) / 100 for p, v in (data.get("expect") or {}).items() if v is not None}
    deploy = data.get("deploy_pct")
    bonus = data.get("bonus")
    return {
        "expect": expect or None,
        "deploy_share": float(deploy) / 100 if deploy not in (None, "") else None,
        "bonus": set(bonus) if bonus is not None else set((inp["zone_data"].get("bonus") or {}).keys()),
        "preload": {k: {p: float(x) for p, x in v.items()} for k, v in _int_keys(data.get("preload")).items()},
        "no_platoons": {k: list(v) for k, v in _int_keys(data.get("no_platoons")).items()},
    }


def _members(inp: dict) -> int:
    return inp.get("players") or 50


def suggest(inp: dict, data: dict = None, fixed_until: int = 0) -> dict:
    """Подобрать звёзды: этапы 1…fixed_until — как в data["choices"], остальное — прогнозом.
    → новый data (с подобранными choices)."""
    data = dict(data or {})
    params = _params(data, inp)
    choices = _int_keys(data.get("choices"))
    fixed = {ph: choices[ph] for ph in range(1, fixed_until + 1) if ph in choices}
    result = tb_forecast.forecast(inp["zone_data"], inp["platoons"], inp["covert_ready"], inp["hist"], inp["gp"],
                                  members=_members(inp), fixed=fixed, **params)
    data["choices"] = {str(ph): ks for ph, ks in result["choices"].items()}
    # прелоад, заданный руками на подобранных заново этапах, к новому маршруту уже не относится
    data["preload"] = {k: v for k, v in (data.get("preload") or {}).items() if int(k) <= fixed_until}
    return data


def evaluate(inp: dict, data: dict) -> dict:
    """Прогнать план (tb_forecast.simulate) + сводка: total, phases[...] с планетами."""
    params = _params(data, inp)
    result = tb_forecast.simulate(inp["zone_data"], inp["platoons"], inp["covert_ready"], inp["hist"], inp["gp"],
                                  _members(inp), _int_keys(data.get("choices")), **params)
    starred = {r["planet"] for ph in result["phases"] for r in ph["planets"] if r["stars"]}
    for ph in result["phases"]:
        for r in ph["planets"]:
            r["side"] = "bonus" if r["bonus"] else SIDE_OF.get(r["planet"], "")
            r["carry"] = not r["stars"] and r["planet"] in starred
            r["skip"] = not r["stars"] and r["planet"] not in starred
            r["expect_pct"] = (data.get("expect") or {}).get(r["planet"])
    result["ok"] = all(ph["ok"] for ph in result["phases"])
    result["deploy_share"] = params["deploy_share"] if params["deploy_share"] is not None \
        else tb_forecast.history_deploy_share(inp["hist"])
    result["bonus_on"] = sorted(params["bonus"])
    return result


def set_stars(inp: dict, data: dict, phase: int, planet: str, stars: int) -> dict:
    """Офицер поставил planet на этапе phase stars звёзд: этапы до phase — как были, этап phase
    — с этой правкой, следующие — подбираются заново."""
    data = dict(data)
    choices = {k: dict(v) for k, v in (data.get("choices") or {}).items()}
    choices.setdefault(str(phase), {})[planet] = int(stars)
    data["choices"] = choices
    if stars:
        pre = {k: dict(v) for k, v in (data.get("preload") or {}).items()}
        pre.get(str(phase), {}).pop(planet, None)
        data["preload"] = pre
    return suggest(inp, data, fixed_until=phase)


# ------------------------------------------------------------------ ордера без плана

IMPORTED_STATE = "tb_plan_imported_orders"  # bot_state гильдии: JSON-список id ордеров, уже ставших планами
ALL_PLANETS = [p for branch in BRANCHES.values() for p in branch] + ["Zeffo", "Mandalore"]


def order_to_data(entries: list[dict]) -> dict:
    """Разбор ветки ордера (tb_plan_reader.fetch_plan_planets) → data плана. Планета, которой нет
    в ордере этапа, — 0 звёзд (ордер не пишет 0★ на 6 этапе), иначе simulate взял бы максимум."""
    choices = {str(ph): {p: 0 for p in ALL_PLANETS} for ph in range(1, 7)}
    preload, no_platoons, bonus = {}, {}, set()
    for e in entries:
        planet, ph = e.get("planet"), str(e["round"])
        if not planet:
            continue
        if planet in ("Zeffo", "Mandalore"):
            bonus.add(planet)
        stars = e.get("stars") or 0
        if planet in ("Zeffo", "Mandalore"):
            stars = 1 if stars >= 3 else 0  # в ордере бонусная «3 звезды» = одна звезда в зачёт
        choices[ph][planet] = stars
        if not stars and e.get("preload"):
            preload.setdefault(ph, {})[planet] = e["preload"]
        if e.get("no_platoons"):
            no_platoons.setdefault(ph, []).append(planet)
    return {"choices": choices, "preload": preload, "no_platoons": no_platoons,
            "bonus": sorted(bonus), "imported": True}


async def import_orders(guild_id: int) -> list[str]:
    """Ордера (database.tb_saved_plans), у которых нет плана, — в планы (звёзды из текста ветки).
    Каждый ордер переносится один раз: удалённый потом план не возвращается. → ошибки чтения."""
    import tb_plan_reader

    linked = {p["order_plan_id"] for p in list_plans(guild_id) if p.get("order_plan_id")}
    done = set(json.loads(database.get_bot_state(IMPORTED_STATE, guild_id=guild_id) or "[]"))
    errors = []
    for order in database.get_tb_saved_plans(guild_id):
        if order["id"] in linked or order["id"] in done:
            continue
        entries, error = await tb_plan_reader.fetch_plan_planets({**order, "guild_id": guild_id})
        if error:
            errors.append(error)
            continue
        plan_id = create_plan(guild_id, order["name"][:80], order_to_data(entries), order.get("total_stars"),
                              "import:order")
        update_plan(guild_id, plan_id, order_plan_id=order["id"])
        done.add(order["id"])
        database.set_bot_state(IMPORTED_STATE, json.dumps(sorted(done)), guild_id=guild_id)
    return errors


def active_plan_id(guild_id: int, plans: list[dict]) -> int | None:
    """План, чей ордер активный (guilds.tb_active_plan_id)."""
    cfg = database.get_guild_config(guild_id) or {}
    order_id = cfg.get("tb_active_plan_id")
    if not order_id:
        return None
    return next((p["id"] for p in plans if p.get("order_plan_id") == int(order_id)), None)


def plan_for_order(guild_id: int, order_id: int) -> dict | None:
    return next((p for p in list_plans(guild_id) if p.get("order_plan_id") == order_id), None)


# ------------------------------------------------------------------ ордер в Discord

def order_data(result: dict) -> dict:
    """План → структура для tb_order_image.build_order_blocks (как ответ vision-модели)."""
    rounds = []
    for ph in result["phases"]:
        row = {"round": ph["phase"], "dark": None, "mixed": None, "light": None, "bonus": []}
        for r in ph["planets"]:
            if r["bonus"]:
                # у бонусной планеты одна звезда за полное заполнение — в ордере это «3»
                row["bonus"].append({"planet": r["planet"], "stars": 3 if r["stars"] else 0,
                                     "preload": None if r["stars"] else r["points"],
                                     "no_platoons": r["no_platoons"]})
            else:
                row[SIDE_OF[r["planet"]]] = {"planet": r["planet"], "stars": r["stars"],
                                             "preload": None if r["stars"] else r["points"],
                                             "no_platoons": r["no_platoons"]}
        rounds.append(row)
    while len(rounds) < 6:
        rounds.append({"round": len(rounds) + 1, "dark": None, "mixed": None, "light": None, "bonus": []})
    return {"rounds": rounds}


class PublishError(Exception):
    pass


async def publish(guild_id: int, result: dict, link: str, name: str, force: bool, created_by: str) -> tuple[int, str]:
    """Опубликовать ордер плана в ветку Discord и сохранить как план ордера.
    → (id плана ордера, сообщение)."""
    import os
    import tb_platoon_autofill

    blocks, total = tb_order_image.build_order_blocks(order_data(result))
    thread_id = tb_order_image._thread_id_from_link(link)
    if thread_id is None:
        raise PublishError("Не удалось распознать ссылку на ветку.")
    token = os.environ.get("DISCORD_TOKEN")
    if not token:
        raise PublishError("DISCORD_TOKEN не настроен на сервере.")
    headers = {"Authorization": f"Bot {token}", "User-Agent": "DiscordBot (https://swgoh-sng.ru, 1.0)"}
    api = tb_order_image.DISCORD_API
    async with httpx.AsyncClient(headers=headers, timeout=30) as client:
        me = await client.get(f"{api}/users/@me")
        me.raise_for_status()
        bot_id = me.json()["id"]
        resp = await client.get(f"{api}/channels/{thread_id}")
        if resp.status_code in (403, 404):
            raise PublishError("Ветка не найдена или у бота нет к ней доступа — проверьте ссылку.")
        resp.raise_for_status()
        thread_name = resp.json().get("name") or str(thread_id)
        if not force:
            existing = await tb_order_image._bot_order_phases(client, thread_id, bot_id)
            if existing:
                raise PublishError(f"В ветке уже есть ордер для этапов: {', '.join(map(str, existing))}. "
                                   "Повторная публикация создаст дубли — если план поменялся, отметьте «Опубликовать заново».")
        for block in blocks:
            resp = await client.post(f"{api}/channels/{thread_id}/messages",
                                     json={"content": block, "allowed_mentions": {"parse": []}})
            if resp.status_code >= 400:
                raise PublishError(f"Публикация прервалась (не всё могло уйти в ветку): Discord {resp.status_code}.")
    order_id = database.save_tb_plan(guild_id, name, thread_id, total, created_by=created_by)
    parts = [f"Опубликовано 6 этапов в ветку «{thread_name}», ордер сохранён как «{name}» ({total}★)."]
    try:
        parts.append(tb_order_image.summarize_autofill(await tb_platoon_autofill.autofill_plan(guild_id, order_id)))
    except (ValueError, RuntimeError) as e:
        parts.append(f"Взводы автоматически не расставлены: {e}.")
    return order_id, " ".join(parts)
