import unittest
import csv
import io
from unittest.mock import AsyncMock, patch

from services import game_account as ga, game_read_decode, game_read_probes
from services import game_gear_planner, game_mod_inventory, mod_optimizer


class GameReadProbesTests(unittest.IsolatedAsyncioTestCase):
    def test_gear_calculator_consumes_shared_stock_once(self):
        equipped = ga.pack([(1, 2, b"A"), (2, 0, 0)])
        unit = game_gear_planner.unit_progress(ga.pack([
            (2, 2, b"TEST:SEVEN_STAR"), (19, 0, 11), (18, 2, equipped),
        ]))
        catalog = {("TEST", 11): ["A", "A"], ("TEST", 12): ["B"]}
        with (patch.object(game_gear_planner.database, "get_gear_recipe_items", return_value=catalog),
              patch.object(game_gear_planner.database, "get_game_equipment_names", return_value={}),
              patch.object(game_gear_planner.database, "get_bot_state",
                           return_value='{"A":[["scrap",2]],"B":[["scrap",3]]}')):
            rows, synced = game_gear_planner.calculate(unit, "gear", 13, {"scrap": 4})
        self.assertTrue(synced)
        self.assertIn(("A", 1, 0, 0, 1), rows)
        self.assertIn(("scrap", 5, 4, 1, 0), rows)

    def test_energy_shipment_and_loadout_details(self):
        with patch.object(game_read_decode.time, "time", return_value=1_800_000_000):
            energy = ga.pack([(1, 0, 1), (2, 0, 1_800_000_721), (3, 0, 100), (4, 0, 360)])
            decoded = dict(game_read_decode.decode(energy, "energy"))
        self.assertEqual(decoded["Сейчас"], 97)
        self.assertEqual(decoded["Следующая единица через"], "0 мин 01 сек")
        cost = ga.pack([(1, 2, b"PREMIUM"), (2, 0, 3), (4, 0, 200), (5, 0, 200)])
        shipment = ga.pack([(12, 2, b"offer"), (14, 2, cost)])
        self.assertEqual(dict(game_read_decode.decode(shipment, "shipment_item"))["Цена"],
                         [("Название", "Кристаллы"), ("Количество", 200)])
        loadout = ga.pack([(2, 2, b"A"), (3, 2, b"free"), (3, 2, b"missing")])
        modules = {"free": {"slot_label": "Стрела", "set_label": "Скорость", "rarity": 6, "owner": "Свободен"}}
        decoded = game_read_decode.decode(loadout, "loadout", modules=modules, inventory_complete=True)
        self.assertIn(("Модуль", "Стрела · Скорость · 6★ · Свободен"), decoded)
        self.assertIn(("Модуль", "missing · отсутствует в инвентаре"), decoded)

    def test_inventory_csv_round_trips_through_optimizer(self):
        speed = ga.pack([(2, 2, ga.pack([(3, 0, 5), (6, 0, 3_200_000_000)]))])
        offense = ga.pack([(2, 2, ga.pack([(3, 0, 48), (6, 0, 8_500_000)])), (4, 0, 3)])
        mod = ga.pack([(1, 2, b'mod,"1"'), (2, 2, b"462"), (4, 0, 15), (5, 0, 3),
                       (11, 0, 1), (12, 2, speed), (13, 2, offense)])
        unit = ga.pack([(2, 2, b"TEST:SEVEN_STAR"), (21, 2, mod)])
        with patch.object(game_mod_inventory.database, "get_game_unit_names", return_value={}):
            bank = game_mod_inventory.read_inventory([(1, 2, unit)])
        output = io.StringIO()
        csv.writer(output, quoting=csv.QUOTE_ALL).writerows(
            [bank["csv_columns"], bank["modules"][0]["csv_row"]]
        )
        parsed, = mod_optimizer.parse_mods_csv(output.getvalue().encode("utf-8-sig"))
        self.assertEqual(parsed["id"], 'mod,"1"')
        self.assertEqual(parsed["equipped_to"], "TEST")
        self.assertTrue(parsed["locked"])
        self.assertEqual(parsed["primary"]["value"], 32)
        self.assertEqual(parsed["secondaries"][0]["value"], 8.5)
        self.assertEqual(parsed["secondaries"][0]["rolls"], 3)

    def setUp(self):
        self.request = ga.pack([
            (4, 2, b"PlayerRpc"), (5, 2, b"GetInitialData"), (6, 2, b"initial-only"),
            (7, 2, b"player"), (8, 2, b"token"),
        ])

    async def test_fixed_requests_keep_identity_and_replace_initial_payload(self):
        transport = AsyncMock(side_effect=[b"", ga.AccountError("Отклонено")] + [b"\x08\x01"] * 5)
        with patch.object(ga, "_read_payload", transport):
            results = await game_read_probes.run(self.request, "player", is_current=lambda: True)

        self.assertEqual(len(results), 7)
        self.assertEqual(results[0][1], "Пустой ответ")
        self.assertEqual(results[1][1], "Отклонено")
        self.assertEqual(results[-1][1], [("Поля без расшифровки", "№1 × 1")])
        event_types = []
        for call in transport.await_args_list:
            body, service, method = call.args
            rows = ga.fields(body)
            self.assertEqual(ga._one(rows, 4, 2), service.encode())
            self.assertEqual(ga._one(rows, 5, 2), method.encode())
            self.assertEqual(ga._one(rows, 7, 2), b"player")
            self.assertEqual(ga._one(rows, 8, 2), b"token")
            payload = ga._one(rows, 6, 2)
            if method == "GetTerritoryMapStatuses":
                event_types.append(ga._one(ga.fields(payload), 1, 0))
            else:
                self.assertEqual(payload, b"")
        self.assertEqual(event_types, [9, 8, 10])

    async def test_rejects_wrong_identity_and_stops_when_link_changes(self):
        transport = AsyncMock(return_value=b"")
        with patch.object(ga, "_read_payload", transport):
            with self.assertRaises(ga.AccountError):
                await game_read_probes.run(self.request, "other", is_current=lambda: True)
            transport.assert_not_awaited()
            checks = iter([True, False])
            with self.assertRaises(ga.AccountError):
                await game_read_probes.run(self.request, "player", is_current=lambda: next(checks))
            transport.assert_awaited_once()

    async def test_expired_session_stops_sequence(self):
        transport = AsyncMock(side_effect=ga.SessionExpired)
        with patch.object(ga, "_read_payload", transport):
            results = await game_read_probes.run(self.request, "player", is_current=lambda: True)
        transport.assert_awaited_once()
        self.assertEqual(len(results), 1)

    def test_inventory_uses_wire_tags_and_decodes_mod_speed(self):
        currency = ga.pack([(1, 0, 2), (2, 0, 291)])
        material = ga.pack([(1, 2, b"xp-mat-one-star"), (2, 0, 10)])
        stat = ga.pack([(3, 0, 5), (6, 0, 3_200_000_000)])
        mod = ga.pack([(2, 2, b"462"), (12, 2, ga.pack([(2, 2, stat)]))])
        gear = ga.pack([(1, 2, b"007"), (2, 0, 4), (3, 0, 2)])
        inventory = ga.pack([(5, 2, material), (9, 2, currency), (13, 2, mod),
                             (10, 2, gear), (10, 2, gear), (99, 2, b"hidden")])
        with patch.object(game_read_decode.database, "get_game_equipment_names",
                          return_value={"007": "Деталь"}) as names:
            decoded = dict(game_read_decode.decode(inventory, "inventory"))
        names.assert_called_once_with(["007", "xp-mat-one-star"])
        self.assertEqual(dict(decoded["Валюты · 1"][0][1]), {"Валюта": "Кристаллы", "Количество": 291})
        self.assertEqual(decoded["Материалы и осколки"]["stock_rows"],
                         [("Тренировочный дроид · 1★", "xp-mat-one-star", 10, 0)])
        self.assertEqual(decoded["Снаряжение и детали"]["stock_rows"], [("Деталь", "007", 8, 4)])
        module = dict(decoded["Свободные модули · 1"][0][1])
        bank = decoded["Поиск по модулям"]["mod_inventory"]
        self.assertEqual(len(bank["modules"]), 1)
        self.assertFalse(bank["modules"][0]["assigned"])
        self.assertEqual(bank["modules"][0]["primary"]["value"], 32)
        self.assertEqual(module["Модуль"], "Стрела · Скорость · 6★")
        self.assertEqual(module["Основная характеристика"], [("Характеристика", [("Скорость", "32")])])
        self.assertEqual(decoded["Поля без расшифровки"], "№99 × 1")
