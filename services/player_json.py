"""JSON for game-player caches; keep SQLite values as text, never binary blobs."""
import json

try:
    import orjson
except ImportError:  # Older public workers can still read the shared cache.
    orjson = None

# JSON whitespace marks values written by our fast encoder. Existing writers
# (including the stable public image) emit no leading whitespace, so their
# caches retain the standard decoder's arbitrary-integer semantics. Both
# formats remain ordinary JSON text for every old reader.
_FAST_PREFIX = "\t"


def loads(value):
    prefix = _FAST_PREFIX if isinstance(value, str) else _FAST_PREFIX.encode()
    if orjson is not None and value.startswith(prefix):
        return orjson.loads(value)
    return json.loads(value)


def dumps(value):
    if orjson is not None:
        try:
            return _FAST_PREFIX + orjson.dumps(value).decode("utf-8")
        except (TypeError, OverflowError):
            # Preserve existing support for integers wider than 64 bits.
            pass
    return json.dumps(value, ensure_ascii=False)


def get_player(client, ally_code):
    """Use the client's pool, signing and gzip; decode only /player with orjson."""
    import httpx
    from swgoh_comlink import SwgohComlink
    from swgoh_comlink.exceptions import SwgohComlinkException

    if type(client) is not SwgohComlink or orjson is None:
        return client.get_player(allycode=str(ally_code))
    payload = client._get_player_payload(allycode=str(ally_code))
    headers = client._construct_request_headers("POST", "player", payload)
    try:
        response = client.client.request("POST", "/player", json=payload, headers=headers, timeout=12.0)
        response.raise_for_status()
        # Game protobuf numeric fields are bounded to 64 bits; cache decoding
        # separately preserves arbitrary integers emitted by legacy writers.
        result = orjson.loads(response.content)
        if not isinstance(result, dict):
            raise ValueError("Comlink /player returned a non-object response")
        return result
    except httpx.HTTPStatusError as exc:
        raise SwgohComlinkException(f"HTTP {exc.response.status_code}: {exc.response.text}") from exc
    except httpx.RequestError as exc:
        raise SwgohComlinkException(exc) from exc
