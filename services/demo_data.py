"""Сборка demo.db — базы демо-режима сайта (services/demo_mode.py).

Гильдия, игроки, ники, коды союзника, Discord-ID, задачи, нарушения, ТБ/ВГ, энка, ДР и
плейты — выдуманы. Чтобы страницы со статами/модами выглядели живо, ростер каждого
выдуманного игрока собран «лоскутами»: каждый юнит взят у случайного игрока из общего
кэша ростеров (и с новыми id), так что ни один реальный ростер целиком не повторяется.
Игровые справочники (юниты, навыки, шмот, рецепты) копируются как есть.

Запуск в веб-контейнере: python -m services.demo_data  (пишет demo.db рядом с основной
базой; веб подхватывает её без рестарта). Пересобирать раз в неделю-две — даты событий
считаются от дня сборки."""

import hashlib
import json
import os
import random
import secrets
import sqlite3
import string
import sys
from datetime import datetime, timedelta, timezone

DEMO_GUILD_ID = 900

# Игровые справочники — копируются целиком (в них нет данных гильдий).
REF_TABLES = (
    "game_units", "game_equipment", "game_equipment_locations", "game_scavenger_recipes",
    "journey_guide_requirements", "relic_promotion_recipe", "skill_tier_thresholds",
    "unit_gear_tier_recipe", "unit_omicron_skills", "omicron_phrases", "datacron_ability_shorts",
)

NAMES = [
    "Звёздный Лис", "Kessel Runner", "Мастер Йогурт", "DarkBantha", "Тихий Дроид", "Nova Ray",
    "Банта Мирная", "Lord Pudge", "Ворчун", "Sky Walkman", "Капитан Рекс-2", "Мандо Сибирь",
    "Nightfox", "Джедай с Урала", "Пыльный Татуин", "Wookiee Hugs", "Синий Твилек", "ТайфунX",
    "Энакин Петрович", "Эвок Боевой", "Grand Admiral Bob", "Ситх Лайт", "Падаван Вася", "Zephyr",
    "Hutt Happens", "Бескар", "Midnight Jawa", "Кайбер", "Оби-Ван Тогда", "Неон", "Star Pilot 77",
    "Фрегат", "Дарт Чай", "Лея Органик", "Krayt", "Ronin", "Тень Мустафара", "Pew Pew",
    "Гранд Мофф Тортик", "Вейдер Дома", "Ion Cannon", "Мирный Сарлакк", "Tauntaun", "Nebula",
    "Сокол", "Kyber Kid", "Холодный Хот", "Rancor Rider",
]
N_MEMBERS = 45  # остальные NAMES — выбывшие

OPPONENTS = [
    "Galactic Pancakes", "Орден Пыльной Банты", "Outer Rim Outlaws", "Империя Котиков",
    "Hyperspace Hamsters", "Северный Альянс", "Moisture Farmers", "Лунный Кайбер",
    "Rogue Squadron 42", "Кантина у Мос-Эйсли", "The Sarlacc Pit", "Mandalorian Moms",
    "Тёмная Сторона Пельменей", "Bespin Cloud Club",
]
WARNS = [
    ("ТБ", "Не залил склад", ["Поздно залил склад", None, "Забыл про склад", None]),
    ("ТБ", "Не отбил БЗ", ["Отбил за 10 минут до конца", None, "Был в отпуске"]),
    ("ТБ", "Не поставил взвод", [None, "Взвод на 4-м этапе"]),
    ("ТБ", "Не отбил ОЗ", [None]),
    ("ВГ", "Не поставил деф", [None, "Деф не поставлен в первые сутки"]),
    ("ВГ", "Поздно поставил деф (1 час)", [None]),
    ("ВГ", "Не зашел в атаку", [None, "Ни одной атаки"]),
    ("ВГ", "Ошибки в атаке", ["Слил Рей в начале", None]),
    ("Рейд", "Не отбил рейд", [None]),
]
CHANNELS = {  # поле guilds -> (fake id, имя канала/роли)
    "ping_channel_id": ("900000000000000101", "оповещения"),
    "birthday_channel_id": ("900000000000000102", "дни-рождения"),
    "officer_channel_id": ("900000000000000103", "офицерская"),
    "tb_plan_channel_id": ("900000000000000104", "тб-план"),
    "tb_order_source_channel_id": ("900000000000000105", "тб-ордер"),
    "omicron_channel_id": ("900000000000000106", "омикроны"),
    "tasks_log_channel_id": ("900000000000000107", "задачи"),
    "raid_tickets_channel_id": ("900000000000000108", "энка"),
    "antispam_alert_channel_id": ("900000000000000109", "модерация"),
}
ROLES = {
    "member_role_id": "900000000000000201", "officer_role_id": "900000000000000202",
    "ping_role_id": "900000000000000201", "birthday_role_id": "900000000000000203",
    "tb_order_role_id": "900000000000000201",
}


