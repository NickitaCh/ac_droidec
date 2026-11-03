"""Small JSON snapshot shared by gateways and web under serialized_sync."""

import json
import time

import database

KEY = "shared_datacron_catalog_v1"
TTL = 12 * 60 * 60


def load():
    raw = database.get_bot_state(KEY, guild_id=0)
    if not raw:
        return None
    try:
        payload = json.loads(raw)
        if not 0 <= time.time() - payload["created_at"] < TTL:
            return None
        catalog = payload["catalog"]
        catalog["seasons"] = {int(k): v for k, v in catalog["seasons"].items()}
        for season in catalog["seasons"].values():
            season["entries"] = {int(k): v for k, v in season["entries"].items()}
        return catalog
    except (ValueError, TypeError, KeyError, AttributeError):
        return None


def save(catalog):
    created_at = time.time()
    catalog["shared_generated_at"] = created_at
    database.set_bot_state(KEY, json.dumps({"created_at": created_at, "catalog": catalog}, ensure_ascii=False), guild_id=0)
