"""Export commands offline; --apply registers them in the selected Discord scope.

Examples:
  python scripts/sync_discord_commands.py --profile prod --output public-commands.json
  python scripts/sync_discord_commands.py --profile prod --apply
  python scripts/sync_discord_commands.py --profile test --guild-id 931280548402442310 --apply
Guild updates upsert only the explicitly guild-scoped commands.
"""

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


async def run(args):
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
    os.environ["BOT_PROFILE"] = args.profile
    from services import bot_profiles
    token_key = "PROD_DISCORD_TOKEN" if args.profile == "prod" else "DISCORD_TOKEN"
    id_key = "PROD_DISCORD_CLIENT_ID" if args.profile == "prod" else "DISCORD_CLIENT_ID"
    token = bot_profiles.token("discord", args.profile)
    application_id = os.getenv(id_key)
    if args.apply and (not token or not application_id):
        raise ValueError("Для синхронизации нужны " + token_key + " и " + id_key)
    # Only the offline export may use a placeholder: it never connects to Discord.
    if not token:
        os.environ[token_key] = "offline-command-export"
    import main
    try:
        for path in sorted((ROOT / "cogs").glob("*.py")):
            if not path.name.startswith("__") and bot_profiles.extension_available(path.stem):
                main.bot.load_extension("cogs." + path.stem)
        commands = []
        for command in sorted(main.bot.slash_commands, key=lambda c: c.name):
            if args.guild_id is None and not command.guild_ids:
                commands.append(command.body.to_dict())
            elif args.guild_id is not None and args.guild_id in (command.guild_ids or ()):
                payload = command.body.to_dict()
                # These fields apply to global application commands only.
                for field in ("contexts", "integration_types", "dm_permission"):
                    payload.pop(field, None)
                commands.append(payload)
    finally:
        await main.bot.close()
    if not commands:
        raise ValueError("Пустой список команд: отправка запрещена.")
    if args.output:
        Path(args.output).write_text(json.dumps(commands, ensure_ascii=False, indent=2), encoding="utf-8")
    if args.apply:
        import httpx
        async with httpx.AsyncClient(timeout=30) as client:
            url = f"https://discord.com/api/v10/applications/{application_id}"
            headers = {"Authorization": "Bot " + token, "User-Agent": "DiscordBot (https://swgoh-sng.ru, 1.0)"}
            if args.guild_id is None:
                response = await client.put(url + "/commands", headers=headers, json=commands)
                if response.status_code != 200:
                    raise ValueError(f"Discord отклонил глобальную синхронизацию: HTTP {response.status_code}")
            else:
                for command in commands:
                    response = await client.post(f"{url}/guilds/{args.guild_id}/commands", headers=headers, json=command)
                    if response.status_code not in (200, 201):
                        raise ValueError(f"Discord отклонил /{command['name']}: HTTP {response.status_code}")
    print(f"{args.profile}: {len(commands)} команд; " + ("зарегистрированы" if args.apply else "проверены без отправки"))
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", choices=("test", "prod"), default="prod")
    parser.add_argument("--output")
    parser.add_argument("--guild-id", type=int, help="Export or upsert only commands scoped to this Discord server")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    try:
        return asyncio.run(run(args))
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
