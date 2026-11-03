"""Export commands offline; --apply explicitly registers them globally in Discord.

Examples:
  python scripts/sync_discord_commands.py --profile prod --output public-commands.json
  python scripts/sync_discord_commands.py --profile prod --apply
Never starts a gateway, sets test_guilds, or pushes an empty command list.
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
        commands = [command.body.to_dict() for command in sorted(main.bot.slash_commands, key=lambda c: c.name)]
    finally:
        await main.bot.close()
    if not commands:
        raise ValueError("Пустой список команд: отправка запрещена.")
    if args.output:
        Path(args.output).write_text(json.dumps(commands, ensure_ascii=False, indent=2), encoding="utf-8")
    if args.apply:
        import httpx
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.put(f"https://discord.com/api/v10/applications/{application_id}/commands",
                                        headers={"Authorization": "Bot " + token}, json=commands)
        if response.status_code != 200:
            raise ValueError(f"Discord отклонил синхронизацию: HTTP {response.status_code}")
    print(f"{args.profile}: {len(commands)} команд; " + ("зарегистрированы" if args.apply else "проверены без отправки"))
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", choices=("test", "prod"), default="prod")
    parser.add_argument("--output")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    try:
        return asyncio.run(run(args))
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