def _code(rng, taken: set) -> str:
    while True:
        c = "".join(rng.choice("123456789") for _ in range(9))
        if c not in taken:
            taken.add(c)
            return c


def _pid(rng) -> str:
    alphabet = string.ascii_letters + string.digits + "-_"
    return "".join(rng.choice(alphabet) for _ in range(22))


def _uid() -> str:
    return secrets.token_urlsafe(16)[:22]


def _utc(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _level_score(u: dict) -> float:
    relic = (u.get("relic") or {}).get("currentTier") or 0
    return (u.get("currentRarity") or 0) * 100 + (u.get("currentTier") or 0) * 10 + relic


def _anonymize_unit(u: dict) -> dict:
    u = json.loads(json.dumps(u))
    u["id"] = _uid()
    for m in u.get("equippedStatMod") or []:
        m["id"] = _uid()
    return u


def _scale_json(obj, k: float):
    if isinstance(obj, dict):
        return {key: _scale_json(v, k) for key, v in obj.items()}
    if isinstance(obj, bool):
        return obj
    if isinstance(obj, int):
        return int(round(obj * k))
    if isinstance(obj, float):
        return obj * k
    return obj


def build(src_path: str, dst_path: str, seed: int | None = None) -> dict:
    rng = random.Random(seed)
    now = datetime.now(timezone.utc)
    today = (now + timedelta(hours=3)).date()  # МСК
    tmp = dst_path + ".tmp"
    if os.path.exists(tmp):
        os.remove(tmp)
    src = sqlite3.connect(f"file:{src_path}?mode=ro", uri=True)
    dst = sqlite3.connect(tmp)

    # ---- схема + справочники ----
    for name, sql in src.execute("SELECT name, sql FROM sqlite_master WHERE type='table' AND sql IS NOT NULL AND name NOT LIKE 'sqlite_%'"):
        dst.execute(sql)
    for (sql,) in src.execute("SELECT sql FROM sqlite_master WHERE type='index' AND sql IS NOT NULL"):
        try:
            dst.execute(sql)
        except sqlite3.OperationalError:
            pass
    for table in REF_TABLES:
        cols = [r[1] for r in src.execute(f"PRAGMA table_info({table})")]
        if not cols:
            continue
        rows = src.execute(f"SELECT {','.join(cols)} FROM {table}").fetchall()
        dst.executemany(f"INSERT INTO {table} ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})", rows)
    dst.executemany("INSERT INTO bot_state (guild_id, key, value) VALUES (?, ?, ?)", [
        r for r in src.execute("SELECT guild_id, key, value FROM bot_state WHERE guild_id = 0")
        if not r[1].startswith("openrouter")
    ])

    unit_types = dict(src.execute("SELECT base_id, unit_type FROM game_units"))
    skill_info = {r[0]: r[1:] for r in src.execute("SELECT skill_id, zeta_tier, omicron_tier FROM skill_tier_thresholds")}

    # ---- игроки ----
    taken = {r[0] for r in src.execute("SELECT DISTINCT ally_code FROM player_unit_cache")}
    taken |= {r[0] for r in src.execute("SELECT ally_code FROM user_mapping")}
    names = NAMES[:]
    rng.shuffle(names)
    players = []
    for i, name in enumerate(names):
        players.append({
            "name": name, "code": _code(rng, taken), "pid": _pid(rng),
            "discord_id": str(900000000000001000 + i),
            "strength": max(0.35, min(1.0, rng.gauss(0.72, 0.16))),
            "rank": 4 if i == 0 else (3 if i <= 5 else 2),
            "departed": i >= N_MEMBERS,
            "registered": i < 38 or i >= N_MEMBERS,
        })
    players[0]["strength"] = 0.97
    members = [p for p in players if not p["departed"]]
    departed = [p for p in players if p["departed"]]
    officer_ids = [p["discord_id"] for p in members if p["rank"] >= 3]

    # ---- ростеры «лоскутами» ----
    sample_codes = [r[0] for r in src.execute(
        "SELECT ally_code FROM player_unit_cache GROUP BY ally_code HAVING COUNT(*) > 150 ORDER BY RANDOM() LIMIT 90")]
    pool: dict[str, list] = {}
    for code in sample_codes:
        for base_id, raw in src.execute("SELECT base_id, unit_json FROM player_unit_cache WHERE ally_code = ?", (code,)):
            pool.setdefault(base_id, []).append(json.loads(raw))
    for templates in pool.values():
        templates.sort(key=_level_score)
    n_sampled = max(len(sample_codes), 1)
    rosters: dict[str, dict] = {}
    for p in players:
        s = p["strength"]
        roster = {}
        for base_id, templates in pool.items():
            if unit_types.get(base_id) == "ship":
                continue  # экипаж собирается случайно — расчёт корабля упал бы; корабли в демо не нужны
            freq = len(templates) / n_sampled
            if rng.random() > min(1.0, freq * (0.55 + 0.6 * s)):
                continue
            idx = int(len(templates) * min(0.999, max(0.0, rng.gauss(s, 0.18))))
            roster[base_id] = _anonymize_unit(templates[idx])
        rosters[p["code"]] = roster
        stamp = _utc(now - timedelta(minutes=rng.randint(5, 50)))
        dst.executemany("INSERT INTO player_unit_cache (ally_code, base_id, unit_json, updated_at) VALUES (?, ?, ?, ?)",
                        [(p["code"], b, json.dumps(u), stamp) for b, u in roster.items()])

    dc_pool = []
    for (raw,) in src.execute("SELECT datacron_json FROM player_datacron_cache ORDER BY RANDOM() LIMIT 80"):
        try:
            dc_pool.extend(json.loads(raw))
        except ValueError:
            pass
    if dc_pool:
        for p in players:
            crons = [dict(rng.choice(dc_pool)) for _ in range(rng.randint(2, 7))]
            for c in crons:
                c["id"] = _uid()
            dst.execute("INSERT INTO player_datacron_cache (ally_code, datacron_json, updated_at) VALUES (?, ?, ?)",
                        (p["code"], json.dumps(crons), _utc(now)))

    # ---- гильдия ----
    cols = [r[1] for r in src.execute("PRAGMA table_info(guilds)")]
    row = dict(zip(cols, src.execute("SELECT * FROM guilds WHERE id = 1").fetchone()))
    for k in list(row):
        if k.endswith("_channel_id") or k.endswith("_role_id"):
            row[k] = None
    row.update({
        "id": DEMO_GUILD_ID, "name": "Звёздный патруль", "ally_code": members[0]["code"],
        "swgoh_guild_id": "demo-guild", "swgoh_gg_guild_id": None, "discord_guild_id": "900000000000000001",
        "is_active": 1, "created_at": _utc(now - timedelta(days=120)), "tb_active_plan_id": None,
        "subscription_expires_at": None, "subscription_source": None, "payment_customer_id": None,
        "antispam_alert_message": None,
    })
    for k, (cid, _) in CHANNELS.items():
        if k in row:
            row[k] = cid
    for k, rid in ROLES.items():
        if k in row:
            row[k] = rid
    dst.execute(f"INSERT INTO guilds ({','.join(row)}) VALUES ({','.join('?' * len(row))})", list(row.values()))
    dst.executemany(
        "INSERT INTO guild_channels (guild_id, channel_id, channel_name, channel_type, registered_by, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
        [(DEMO_GUILD_ID, cid, name, "text", officer_ids[0], _utc(now)) for cid, name in CHANNELS.values()])
    for key, value in (("feature:raid_tickets", "1"), ("feature:tasks_mention", "0")):
        dst.execute("INSERT INTO bot_state (guild_id, key, value) VALUES (?, ?, ?)", (DEMO_GUILD_ID, key, value))
    sync_mark = (now + timedelta(hours=3) - timedelta(minutes=rng.randint(10, 40))).isoformat()
    for gid in (1, DEMO_GUILD_ID):
        dst.execute("INSERT INTO bot_state (guild_id, key, value) VALUES (?, ?, ?)", (gid, "player_units_sync_loop_last_auto_run", sync_mark))

    for p in members:
        dst.execute("INSERT INTO user_mapping (guild_id, discord_id, ally_code, ingame_name, member_level, comlink_player_id) VALUES (?, ?, ?, ?, ?, ?)",
                    (DEMO_GUILD_ID, p["discord_id"] if p["registered"] else p["code"], p["code"], p["name"], p["rank"], p["pid"]))
    for p in players:
        dst.execute("INSERT INTO known_player_names (guild_id, ally_code, ingame_name, updated_at) VALUES (?, ?, ?, ?)",
                    (DEMO_GUILD_ID, p["code"], p["name"], _utc(now)))
        if p["registered"]:
            dst.execute("INSERT INTO user_registration (guild_id, discord_id, ally_code, ingame_name, is_main, registered_at) VALUES (?, ?, ?, ?, 1, ?)",
                        (DEMO_GUILD_ID, p["discord_id"], p["code"], p["name"], _utc(now - timedelta(days=rng.randint(20, 110)))))
        dst.execute("INSERT INTO discord_member_cache (discord_id, display_name, updated_at) VALUES (?, ?, ?)",
                    (p["discord_id"], p["name"], _utc(now)))
    for p in departed:
        p["departed_at"] = now - timedelta(days=rng.randint(12, 45))
        dst.execute("INSERT INTO departed_players (guild_id, ally_code, ingame_name, comlink_player_id, departed_at) VALUES (?, ?, ?, ?, ?)",
                    (DEMO_GUILD_ID, p["code"], p["name"], p["pid"], _utc(p["departed_at"])))

    # ---- активность (дифф ростеров) ----
    weights = [("gear", 30), ("relic", 34), ("star", 18), ("zeta", 10), ("omicron", 4), ("unlock", 4)]
    kinds = [k for k, w in weights for _ in range(w)]
    events = []
    for days_ago in range(75, -1, -1):
        day = today - timedelta(days=days_ago)
        n = rng.randint(14, 34) + (12 if day.weekday() >= 5 else 0)
        for _ in range(n):
            p = rng.choices(players, weights=[0.4 + q["strength"] for q in players])[0]
            if p["departed"] and day >= (p["departed_at"] + timedelta(hours=3)).date():
                continue
            roster = rosters[p["code"]]
            if not roster:
                continue
            base_id = rng.choice(list(roster))
            unit = roster[base_id]
            kind = rng.choice(kinds)
            old, new = "", ""
            if kind == "gear":
                g = rng.randint(4, 12)
                old, new = str(g - 1), str(g)
            elif kind == "relic":
                r = rng.randint(1, 9)
                old, new = str(r - 1), str(min(10, r + (1 if rng.random() < 0.8 else 2)))
            elif kind == "star":
                st = rng.randint(2, 7)
                old, new = str(st - 1), str(st)
            elif kind == "unlock":
                new = f"{rng.choice([1, 2, 3, 4, 5, 7])}★"
            else:
                idx = 0 if kind == "zeta" else 1
                skills = [sk.get("id") for sk in unit.get("skill") or [] if (skill_info.get(sk.get("id")) or (None, None))[idx] is not None]
                if not skills:
                    continue
                new = rng.choice(skills)
            scraped = datetime.combine(day, datetime.min.time()) + timedelta(hours=rng.randint(6, 23), minutes=rng.randint(0, 59))
            archived = _utc(p["departed_at"]) if p["departed"] else None
            events.append((DEMO_GUILD_ID, p["code"], base_id, kind, old, new, day.isoformat(),
                           scraped.strftime("%Y-%m-%d %H:%M:%S"), 1, archived))
    events.sort(key=lambda e: e[7])
    dst.executemany(
        "INSERT INTO guild_activity_events (guild_id, ally_code, base_id, action_type, old_value, new_value, event_date, scraped_at, announced, archived_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        events)

    # ---- задачи ----
    def task_unit(p):
        chars = [b for b in rosters[p["code"]] if unit_types.get(b) != "ship"]
        return rng.choice(chars) if chars else None

    def relic_now(unit) -> int:
        tier = (unit.get("relic") or {}).get("currentTier") or 0
        return max(0, tier - 2) if (unit.get("currentTier") or 0) >= 13 else 0

    tasks = []
    for status, count in (("ACTIVE", 24), ("COMPLETED", 14), ("FAILED", 9)):
        for _ in range(count):
            p = rng.choice(members)
            base_id = task_unit(p)
            if not base_id:
                continue
            cur = relic_now(rosters[p["code"]][base_id])
            target = min(10, cur + rng.randint(1, 3))
            if status == "ACTIVE":
                created = now - timedelta(days=rng.randint(1, 14), hours=rng.randint(0, 20))
                deadline = (today + timedelta(days=rng.randint(2, 21))).isoformat()
                progress = rng.random() < 0.35
                initial, current = str(cur), str(min(target - 1, cur + (1 if progress else 0)))
                resolved = None
            elif status == "COMPLETED":
                created = now - timedelta(days=rng.randint(10, 45))
                resolved = created + timedelta(days=rng.randint(2, 9))
                deadline = (resolved + timedelta(days=rng.randint(1, 7))).date().isoformat()
                initial, current, progress = str(max(0, target - rng.randint(1, 3))), str(target), False
            else:
                created = now - timedelta(days=rng.randint(18, 45))
                deadline_d = (created + timedelta(days=rng.randint(7, 14))).date()
                deadline = deadline_d.isoformat()
                resolved = datetime.combine(deadline_d + timedelta(days=1), datetime.min.time(), tzinfo=timezone.utc) + timedelta(hours=7)
                initial = str(max(0, target - rng.randint(2, 3)))
                current, progress = str(int(initial) + rng.randint(0, 1)), False
            tasks.append((p["code"], base_id, "relic", str(target), deadline, status, rng.choice(officer_ids),
                          _utc(created), DEMO_GUILD_ID, initial, current, int(progress),
                          _utc(resolved) if resolved else None, 0))
    for p in rng.sample(members, 3):  # личные
        base_id = task_unit(p)
        if base_id:
            cur = relic_now(rosters[p["code"]][base_id])
            tasks.append((p["code"], base_id, "relic", str(min(10, cur + 2)), "", "ACTIVE", p["discord_id"],
                          _utc(now - timedelta(days=rng.randint(1, 6))), DEMO_GUILD_ID, str(cur), str(cur), 0, None, 1))
    for p in departed:
        base_id = task_unit(p)
        if base_id:
            tasks.append((p["code"], base_id, "relic", "8", (today - timedelta(days=5)).isoformat(), "LEFT", officer_ids[0],
                          _utc(p["departed_at"] - timedelta(days=10)), DEMO_GUILD_ID, "6", "6", 0, _utc(p["departed_at"]), 0))
    dst.executemany(
        "INSERT INTO tasks (ally_code, base_id, target_type, target_value, deadline, status, created_by, date_created, guild_id, initial_value, current_value, in_progress, resolved_at, is_personal) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        tasks)

    # ---- нарушения ----
    warns = []
    for _ in range(72):
        p = rng.choices(members, weights=[1.3 - q["strength"] for q in members])[0]
        cat, sub, comments = rng.choice(WARNS)
        day = today - timedelta(days=rng.randint(0, 88))
        warns.append((p["code"], cat, sub, day.strftime("%d.%m.%Y"), rng.choice(comments), DEMO_GUILD_ID))
    dst.executemany("INSERT INTO position_warns (ally_code, category, subcategory, date_str, comment, guild_id) VALUES (?, ?, ?, ?, ?, ?)", warns)

    # ---- ТБ: структура из реального события, все числа и имена — свои ----
    tpl = src.execute("""
        SELECT d.event_id, d.zone_data_json, d.global_totals_json, d.round_totals_json, d.raw_keys_json
        FROM tb_player_detail d JOIN tb_events e ON e.id = d.event_id
        WHERE e.guild_id = 1 ORDER BY e.id DESC, LENGTH(d.zone_data_json) DESC LIMIT 1""").fetchone()
    if tpl:
        tpl_event, zone_t, global_t, round_t, raw_keys = tpl
        zone_t, global_t, round_t = json.loads(zone_t), json.loads(global_t), json.loads(round_t)
        planets = src.execute("SELECT phase, conflict_key, planet_name FROM tb_event_planet_names WHERE event_id = ?", (tpl_event,)).fetchall()
        live_planets = src.execute("SELECT phase, conflict_key, planet_name, source FROM tb_planet_names WHERE guild_id = 1").fetchall()
        dst.executemany("INSERT INTO tb_planet_names (guild_id, phase, conflict_key, planet_name, source, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
                        [(DEMO_GUILD_ID, *r, _utc(now)) for r in live_planets])
        for i in range(6):
            completed = now - timedelta(days=2 + 14 * (5 - i), hours=rng.randint(0, 3))
            stars = min(56, 30 + 2 * i + rng.randint(0, 5))
            cur = dst.execute("INSERT INTO tb_events (guild_id, fingerprint, completed_at, stars) VALUES (?, ?, ?, ?)",
                              (DEMO_GUILD_ID, hashlib.sha1(f"demo{i}{completed}".encode()).hexdigest(), _utc(completed), stars))
            event_id = cur.lastrowid
            dst.executemany("INSERT INTO tb_event_planet_names (event_id, phase, conflict_key, planet_name) VALUES (?, ?, ?, ?)",
                            [(event_id, *r) for r in planets])
            for p in players:
                if p["departed"] and p["departed_at"] < completed:
                    continue
                if not p["departed"] and rng.random() < 0.04:
                    continue  # пропустил ТБ
                k = p["strength"] * rng.uniform(0.55, 1.15) * (0.85 + 0.04 * i)
                zone, glob, rounds = _scale_json(zone_t, k), _scale_json(global_t, k), _scale_json(round_t, k)
                dst.execute("INSERT INTO tb_player_detail (event_id, member_id, player_name, zone_data_json, global_totals_json, round_totals_json, raw_keys_json) VALUES (?, ?, ?, ?, ?, ?, ?)",
                            (event_id, p["pid"], p["name"], json.dumps(zone), json.dumps(glob), json.dumps(rounds), raw_keys))
                dst.execute("INSERT INTO tb_player_summary (event_id, member_id, player_name, summary, unit_donated, covert_attempt, strike_encounter, strike_attempt) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                            (event_id, p["pid"], p["name"], glob.get("summary", 0), glob.get("unit_donated", 0),
                             glob.get("covert_attempt", 0), glob.get("strike_encounter", 0), glob.get("strike_attempt", 0)))

    # ---- ВГ ----
    tw_rows = []
    for i in range(14):
        start = now - timedelta(days=4 + 7 * (13 - i))
        win = rng.random() < 0.68
        own = rng.randint(24000, 34000)
        opp = own - rng.randint(1500, 12000) if win else own + rng.randint(800, 8000)
        tw_rows.append((DEMO_GUILD_ID, f"tw01{'ABCD'[i % 4]}", own, max(opp, 5000), rng.randint(520, 640) * 10 ** 6,
                        OPPONENTS[i % len(OPPONENTS)], _pid(rng), rng.randint(540, 700) * 10 ** 6,
                        int(start.timestamp()), int((start + timedelta(days=1)).timestamp()), "win" if win else "loss",
                        _utc(start + timedelta(days=1, minutes=5))))
    dst.executemany("INSERT INTO tw_events (guild_id, territory_war_id, own_score, opponent_score, own_power, opponent_name, opponent_guild_id, opponent_gp, start_time, end_time, result, recorded_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    tw_rows)

    # ---- энка ----
    tickets = []
    for days_ago in range(1, 31):
        day = (today - timedelta(days=days_ago)).isoformat()
        for p in members:
            t = 600 if rng.random() < 0.9 else rng.choice([0, 200, 400, 450, 550, 580])
            tickets.append((DEMO_GUILD_ID, day, p["pid"], p["code"], p["name"], t))
    dst.executemany("INSERT INTO raid_ticket_daily (guild_id, day, player_id, ally_code, player_name, tickets) VALUES (?, ?, ?, ?, ?, ?)", tickets)

    # ---- дни рождения ----
    for p in rng.sample([q for q in members if q["registered"]], 16):
        d = today + timedelta(days=rng.randint(-60, 300))
        dst.execute("INSERT INTO birthdays (guild_id, discord_id, day, month, year) VALUES (?, ?, ?, ?, ?)",
                    (DEMO_GUILD_ID, p["discord_id"], d.day, d.month, rng.choice([None, rng.randint(1980, 2004)])))

    # ---- плейты ----
    _demo_plates(dst, rng, now, officer_ids[0], pool)

    # ---- датакроны: требования того же вида, что настраивают гильдии ----
    last_sets = [r[0] for r in src.execute("SELECT DISTINCT set_id FROM datacron_requirements ORDER BY set_id DESC LIMIT 2")]
    for set_id in last_sets:
        rows = src.execute("SELECT DISTINCT level3_value, level6_value, level9_value FROM datacron_requirements WHERE set_id = ? LIMIT 8", (set_id,)).fetchall()
        for l3, l6, l9 in rows:
            dst.execute("INSERT INTO datacron_requirements (set_id, level3_value, level6_value, level9_value, comment, created_by, created_at, pack, priority, guild_id, stats, quantity) VALUES (?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, NULL, ?)",
                        (set_id, l3, l6, l9, officer_ids[0], _utc(now - timedelta(days=10)), rng.choice(["ВГ", "ТБ", None]),
                         rng.choice(["required", "required", "optional", "useful"]), DEMO_GUILD_ID, rng.choice([1, 1, 2])))
        for key, level in src.execute("SELECT DISTINCT character_key, required_level FROM datacron_focused_requirements WHERE set_id = ? LIMIT 4", (set_id,)):
            dst.execute("INSERT INTO datacron_focused_requirements (set_id, character_key, required_level, comment, created_by, created_at, pack, priority, guild_id) VALUES (?, ?, ?, NULL, ?, ?, NULL, 'required', ?)",
                        (set_id, key, level, officer_ids[0], _utc(now - timedelta(days=10)), DEMO_GUILD_ID))

    # ---- приоритет омикронов для ВГ ----
    omis = [r[0] for r in src.execute("SELECT skill_id FROM skill_tier_thresholds WHERE omicron_tier IS NOT NULL AND omicron_mode IS NOT NULL ORDER BY RANDOM() LIMIT 10")]
    dst.executemany("INSERT INTO guild_omicron_priority (guild_id, skill_id, priority) VALUES (?, ?, ?)",
                    [(DEMO_GUILD_ID, s, i + 1) for i, s in enumerate(omis)])

    dst.commit()
    dst.execute("VACUUM")
    dst.close()
    src.close()
    os.replace(tmp, dst_path)
    return {"players": len(players), "events": len(events), "tasks": len(tasks), "warns": len(warns)}


def _demo_plates(dst, rng, now, author, pool):
    """Плейты демо — свои нормы для популярных персонажей (только тех, что есть в пуле)."""
    plates = {
        "DEMO_GL": ("Галактические легенды", {
            "GLREY": [("Speed", ">=", 330, "required"), ("Health", ">=", 95000, "optional"), ("Physical Damage", ">=", 9000, "optional")],
            "SUPREMELEADERKYLOREN": [("Speed", ">=", 320, "required"), ("Potency", ">=", 60, "optional")],
            "JEDIMASTERKENOBI": [("Speed", ">=", 330, "required"), ("Health", ">=", 110000, "optional")],
            "LORDVADER": [("Speed", ">=", 300, "required"), ("Health", ">=", 130000, "optional"), ("Relic", ">=", 7, "required")],
            "SITHPALPATINE": [("Speed", ">=", 340, "required"), ("Special Damage", ">=", 14000, "optional")],
        }),
        "DEMO_TW": ("Защита ВГ", {
            "JABBATHEHUTT": [("Speed", ">=", 290, "required"), ("Tenacity", ">=", 90, "optional")],
            "BAYLANSKOLL": [("Speed", ">=", 280, "required"), ("Health", ">=", 70000, "optional")],
            "GRANDMASTERLUKE": [("Speed", ">=", 320, "required"), ("Protection", ">=", 90000, "optional")],
            "GLAHSOKATANO": [("Speed", ">=", 330, "required"), ("Relic", ">=", 7, "required")],
            "LEIAJEDITRAINING": [("Speed", ">=", 260, "required"), ("Physical Damage", ">=", 10000, "optional")],
        }),
        "DEMO_TB": ("Спецмиссии ТБ", {
            "DARTHREVAN": [("Speed", ">=", 300, "required"), ("Relic", ">=", 7, "required")],
            "BADBATCHHUNTER": [("Health", ">=", 60000, "required"), ("Relic", ">=", 5, "optional")],
            "BOKATAN": [("Speed", ">=", 250, "required"), ("Potency", ">=", 50, "optional")],
            "COMMANDERAHSOKA": [("Speed", ">=", 300, "required"), ("Critical Damage", ">=", 200, "optional")],
        }),
    }
    created = now - timedelta(days=30)
    for name, (desc, chars) in plates.items():
        chars = {b: reqs for b, reqs in chars.items() if b in pool}
        if not chars:
            continue
        dst.execute("INSERT INTO stat_plates (guild_id, name, description, created_by, created_at, is_modular) VALUES (?, ?, ?, ?, ?, 0)",
                    (DEMO_GUILD_ID, name, desc, author, _utc(created)))
        for order, (base_id, reqs) in enumerate(chars.items()):
            dst.execute("INSERT INTO stat_plate_character_order (guild_id, plate_name, character_key, sort_order) VALUES (?, ?, ?, ?)",
                        (DEMO_GUILD_ID, name, base_id, order))
            rows = [(stat, op, float(val), prio, None, None) for stat, op, val, prio in reqs]
            rows.append(("ModSet", ">=", 1.0, "optional", "4", None))           # сет скорости
            rows.append(("ModPrimary", "=", 5.0, "required", None, "arrow"))     # стрела на скорость
            if rng.random() < 0.6:
                rows.append(("ModPrimary", "=", 55.0, "optional", "55,56", "circle"))
            for stat, op, val, prio, skill_id, slot in rows:
                dst.execute("""INSERT INTO stat_requirements (plate_name, character_key, stat_name, operator, threshold_value, priority, raw_text, comment,
                               created_by, created_at, guild_id, skill_id, mod_slot, compare_character_key, scheme_num)
                               VALUES (?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, NULL, NULL)""",
                            (name, base_id, stat, op, val, prio, f"{base_id} {stat} {op} {val}", author, _utc(created), DEMO_GUILD_ID, skill_id, slot))


if __name__ == "__main__":
    src_path = sys.argv[1] if len(sys.argv) > 1 else "guild_management.db"
    dst_path = sys.argv[2] if len(sys.argv) > 2 else os.path.join(os.path.dirname(os.path.abspath(src_path)), "demo.db")
    print(build(src_path, dst_path))
