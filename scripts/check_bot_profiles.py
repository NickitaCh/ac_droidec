"""Read-only deployment preflight; prints missing key names, never credentials."""

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parents[1] / ".env")

from services import bot_profiles


def problems(profile):
    prefix = "PROD_" if profile == "prod" else ""
    errors = ["Не задан " + prefix + key for key in (
        "DISCORD_TOKEN", "DISCORD_CLIENT_ID", "DISCORD_CLIENT_SECRET", "TELEGRAM_BOT_TOKEN"
    ) if not os.getenv(prefix + key)]
    for platform in ("discord", "telegram"):
        try:
            bot_profiles.token(platform, profile)
        except ValueError as exc:
            errors.append(str(exc))
    if profile == "prod":
        for key in ("DATABASE_PATH", "SHARED_CACHE_DIR"):
            if not os.getenv(key) or not Path(os.environ[key]).is_absolute():
                errors.append(key + " должен указывать на общий абсолютный путь.")
        path = os.getenv("DATABASE_PATH")
        if path and not Path(path).is_file():
            errors.append("Общая база не найдена по DATABASE_PATH.")
    return errors


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--require-prod", action="store_true")
    args = parser.parse_args()
    profile = "prod" if args.require_prod else bot_profiles.runtime_profile()
    errors = problems(profile)
    for error in errors:
        print(error)
    if not errors:
        print("Настройки ботов проверены: " + profile)
    return int(bool(errors))


if __name__ == "__main__":
    raise SystemExit(main())
