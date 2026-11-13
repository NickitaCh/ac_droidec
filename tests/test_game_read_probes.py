import unittest
from unittest.mock import AsyncMock, patch

from services import game_account as ga, game_read_decode, game_read_probes


class GameReadProbesTests(unittest.IsolatedAsyncioTestCase):
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
        inventory = ga.pack([(5, 2, material), (9, 2, currency), (13, 2, mod), (99, 2, b"hidden")])
        decoded = dict(game_read_decode.decode(inventory, "inventory"))
        self.assertEqual(dict(decoded["Валюты · 1"][0][1]), {"Валюта": "Кристаллы", "Количество": 291})
        self.assertEqual(dict(decoded["Материалы и осколки · 1"][0][1])["Количество"], 10)
        module = dict(decoded["Свободные модули · 1"][0][1])
        self.assertEqual(module["Модуль"], "Стрела · Скорость · 6★")
        self.assertEqual(module["Основная характеристика"], [("Характеристика", [("Скорость", "32")])])
        self.assertEqual(decoded["Поля без расшифровки"], "№99 × 1")
