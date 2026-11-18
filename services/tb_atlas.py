"""Territory layouts for the personal TB map.

Legacy zone names and mission IDs are from Comlink territoryBattleDefinition
(t01D–t04D) and RUS_RU localization. Backgrounds and reference positions:
https://genskaar.github.io/tb_geo/ and https://genskaar.github.io/tb_empire/.
Hoth uses game territory banner art (game-assets.swgoh.gg/textures,
tex.territory_banner_hoth_echo_base and tex.territory_banner_hoth_atat)
and a schematic layout by phase.
"""

import json
from pathlib import Path


EVENTS = {
    "rote": {"name": "Восход Империи", "background": "empire", "phases": 6},
    "geo-light": {"name": "Джеонозис · Наступление Республики", "background": "geonosis", "phases": 4},
    "geo-dark": {"name": "Джеонозис · Мощь сепаратистов", "background": "geonosis", "phases": 4},
    "hoth-light": {"name": "Хот · Атака повстанцев", "background": "hoth-light", "phases": 6},
    "hoth-dark": {"name": "Хот · Возмездие Империи", "background": "hoth-dark", "phases": 6},
}

ROTE_COORDINATES = {
    "Mustafar": (40.5, 76.7), "Geonosis": (36.5, 66.7), "Dathomir": (25.5, 67.7),
    "Haven-class Medical Station": (20.5, 56.7), "Malachor": (19, 41.2), "Death Star": (20.5, 28.7),
    "Corellia": (49.5, 69.7), "Felucia": (52.5, 56.7), "Tatooine": (47.5, 46.7),
    "Kessel": (57.5, 36.7), "Vandor": (52.5, 25.7), "Hoth": (61.5, 18.7),
    "Coruscant": (58.5, 73), "Bracca": (62.5, 66.7), "Kashyyyk": (74.5, 66.7),
    "Lothal": (79.5, 56.7), "Ring of Kafrene": (79.5, 40.7), "Scarif": (78.5, 27.7),
    "Zeffo": (62.5, 47), "Mandalore": (40.5, 34),
}
ROTE_ART_NAMES = {
    "Death Star": "deathstar",
    "Haven-class Medical Station": "haven",
    "Ring of Kafrene": "kafrene",
}

SHARD_REWARDS = {
    "phase03_conflict03_covert01": "Лея Органа (офицер повстанцев)",
    "hoth_empire_phase03_conflict03_covert01": "Имперский зонд-дроид",
    "geonosis_republic_phase03_conflict03_covert01": "Ки-Ади-Мунди",
    "geonosis_separatist_phase03_conflict02_covert01": "Ват Тамбор",
}

# Geo LS advances from right to left and DS advances from left to right.
GEO_SECTORS = {
    "geo-light": (
        ((76, 30), (87, 48), (87, 75)),
        ((60, 37), (64, 57), (66, 76)),
        ((43, 32), (39, 50), (38, 73)),
        ((21, 29), (19, 46), (12, 70)),
    ),
    "geo-dark": (
        ((20, 33), (12, 67)),
        ((43, 32), (39, 50), (38, 73)),
        ((60, 37), (64, 57), (66, 76)),
        ((76, 30), (87, 48), (87, 75)),
    ),
}

# One row per sector in game order, with strikes first and specials last.
# Positions come from the corresponding sector on the Genskaar Geo maps.
GEO_MISSIONS = {
    "geo-light": (
        ((66.5, 29.4),),
        ((76.5, 49.4), (79.5, 59.4), (91.5, 41.4)),
        ((80.5, 71.4), (92.5, 79.4)),
        ((55.5, 37.4), (66.5, 39.4)),
        ((57.5, 57.4), (68.5, 49.4), (69.5, 58.4)),
        ((60.5, 77.4), (64.5, 66.4), (66.5, 80.4)),
        ((36.5, 26.4), (39.5, 33.4)),
        ((30.5, 48.4), (31.5, 55.4), (45.5, 48.4)),
        ((26.5, 78.4), (46.5, 72.2)),
        ((20.5, 31.4), (24.5, 25.4)),
        ((19.5, 52.4), (13.5, 39.4), (22.5, 45.4)),
        ((13.5, 59.4), (6.5, 57.4), (16.5, 72.4)),
    ),
    "geo-dark": (
        ((11.5, 43.4), (21.5, 28.4)),
        ((11.5, 56.4), (16.5, 68.4), (5.5, 65.4)),
        ((36.5, 26.4), (39.5, 33.4)),
        ((30.5, 48.4), (37.5, 51.4), (45.5, 48.4)),
        ((26.5, 78.4), (46.5, 78.4), (43.5, 65.4)),
        ((55.5, 37.4), (62.5, 41.4)),
        ((56.5, 55.4), (68.5, 49.4), (64.5, 55.2)),
        ((60.5, 77.4), (64.5, 68.4)),
        ((66.5, 29.4),),
        ((76.5, 49.4), (77.5, 59.4), (86.5, 45.4), (85.5, 33.4)),
        ((80.5, 71.4), (86.5, 79.4), (93.5, 68.4)),
    ),
}

DATA = json.loads((Path(__file__).resolve().parents[1] / "data/tb_atlas.json").read_text(encoding="utf-8"))
# Planet CSS from Genskaar uses the same mission IDs as the squad catalog.
ROTE_MISSION_PINS = json.loads((Path(__file__).resolve().parents[1] / "data/tb_planet_pins.json").read_text(encoding="utf-8"))


def legacy_zones(event):
    zones = {}
    for index, sector in enumerate(DATA[event]):
        phase, number = sector["phase"], sector["number"]
        if event in GEO_SECTORS:
            x, y = GEO_SECTORS[event][phase - 1][number - 1]
        else:
            x, y = 10 + (phase - 1) * 16, (50 if phase < 3 else 25) + (number - 1) * 25
        missions = [
            {**mission, "requirements": [], "enemies": [], "squads": [],
             "shard_reward": SHARD_REWARDS.get(mission["id"])}
            for mission in sector["missions"]
        ]
        if event in GEO_MISSIONS:
            for mission, (pin_x, pin_y) in zip(missions, GEO_MISSIONS[event][index], strict=True):
                mission.update(x=pin_x, y=pin_y)
        zones[sector["id"]] = {
            "phase": phase, "ru": sector["name"], "x": x, "y": y, "color": "#83a9c6",
            "missions": missions,
        }
    return zones
