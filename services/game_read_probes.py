"""Read probes for explicitly enrolled accounts in the test laboratory."""

from services import game_account as ga, game_read_decode


# Request names and shapes from the game client. The event values are
# Protos.GameEventType, not the UI analytics enum with the same name.
PROBES = (
    ("Игровые наборы модулей", "StatModRpc", "GetModLoadouts", ()),
    ("Фильтры модулей", "StatModRpc", "GetModFilters", ()),
    ("Завоевание", "ConquestRpc", "GetConquestStatus", ()),
    ("Рейд", "GuildRpc", "GetGuildRaidStatuses", ()),
    ("ТБ", "TerritoryRpc", "GetTerritoryMapStatuses", ((1, 0, 9),)),
    ("ВГ", "TerritoryRpc", "GetTerritoryMapStatuses", ((1, 0, 8),)),
    ("Великая арена", "TerritoryRpc", "GetTerritoryMapStatuses", ((1, 0, 10),)),
)
RESPONSE_SCHEMAS = {
    "GetModLoadouts": "loadouts", "GetModFilters": "filters",
    "GetConquestStatus": "conquest_response", "GetGuildRaidStatuses": "raid_response",
    "GetTerritoryMapStatuses": "territory",
}


async def run(raw, expected_id, *, is_current):
    results = []
    for title, service, method, arguments in PROBES:
        if not is_current():
            raise ga.AccountError("Подключение изменилось. Проверка остановлена.")
        rows = ga.fields(ga.read_request(raw, expected_id))
        body = ga.pack([row for row in rows if row[0] not in (4, 5, 6)] + [
            (4, 2, service.encode()), (5, 2, method.encode()), (6, 2, ga.pack(arguments)),
        ])
        try:
            payload = await ga._read_payload(body, service, method, allow_empty=True)
            decoded = game_read_decode.decode(payload, RESPONSE_SCHEMAS[method])
        except ga.SessionExpired:
            results.append((title, "Сессия истекла. Остальные запросы остановлены."))
            break
        except ga.AccountError as error:
            results.append((title, str(error)))
        else:
            results.append((title, decoded if decoded else "Пустой ответ"))
    if not is_current():
        raise ga.AccountError("Подключение изменилось. Проверка остановлена.")
    return results
