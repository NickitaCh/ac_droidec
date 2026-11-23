"""Publish verified protocol versions reported by game metadata."""
import asyncio
import base64
import json
import os
from pathlib import Path
import re
import tempfile
from unittest.mock import patch
import uuid

import httpx

import database
from services import ea_connect, game_account


def version_parts(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", value):
        raise ValueError("Invalid game version in metadata or configuration")
    return tuple(map(int, value.split(".")))


async def verify(candidate):
    ga = game_account
    versions = {14: candidate["external"].encode("ascii"), 15: candidate["build"].encode("ascii")}

    def rewrite(body):
        rows = ga.fields(body)
        for field in versions:
            ga._one(rows, field, 2)
        return ga.pack([(n, w, versions.get(n, v) if w == 2 else v) for n, w, v in rows])

    cfg = ea_connect.config()
    template = cfg["guest_template"]
    rows = ga.fields(ga._one(ga.fields(base64.b64decode(template)), 6, 2))
    ga._one(rows, 1, 2)
    uid = uuid.uuid4().hex.encode()
    device = uuid.uuid4().hex.encode()
    payload = ga.pack([(n, w, uid if n == 1 and w == 2 else v) for n, w, v in rows])
    request = ea_connect._envelope(template, "AuthRpc", "DoAuthGuest", payload, device=device)
    # Override the version only in this process until both requests succeed.
    with patch.object(ga, "current_client_version", rewrite):
        player, token = ea_connect._auth_result(await ea_connect._game("AuthRpc", "DoAuthGuest", request))
        await ga.read_initial(ea_connect._read_request(cfg, player, token, device), player.decode())


def publish(path, candidate):
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".game-client-version-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            json.dump(candidate, output)
            output.write("\n")
        os.chmod(temporary, 0o644)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


async def main():
    path = Path(database.DB_NAME).resolve().parent / "cache" / "game-client-version.json"
    current = json.loads(path.read_text(encoding="utf-8"))
    async with httpx.AsyncClient(timeout=15) as client:
        response = await client.post("http://127.0.0.1:3000/metadata", json={})
        response.raise_for_status()
    external = response.json()["latestGamedataVersion"].split(":", 1)[0]
    if version_parts(external) <= version_parts(current["external"]):
        print(f"Game protocol {current['external']} is current")
        return
    candidate = {"external": external, "build": current["build"]}
    await verify(candidate)
    if json.loads(path.read_text(encoding="utf-8")) != current:
        raise RuntimeError("Game client configuration changed during verification")
    publish(path, candidate)
    print(f"Published verified game protocol {external}, build {candidate['build']}")


if __name__ == "__main__":
    asyncio.run(main())
